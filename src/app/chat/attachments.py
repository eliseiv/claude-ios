"""Inline base64 multimodal attachment validation, neutral parts and their provider rendering.

Implements ADR-020 (inline base64 attachments MVP), the per-provider mapping of ADR-033 §5 /
ADR-041, the attachment threat model in 05-security.md and ADR-105 §A2. Attachments arrive inline
in the first user message-step of /v1/chat/run.

Validation pipeline per attachment (order matters — limits BEFORE decode; SHARED across providers,
ADR-033 §5):
1. mediaType is on the fixed allowlist for its class (else 422 unsupported_media_type);
2. base64-string length implies a decoded size within the per-class and total byte limits
   (checked BEFORE b64decode to bound memory — anti memory-DoS);
3. base64 is well-formed (else 422, never 500);
4. decoded content matches the declared mediaType by magic bytes / UTF-8 / JSON parse
   (anti MIME-spoof — never trust the client's mediaType);
5. PDF: page-count guard via pypdf (anti decompression/structure bomb).

ADR-105 §A1/§A2: validation produces NEUTRAL parts (``AttachmentPart``), never provider blocks. The
form of a provider-dependent input is decided by the client that SENDS the call, at call time: each
client calls ``render_attachment_blocks(parts, <its own provider>)``. The caller never names a
provider, so it cannot name the wrong one — which is exactly how a Claude session that failed over
to OpenAI used to lose its picture (Anthropic blocks silently dropped by the Responses client).

``render_attachment_blocks`` is the ONLY mapping from a neutral part to a wire block (the workspace
knowledge files reuse it — there is no second copy of the image mapping). Block shapes are
unchanged (ADR-033 §5, ADR-041):
- ``anthropic``: image ``{type:image,source:{type:base64,...}}``, document(PDF)
  ``{type:document,...}``, text — a plain text block;
- ``openai``: image → ``{type:image_url,image_url:{url:"data:<mediaType>;base64,<data>"}}``, text →
  text block, and **PDF → native ``file`` content-part**
  ``{type:"file",file:{filename,file_data:"data:application/pdf;base64,<data>"}}`` (ADR-041).

Rendering is TOTAL (ADR-105 §A2.4): one wire block per part; a part that cannot be rendered raises
``UnrenderableAttachmentError`` BEFORE the upstream call, it is never skipped.

Raw base64 is NEVER persisted: chat_steps.payload stores a light text placeholder instead
(ADR-020 §3 storage invariant; placeholders are provider-agnostic). The base64 string lives in ONE
copy in memory: a rendered Anthropic block references the part's own string. The Anthropic PDF
document-block is emitted as a raw dict per the wire format because anthropic 0.39.0 has no
DocumentBlockParam (TD-016); the backend already sends messages as raw dicts.

Raises ValidationFailedError (-> 422) for every rejection so attachment errors are technical
validation failures, never 500s. Attachment bytes/text never reach logs (redaction is upstream;
this module never logs content).
"""

from __future__ import annotations

import base64
import binascii
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.config import Settings
from app.errors import (
    AttachmentMediaTypeMismatchError,
    AttachmentsTotalTooLargeError,
    AttachmentTooLargeError,
    InvalidBase64Error,
    PdfTooManyPagesError,
    PdfUnreadableError,
    TooManyAttachmentsError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from app.schemas.chat import AttachmentIn

# --- mediaType allowlist per class (fixed in code; Q-020-1 governs extension) ---------------
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_DOCUMENT_TYPES = frozenset({"application/pdf"})
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})

# ADR-095: голосовые сообщения. Класс отдельный от `document`, потому что обрабатывается иначе —
# запись не доходит до языковой модели вовсе, её заменяет распознанный текст.
# ПУБЛИЧНЫЙ: тот же набор — это набор ВХОДА кадра `utterance.begin` голосового режима
# (ADR-104 §4, «набор ADR-095 §2»). Второго перечня форматов записи не заводится: разойдясь, они
# дали бы поверхность, где вложение принимается, а живая реплика того же формата — нет.
AUDIO_MEDIA_TYPES = frozenset(
    {"audio/mp4", "audio/m4a", "audio/mpeg", "audio/wav", "audio/webm", "audio/ogg"}
)

_ALLOWLIST: dict[str, frozenset[str]] = {
    "image": _IMAGE_TYPES,
    "document": _DOCUMENT_TYPES,
    "text": _TEXT_TYPES,
    "audio": AUDIO_MEDIA_TYPES,
}

# Magic-byte signatures for image/PDF classes. WEBP is "RIFF"...."WEBP" (offset 8).
_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
}


# Provider identifiers for the per-provider rendering (ADR-033 §5). Passed ONLY by a client, and
# only its own (ADR-105 §A2.3).
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"

# Neutral part classes (ADR-105 §A2.1). ``audio`` never reaches block assembly (ADR-095).
KIND_IMAGE = "image"
KIND_DOCUMENT = "document"
KIND_TEXT = "text"


class UnrenderableAttachmentError(Exception):
    """A neutral part the client cannot render (ADR-105 §A2.4).

    Raised BEFORE the upstream call instead of dropping the part. It is a defect of OUR code (a
    class accepted by ``prepare_attachments`` must render on every client, §A2.5), not a user
    error and not an upstream failure — so it is neither a 422 nor a reason to fail over.
    """


@dataclass(frozen=True)
class AttachmentPart:
    """One provider-NEUTRAL attachment part (ADR-105 §A2.1).

    - ``kind`` ∈ {``image``, ``document``, ``text``};
    - ``media_type`` / ``filename`` — as validated;
    - ``data`` — base64 (``image`` / ``document``); the same string object the request carried, so
      rendering references it rather than copying it;
    - ``text`` — the ready text of a ``text`` part (the fenced request file, or the truncated
      extracted text of a workspace knowledge file).
    """

    kind: str
    media_type: str
    filename: str
    data: str | None = None
    text: str | None = None


@dataclass(frozen=True)
class ImageAttachmentRef:
    """In-memory image attachment for media image-to-image bridging (not persisted).

    Chat attachments are vision input (ADR-020); fal generation needs https URLs (ADR-062). The
    orchestrator keeps these refs for the CURRENT turn's tool-loop, uploads them to fal when a
    media tool runs without an explicit reference, then drops them — raw base64 never hits
    ``chat_steps``.
    """

    media_type: str
    filename: str
    data: str  # base64


@dataclass(frozen=True)
class PreparedAttachments:
    """Result of validating a request's attachments (ADR-020 / ADR-105 §A2).

    - parts: NEUTRAL attachment parts for the FIRST provider call of the turn only — full base64
      in memory, never persisted. The client that sends the call renders them in its own form
      (``render_attachment_blocks``); nothing before the client is provider-specific.
    - placeholders: light text blocks persisted in chat_steps.payload INSTEAD of base64
      (storage invariant: raw base64 is never stored; provider-agnostic).
    - images: request image attachments for media tools (same-turn only; never persisted).
    """

    parts: list[AttachmentPart]
    placeholders: list[dict[str, str]]
    images: list[ImageAttachmentRef] = field(default_factory=list)


def _max_bytes_for(attachment_type: str, settings: Settings) -> int:
    if attachment_type == "document":
        return settings.attachment_max_bytes_document
    if attachment_type == "audio":
        # ADR-095: свой потолок. Минута речи в m4a — около мегабайта, и картиночные 20 МиБ
        # пропустили бы получасовую запись: распознавание молотило бы её дольше, чем живёт ход.
        return settings.attachment_max_bytes_audio
    # image and text share the image ceiling; text files are small inline inputs.
    return settings.attachment_max_bytes_image


def _decoded_len_from_base64(data: str) -> int:
    """Upper bound of the decoded byte length from a base64 string, BEFORE decoding.

    base64 encodes 3 bytes per 4 chars; the decoded size is (len/4)*3 minus padding. This is
    used to reject oversized payloads without ever allocating the decoded buffer (anti DoS).
    """
    stripped = data.strip()
    n = len(stripped)
    if n == 0:
        return 0
    padding = stripped.count("=", max(0, n - 2))
    return (n // 4) * 3 - padding


def _decode_base64(data: str) -> bytes:
    try:
        # validate=True rejects non-alphabet characters (truncated/garbage -> 422, not 500).
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidBase64Error("attachment data is not valid base64") from exc


def _check_magic_bytes(media_type: str, decoded: bytes) -> None:
    if media_type == "image/webp":
        # RIFF container with a WEBP fourcc at offset 8.
        if not (decoded[:4] == b"RIFF" and decoded[8:12] == b"WEBP"):
            raise AttachmentMediaTypeMismatchError(
                "attachment content does not match declared mediaType"
            )
        return
    prefixes = _MAGIC_PREFIXES.get(media_type)
    if prefixes is None:  # pragma: no cover - allowlist guarantees a known image/pdf type here
        return
    if not any(decoded.startswith(prefix) for prefix in prefixes):
        raise AttachmentMediaTypeMismatchError(
            "attachment content does not match declared mediaType"
        )


def _decode_text(media_type: str, decoded: bytes) -> str:
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AttachmentMediaTypeMismatchError("text attachment is not valid UTF-8") from exc
    if media_type == "application/json":
        try:
            json.loads(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise AttachmentMediaTypeMismatchError(
                "attachment content does not match declared mediaType"
            ) from exc
    return text


def _check_pdf_pages(decoded: bytes, settings: Settings) -> None:
    """Guard PDF page count (anti decompression/structure bomb) via pypdf — no full render.

    A malformed or password-protected PDF is rejected as 422 (suspicious structure).
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(decoded))
        if reader.is_encrypted:
            raise PdfUnreadableError("password-protected PDF is not accepted")
        pages = len(reader.pages)
    except ValidationFailedError:
        raise
    except (PdfReadError, ValueError, OSError) as exc:
        raise PdfUnreadableError("PDF could not be parsed") from exc
    if pages > settings.attachment_pdf_max_pages:
        raise PdfTooManyPagesError("PDF exceeds the maximum allowed number of pages")


def _placeholder(att: AttachmentIn, decoded_size: int) -> dict[str, str]:
    name = att.filename or "file"
    return {
        "type": "text",
        "text": (
            f'[attachment: {att.mediaType} "{name}", {decoded_size}B '
            f"— отправлено в первом обращении к модели]"
        ),
    }


def _text_part_text(att: AttachmentIn, text: str | None) -> str:
    # text: inline the decoded UTF-8 with an explicit filename and a fenced code block. Same
    # representation for every provider (a plain text block).
    name = att.filename or "file"
    body = text if text is not None else ""
    return f"{name}\n```\n{body}\n```"


def _part_for(att: AttachmentIn, text: str | None) -> AttachmentPart:
    """The neutral part of one validated attachment (ADR-105 §A2.1)."""
    filename = att.filename or "file"
    if att.type == KIND_IMAGE:
        return AttachmentPart(
            kind=KIND_IMAGE, media_type=att.mediaType, filename=filename, data=att.data
        )
    if att.type == KIND_DOCUMENT:
        return AttachmentPart(
            kind=KIND_DOCUMENT, media_type=att.mediaType, filename=filename, data=att.data
        )
    return AttachmentPart(
        kind=KIND_TEXT,
        media_type=att.mediaType,
        filename=filename,
        text=_text_part_text(att, text),
    )


def _required_data(part: AttachmentPart) -> str:
    if not isinstance(part.data, str) or not part.data:
        raise UnrenderableAttachmentError(f"{part.kind} part carries no base64 data")
    return part.data


def _required_text(part: AttachmentPart) -> str:
    if not isinstance(part.text, str):
        raise UnrenderableAttachmentError("text part carries no text")
    return part.text


def _anthropic_block(part: AttachmentPart) -> dict[str, object]:
    if part.kind == KIND_IMAGE:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part.media_type,
                "data": _required_data(part),
            },
        }
    if part.kind == KIND_DOCUMENT:
        # TD-016: anthropic 0.39.0 has no DocumentBlockParam — emit the raw wire-format dict.
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": part.media_type,
                "data": _required_data(part),
            },
        }
    if part.kind == KIND_TEXT:
        return {"type": "text", "text": _required_text(part)}
    raise UnrenderableAttachmentError(f"attachment kind {part.kind!r} is not renderable")


def _openai_block(part: AttachmentPart) -> dict[str, object]:
    """OpenAI Chat Completions content part (ADR-033 §5, ADR-041).

    image → ``image_url`` with a base64 data-URI; text → text block; document (PDF) → native
    Chat Completions ``file`` content-part (ADR-041 §1, closes TD-023 — the text-extraction
    fallback of §2 is not taken). The Responses client renders through this function too and then
    maps the parts to ``input_*`` with its own ``_responses_content_part``.
    """
    if part.kind == KIND_IMAGE:
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{part.media_type};base64,{_required_data(part)}"},
        }
    if part.kind == KIND_DOCUMENT:
        return {
            "type": "file",
            "file": {
                "filename": part.filename,
                "file_data": f"data:{part.media_type};base64,{_required_data(part)}",
            },
        }
    if part.kind == KIND_TEXT:
        return {"type": "text", "text": _required_text(part)}
    raise UnrenderableAttachmentError(f"attachment kind {part.kind!r} is not renderable")


def render_attachment_blocks(
    parts: Sequence[AttachmentPart], provider: str
) -> list[dict[str, object]]:
    """Render neutral parts into the wire blocks of ``provider`` (ADR-105 §A2.3/§A2.4).

    Called ONLY by a client, with its OWN provider. Total: exactly one block per part, in order;
    a part that cannot be rendered raises ``UnrenderableAttachmentError`` before any upstream
    call — a part is never skipped. An unknown provider raises the same error.
    """
    if provider == PROVIDER_ANTHROPIC:
        return [_anthropic_block(part) for part in parts]
    if provider == PROVIDER_OPENAI:
        return [_openai_block(part) for part in parts]
    raise UnrenderableAttachmentError(f"unknown provider for attachment rendering: {provider!r}")


def prepare_attachments(
    attachments: list[AttachmentIn],
    settings: Settings,
) -> PreparedAttachments:
    """Validate inline attachments and build neutral parts + storage placeholders.

    Enforces (ADR-020 / 05-security.md): mediaType allowlist, size/count limits BEFORE decode,
    base64 validity, magic-byte/UTF-8/JSON consistency, PDF page-guard. Raises
    ValidationFailedError (-> 422) on any violation, BEFORE the user step is written. Produces only
    NEUTRAL parts (ADR-105 §A2.2): the provider form is chosen later by the client that sends the
    call. Never logs attachment content.
    """
    if len(attachments) > settings.attachment_max_count:
        raise TooManyAttachmentsError("too many attachments")

    parts: list[AttachmentPart] = []
    placeholders: list[dict[str, str]] = []
    images: list[ImageAttachmentRef] = []
    total_decoded = 0

    for att in attachments:
        allowed = _ALLOWLIST.get(att.type)
        # type is constrained by the schema Literal; defensive guard keeps mypy/logic explicit.
        if allowed is None or att.mediaType not in allowed:  # pragma: no branch
            raise UnsupportedMediaTypeError(f"unsupported_media_type: {att.mediaType}")

        # Limits BEFORE base64 decode (anti memory-DoS): bound decoded size from the b64 length.
        approx_decoded = _decoded_len_from_base64(att.data)
        if approx_decoded > _max_bytes_for(att.type, settings):
            raise AttachmentTooLargeError("attachment exceeds the maximum size")
        total_decoded += approx_decoded
        if total_decoded > settings.attachment_total_bytes:
            raise AttachmentsTotalTooLargeError("attachments exceed the total size limit")

        decoded = _decode_base64(att.data)
        text: str | None = None
        if att.type == "audio":
            # ADR-095: сюда аудио доходить не должно — его распознают и заменяют текстом ДО
            # сборки блоков. Явный отказ вместо падения ниже: без него запись попала бы в
            # текстовую ветку и умерла на декодировании UTF-8, а сообщение указывало бы на
            # «битый текстовый файл» — то есть увело бы разбор в сторону от настоящей причины.
            raise UnsupportedMediaTypeError("audio must be transcribed before block assembly")
        if att.type == "image":
            _check_magic_bytes(att.mediaType, decoded)
        elif att.type == "document":
            _check_magic_bytes(att.mediaType, decoded)
            _check_pdf_pages(decoded, settings)
        else:  # text
            text = _decode_text(att.mediaType, decoded)

        # Neutral part (AFTER shared validation). The provider form is rendered by the client.
        parts.append(_part_for(att, text))
        placeholders.append(_placeholder(att, len(decoded)))
        if att.type == "image":
            images.append(
                ImageAttachmentRef(
                    media_type=att.mediaType,
                    filename=att.filename or "image.jpg",
                    data=att.data,
                )
            )

    return PreparedAttachments(parts=parts, placeholders=placeholders, images=images)
