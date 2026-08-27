-- Перенос истории чатов veltriohub (232) в velunixa.
--
-- Формат содержимого СОВПАДАЕТ буквально: и там и здесь это массив частей вида
-- {"type": "text", "text": "..."}. Поэтому содержимое переносится как есть, без преобразования —
-- любое «улучшение» формата здесь означало бы риск разойтись с тем, что ждёт приложение.
--
-- Идентификатор беседы в 232 — ТЕКСТ, у нас UUID. Берём детерминированный UUID версии 5 от
-- текстового идентификатора: одна и та же беседа при повторном запуске получит тот же UUID,
-- то есть перенос можно повторять, не плодя дубликаты.
--
-- Запускать ПОСЛЕ переноса пользователей: сессии ссылаются на них внешним ключом.

BEGIN;

-- Соответствие пользователей: источник -> наш, по идентификатору устройства.
--
-- ПЕРВАЯ РЕДАКЦИЯ строила его по следу в журнале — и молча теряла историю всех, у кого баланс
-- нулевой: запись в журнал создаётся только при ненулевом начислении. Из 33 109 бесед
-- переносились 30 953. Поймано совместным холостым прогоном.
--
-- Устройство покрывает ВСЕХ: и перенесённых, и объединённых с уже существовавшими.
CREATE TEMP TABLE umap ON COMMIT DROP AS
SELECT
  s.id      AS src_user_id,
  d.user_id AS dst_user_id
FROM src232.users s
JOIN public.auth_devices d ON upper(d.device_id) = upper(s.apphud_id);

CREATE INDEX ON umap (src_user_id);

-- Беседы -> сессии.
CREATE TEMP TABLE smap ON COMMIT DROP AS
SELECT
  c.id                                                       AS src_conv_id,
  md5('veltriohub:' || c.id)::uuid     AS dst_session_id,
  u.dst_user_id,
  c.created_at,
  COALESCE(c.updated_at, c.created_at)                       AS updated_at
FROM src232.chat_conversations c
JOIN umap u ON u.src_user_id = c.user_id
WHERE NOT EXISTS (
  SELECT 1 FROM public.chat_sessions s
  WHERE s.id = md5('veltriohub:' || c.id)::uuid
);

CREATE INDEX ON smap (src_conv_id);

INSERT INTO public.chat_sessions (id, user_id, mode, created_at, updated_at, assistant_mode)
SELECT dst_session_id, dst_user_id, 'credits'::chat_mode, created_at, updated_at, 'chat'::assistant_mode
FROM smap;

-- Сообщения -> шаги. `message_step_id` у нас связывает ход пользователя с ответом модели;
-- в источнике такой связи нет, поэтому берём детерминированный идентификатор самого шага.
INSERT INTO public.chat_steps (id, session_id, message_step_id, role, payload, created_at)
SELECT
  md5('veltriohub-msg:' || m.conversation_id || ':' || m.seq::text)::uuid,
  s.dst_session_id,
  md5('veltriohub-msg:' || m.conversation_id || ':' || m.seq::text)::uuid,
  m.role::chat_role,
  CASE WHEN m.role = 'user'
       THEN jsonb_build_object('content', m.content, 'generationMode', 'general')
       ELSE jsonb_build_object('content', m.content) END,
  m.created_at
FROM src232.chat_messages m
JOIN smap s ON s.src_conv_id = m.conversation_id
ORDER BY m.conversation_id, m.seq;

SELECT
  (SELECT count(*) FROM smap)                                    AS sessij_sozdano,
  (SELECT count(*) FROM src232.chat_messages m JOIN smap s ON s.src_conv_id = m.conversation_id) AS soobshenij,
  (SELECT count(*) FROM umap)                                    AS polzovateley_v_sootvetstvii;

COMMIT;
