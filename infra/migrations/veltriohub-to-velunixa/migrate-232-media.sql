-- Перенос истории генераций veltriohub (232) в velunixa: картинки и видео.
--
-- Ответы чата (тип openai_response) сюда НЕ входят — они уже перенесены как сообщения.
--
-- ТЕКСТА ЗАПРОСА НЕТ. В таблице генераций 232 колонки prompt не существует вовсе, поэтому
-- поле остаётся пустым — это согласовано с владельцем. Пользователь увидит результат, модель
-- и дату, но не увидит, что просил.
--
-- НЕЗАВЕРШЁННЫЕ ЗАДАЧИ переносятся как неуспешные. Наш согласователь опрашивает провайдера по
-- всем задачам в состоянии queued/running (для того и существует частичный индекс
-- ix_media_jobs_non_terminal). Задача, висевшая в очереди умершего сервиса, не завершится
-- никогда — оставить её незавершённой значит обречь согласователь на вечный опрос впустую.
--
-- Запускать ПОСЛЕ переноса пользователей.

BEGIN;

CREATE TEMP TABLE umap_m ON COMMIT DROP AS
SELECT s.id AS src_user_id, d.user_id AS dst_user_id
FROM src232.users s
JOIN public.auth_devices d ON upper(d.device_id) = upper(s.apphud_id);

CREATE INDEX ON umap_m (src_user_id);

INSERT INTO public.media_jobs (
  id, user_id, model_id, kind, fal_endpoint, fal_request_id, status_url, response_url,
  status, prompt, credits_charged, credits_refunded, result, error, created_at, updated_at
)
SELECT
  g.id,                                   -- идентификатор источника уже uuid: повторный запуск не задвоит
  u.dst_user_id,
  COALESCE(NULLIF(g.model, ''), 'unknown'),
  g.type,
  '',                                     -- endpoint провайдера в источнике не сохранялся
  COALESCE(g.external_id, ''),
  '', '',                                 -- ссылки на статус и ответ бессмысленны для завершённых задач
  CASE g.status::text
       WHEN 'finished' THEN 'completed'
       WHEN 'error'    THEN 'failed'
       ELSE 'failed'                      -- queued/started: см. врезку выше
  END,
  '',                                     -- текста запроса в источнике нет
  COALESCE(round(g.tokens_cost * 20), 0)::int,   -- тот же курс 20, что и у балансов
  g.refunded_at IS NOT NULL,
  CASE WHEN g.result IS NOT NULL AND g.result <> ''
       THEN jsonb_build_object('assets', jsonb_build_array(jsonb_build_object('url', g.result)))
       ELSE NULL END,
  g.error,
  g.created_at,
  COALESCE(g.completed_at, g.updated_at, g.created_at)
FROM src232.generations g
JOIN umap_m u ON u.src_user_id = g.user_id
WHERE g.type IN ('image', 'video')
  AND NOT EXISTS (SELECT 1 FROM public.media_jobs mj WHERE mj.id = g.id);

SELECT
  (SELECT count(*) FROM public.media_jobs)                                        AS vsego_teper,
  (SELECT count(*) FROM src232.generations g JOIN umap_m u ON u.src_user_id = g.user_id
     WHERE g.type IN ('image','video'))                                           AS bylo_v_istochnike;

COMMIT;
