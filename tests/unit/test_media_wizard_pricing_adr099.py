"""Unit: мастер медиа показывает РОВНО ту цену, которую спишет (ADR-099 §4.4, §5.1).

Форма дефекта, ради которой файл написан: **цена объявлена по одной величине, а списана по
другой.** Мастер выбора параметров в чате рисует подпись каждой опции («1K · N cr.», «from N
cr.»), а списание идёт через ``media_run_price``; пока подпись считалась реестровой формулой, не
знающей операторского тарифа, первая же правка ячейки разводила показанное и списанное — оператор
менял цену, пользователь видел старую и платил новую.

⚠️ **Кейсы обязаны падать при возврате подписи к реестровой формуле** (``run_price`` /
``image_unit_credits`` / ``base_credits_for``) — именно это и проверяется мутацией, а не
«числа сошлись». Поэтому ни одно ожидаемое значение здесь не копируется константой: оно
ВЫЧИСЛЯЕТСЯ тем же резолвером, которым пойдёт списание, а расхождение создаётся заведомо
отличным от реестра оверлеем — на дефолтах кейс прошёл бы и при полностью развязанных величинах.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

import pytest

from app.chat.media_choices import (
    STEP_DURATION,
    STEP_GENERATE_AUDIO,
    STEP_MODEL,
    STEP_RESOLUTION,
    build_step_questions,
)
from app.config import Settings
from app.instance_config.media_pricing import media_base_credits
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    TariffOverlay,
)
from app.instance_config.tariffs import (
    base_credits_for,
    has_audio_dimension,
    media_run_price,
    photo_cells,
    video_cells,
)
from app.media_generation.catalog import (
    KIND_IMAGE,
    KIND_VIDEO,
    image_unit_credits,
    models_of_kind,
    run_price,
)

_NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.UTC)
_IMAGE_MODELS = models_of_kind(KIND_IMAGE)
_VIDEO_MODELS = models_of_kind(KIND_VIDEO)


def _settings() -> Settings:
    return Settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-openai-test",
        FAL_API_KEY="fal-test-key",
    )


def _snapshot(prices: dict[str, int]) -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        tariffs={
            tariff_id: TariffOverlay(tariff_id=tariff_id, tokens=value, updated_at=_NOW)
            for tariff_id, value in prices.items()
        }
    )


def _priced_overlay(model: Any) -> InstanceConfigSnapshot:
    """Оверлей на КАЖДУЮ ячейку модели, заведомо не равный реестровой цене.

    Числа выбраны далеко от реестровых и попарно различными: совпадение оверлея с реестром хоть
    в одной ячейке сделало бы кейс слепым ровно к тому дефекту, ради которого он написан.
    """
    cells = photo_cells(model) if model.kind == KIND_IMAGE else video_cells(model)
    return _snapshot({cell.tariff_id: 700 + 13 * index for index, cell in enumerate(cells)})


def _label_credits(label: str) -> int:
    """Число, которое пользователь ВИДИТ в подписи опции.

    Подпись читается отдельно от поля ``credits``: клиент показывает именно её, и расхождение
    между ними означало бы, что одно из двух чисел мёртвое.
    """
    match = re.search(r"(\d+) cr\.", label)
    assert match is not None, label
    return int(match.group(1))


def _step(kind: str, answers: dict[str, str], snapshot: InstanceConfigSnapshot) -> tuple[str, Any]:
    built = build_step_questions(
        kind=kind,
        answers=answers,
        source_job_id=None,
        settings=_settings(),
        snapshot=snapshot,
    )
    assert built is not None
    step, questions = built
    assert len(questions) == 1
    return step, {option["value"]: option for option in questions[0]["options"]}


# ====== 1. подпись опции = цена запуска с этим ответом =======================================
@pytest.mark.parametrize("model", _IMAGE_MODELS, ids=lambda m: m.id)
def test_the_photo_wizard_quotes_each_resolution_at_the_price_the_submit_will_charge(
    model: Any,
) -> None:
    """producer: ячейка `photo:*` в снимке → consumer: подпись шага `resolution` мастера.

    Ожидаемое ВЫЧИСЛЯЕТСЯ ``media_run_price`` — тем же резолвером, которым пойдёт списание. Кейс
    падает, если подпись вернётся к ``image_unit_credits``: оверлей заведомо не равен реестру,
    поэтому расхождение видно на КАЖДОМ разрешении, а не только на правленом.
    """
    snapshot = _priced_overlay(model)
    settings = _settings()

    step, options = _step(KIND_IMAGE, {STEP_MODEL: model.id}, snapshot)

    assert step == STEP_RESOLUTION
    assert options, model.id
    for value, option in options.items():
        expected = media_run_price(
            model=model, resolution=value, settings=settings, snapshot=snapshot
        )
        assert option["credits"] == expected, (model.id, value)
        assert _label_credits(option["label"]) == expected, (model.id, value)
        # Предусловие кейса: реестровая формула даёт ДРУГОЕ число, иначе он слеп.
        assert expected != image_unit_credits(
            model, value, base_credits=base_credits_for(model, settings)
        ), (model.id, value)


@pytest.mark.parametrize("model", _VIDEO_MODELS, ids=lambda m: m.id)
def test_the_video_wizard_quotes_duration_and_audio_at_the_price_the_submit_will_charge(
    model: Any,
) -> None:
    """Те же координаты, что у ячейки: длительность, разрешение и звук — по отдельному шагу.

    Видео проверяется ОТДЕЛЬНО от фото не для симметрии: у него другая единица (`generation`
    против `image`) и другой набор координат, и общий кейс не показал бы, что оверлей накрывает
    ту же комбинацию, по которой пойдёт списание.
    """
    snapshot = _priced_overlay(model)
    settings = _settings()
    answers: dict[str, str] = {STEP_MODEL: model.id}
    checked: list[str] = []

    while True:
        built = build_step_questions(
            kind=KIND_VIDEO,
            answers=answers,
            source_job_id=None,
            settings=settings,
            snapshot=snapshot,
        )
        if built is None:
            break
        step, questions = built
        options = {option["value"]: option for option in questions[0]["options"]}
        if step in (STEP_RESOLUTION, STEP_DURATION, STEP_GENERATE_AUDIO):
            checked.append(step)
            for value, option in options.items():
                merged = {**answers, step: value}
                audio = (
                    merged[STEP_GENERATE_AUDIO] == "true" if STEP_GENERATE_AUDIO in merged else None
                )
                expected = media_run_price(
                    model=model,
                    resolution=merged.get(STEP_RESOLUTION),
                    duration=merged.get(STEP_DURATION),
                    generate_audio=audio,
                    settings=settings,
                    snapshot=snapshot,
                )
                assert option["credits"] == expected, (model.id, step, value)
                assert _label_credits(option["label"]) == expected, (model.id, step, value)
        # Первый вариант шага — детерминированный ход по мастеру до конца анкеты.
        answers[step] = next(iter(options))

    assert STEP_DURATION in checked, model.id
    if has_audio_dimension(model):
        assert STEP_GENERATE_AUDIO in checked, model.id
    if model.resolution_multipliers:
        assert STEP_RESOLUTION in checked, model.id


# ====== 2. карточка выбора модели = базовая цена показа =====================================
@pytest.mark.parametrize("kind", [KIND_IMAGE, KIND_VIDEO])
def test_the_model_card_from_price_equals_the_base_credits_of_the_same_cells(kind: str) -> None:
    """producer: ячейки модели → consumer: подпись «from N cr.» карточки выбора модели.

    ⚠️ Отдельный кейс, а не следствие предыдущих: карточка модели рисуется ДО того, как выбрана
    хоть одна координата, и потому берёт величину из другого места (``media_base_credits``, не
    ``media_run_price``). Именно она первой и разошлась бы с ячейками — реестровая база
    ``base_credits_for`` операторский тариф не читает вовсе.
    """
    settings = _settings()
    snapshot = _snapshot(
        {
            tariff_id: overlay.tokens
            for model in models_of_kind(kind)
            for tariff_id, overlay in _priced_overlay(model).tariffs.items()
        }
    )

    step, options = _step(kind, {}, snapshot)

    assert step == STEP_MODEL
    assert options
    for model in models_of_kind(kind):
        expected = media_base_credits(model, settings=settings, snapshot=snapshot)
        option = options[model.id]
        assert option["credits"] == expected, model.id
        assert _label_credits(option["label"]) == expected, model.id
        # Реестровая база даёт другое число — значит кейс различает две величины.
        assert expected != base_credits_for(model, settings), model.id


# ====== 3. пустой оверлей — сегодняшний день бит-в-бит =======================================
@pytest.mark.parametrize("model", [*_IMAGE_MODELS, *_VIDEO_MODELS], ids=lambda m: m.id)
def test_on_an_empty_overlay_the_wizard_shows_exactly_what_it_showed_before_the_wave(
    model: Any,
) -> None:
    """Ни одна подпись и ни одна базовая цена не сдвинулись, пока оператор ничего не правил.

    Ожидаемое считается РЕЕСТРОВОЙ формулой (``run_price`` / ``image_unit_credits`` /
    ``base_credits_for``) — той самой, которую волна заменила. Константа в фикстуре кодировала бы
    то же допущение, что и новый код, и кейс прошёл бы при разошедшемся дефолте.
    """
    settings = _settings()
    kind = model.kind
    base = base_credits_for(model, settings)

    _step_id, model_options = _step(kind, {}, EMPTY_SNAPSHOT)
    assert model_options[model.id]["credits"] == base
    assert _label_credits(model_options[model.id]["label"]) == base

    answers: dict[str, str] = {STEP_MODEL: model.id}
    while True:
        built = build_step_questions(
            kind=kind,
            answers=answers,
            source_job_id=None,
            settings=settings,
            snapshot=EMPTY_SNAPSHOT,
        )
        if built is None:
            break
        step, questions = built
        options = {option["value"]: option for option in questions[0]["options"]}
        for value, option in options.items():
            merged = {**answers, step: value}
            if step == STEP_RESOLUTION and kind == KIND_IMAGE:
                expected = image_unit_credits(model, value, base_credits=base)
            elif step in (STEP_RESOLUTION, STEP_DURATION, STEP_GENERATE_AUDIO):
                expected = run_price(
                    model=model,
                    base_credits=base,
                    duration=merged.get(STEP_DURATION),
                    resolution=merged.get(STEP_RESOLUTION),
                    generate_audio=(
                        merged[STEP_GENERATE_AUDIO] == "true"
                        if STEP_GENERATE_AUDIO in merged
                        else None
                    ),
                )
            else:
                continue
            assert option["credits"] == expected, (model.id, step, value)
            assert _label_credits(option["label"]) == expected, (model.id, step, value)
        answers[step] = next(iter(options))
