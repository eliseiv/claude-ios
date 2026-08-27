-- Приведение типов блоков перенесённой истории к нашему словарю.
--
-- ЗАЧЕМ. 232 хранил содержимое в типах OpenAI Responses: `input_text` у пользователя,
-- `output_text` у модели. Наш код извлекает текст ТОЛЬКО из блоков с `type = "text"`
-- (chats/repository.py::_text_from_payload), поэтому перенесённые беседы отдавались клиенту
-- с пустым превью и без заголовка — история выглядела бы как список пустых строк.
--
-- Поймано живой проверкой через API: шаги на месте, текст на месте, превью null.
--
-- Наши собственные блоки уже имеют тип `text`, поэтому замена их не касается: значений
-- `input_text`/`output_text` в родной истории не бывает.

BEGIN;

UPDATE public.chat_steps st
SET payload = jsonb_set(
      st.payload,
      '{content}',
      (
        SELECT jsonb_agg(
                 CASE
                   WHEN e->>'type' IN ('input_text', 'output_text')
                     THEN jsonb_build_object('type', 'text', 'text', e->>'text')
                   -- Картинка: наш словарь использует image_url со вложенным объектом.
                   WHEN e->>'type' = 'input_image' AND e ? 'image_url'
                     THEN jsonb_build_object('type', 'image_url',
                                             'image_url', jsonb_build_object('url', e->>'image_url'))
                   -- Файл: отдельного блока у нас нет, показываем имя текстом — иначе
                   -- вложение исчезло бы из истории бесследно.
                   WHEN e->>'type' = 'input_file'
                     THEN jsonb_build_object('type', 'text',
                                             'text', COALESCE(e->>'filename', 'файл'))
                   ELSE e
                 END
                 ORDER BY ord
               )
        FROM jsonb_array_elements(st.payload->'content') WITH ORDINALITY AS t(e, ord)
      )
    )
WHERE st.payload->'content' @> '[{"type": "input_text"}]'
   OR st.payload->'content' @> '[{"type": "output_text"}]'
   OR st.payload->'content' @> '[{"type": "input_image"}]'
   OR st.payload->'content' @> '[{"type": "input_file"}]';

SELECT
  (SELECT count(*) FROM public.chat_steps st, LATERAL jsonb_array_elements(st.payload->'content') e
     WHERE e->>'type' = 'text')       AS blokov_text,
  (SELECT count(*) FROM public.chat_steps st, LATERAL jsonb_array_elements(st.payload->'content') e
     WHERE e->>'type' IN ('input_text','output_text')) AS ostalos_starih;

COMMIT;
