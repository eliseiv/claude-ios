-- Перенос пользователей veltriohub (232) в velunixa.
--
-- ОСНОВАНИЕ СОПОСТАВЛЕНИЯ. iOS-разработчик подтвердил: значение, уходящее в `deviceId`, и
-- `apphud_id` в 232 — ОДНО И ТО ЖЕ (сделано намеренно ради этой миграции). Поэтому связь
-- строится по нему, регистр приводится к верхнему: приложение шлёт UUID().uuidString.
--
-- КУРС 20. В 232 ответ чата стоит 0.05 токена, у нас — 1 кредит. Умножение на 20 сохраняет
-- покупательную способность ТОЧНО: проверено, что все 73 995 балансов кратны 0.05, исключений
-- нет ни одного. Простое копирование числа отняло бы у пользователей 95% возможностей.
--
-- avatar_tokens НЕ переносятся: за всё время 2 900 начислений и НОЛЬ списаний — функции,
-- которая их тратит, в 232 не существует.

\set BUNDLE 'com.arm.232C1aude'

BEGIN;

CREATE TEMP TABLE mig ON COMMIT DROP AS
SELECT
  s.id                                  AS src_id,
  upper(s.apphud_id)                    AS device_id,
  round(s.tokens * 20)::bigint          AS balance,
  s.created_at                          AS created_at,
  s.subscription_active                 AS sub_active,
  s.subscription_expires_at             AS sub_expires,
  d.user_id                             AS existing_user_id,
  EXISTS (SELECT 1 FROM src232.generations g WHERE g.user_id = s.id) AS used_service
FROM src232.users s
LEFT JOIN public.auth_devices d ON upper(d.device_id) = upper(s.apphud_id)
WHERE s.app_bundle = :'BUNDLE'
  -- ЗАЩИТА ОТ ПОВТОРНОГО ЗАПУСКА. Уникального ограничения на ключ идемпотентности в таблице
  -- нет (проверено), поэтому второй прогон начислил бы кредиты ДВАЖДЫ. Пользователь, уже
  -- перенесённый, отсекается по следу в журнале — по нему же видно, что перенос состоялся.
  AND NOT EXISTS (
    SELECT 1 FROM public.ledger_transactions l
    WHERE l.idempotency_key = 'migration-232:' || s.id::text
  );

-- Новые пользователи: те, кого на этом инстансе ещё нет.
CREATE TEMP TABLE newu ON COMMIT DROP AS
SELECT *, gen_random_uuid() AS new_user_id FROM mig WHERE existing_user_id IS NULL;

INSERT INTO public.users (id, created_at, trial_used)
SELECT new_user_id, created_at, used_service FROM newu;

INSERT INTO public.auth_devices (device_id, user_id, created_at, last_seen_at)
SELECT device_id, new_user_id, created_at, created_at FROM newu;

INSERT INTO public.wallets (user_id, balance, updated_at)
SELECT new_user_id, balance, now() FROM newu;

-- Подписка активна только если она активна В ИСТОЧНИКЕ и срок ещё не истёк. Перенос
-- просроченной как активной выдал бы доступ, за который не платили.
INSERT INTO public.subscriptions (user_id, status, expires_at, updated_at)
SELECT new_user_id,
       CASE WHEN sub_active AND (sub_expires IS NULL OR sub_expires > now())
            THEN 'active'::subscription_status
            WHEN sub_expires IS NOT NULL THEN 'expired'::subscription_status
            ELSE 'none'::subscription_status END,
       sub_expires, now()
FROM newu;

-- Происхождение баланса фиксируется в журнале: иначе кредиты появляются ниоткуда и
-- расследовать спорное списание будет нечем.
INSERT INTO public.ledger_transactions (user_id, type, amount, meta, idempotency_key, created_at)
SELECT new_user_id, 'credit'::ledger_tx_type, balance,
       jsonb_build_object('source', 'migration-232', 'srcUserId', src_id, 'rate', 20),
       'migration-232:' || src_id::text, now()
FROM newu WHERE balance > 0;

-- Уже зарегистрированные на этом инстансе: доначисляем, а не создаём заново.
--
-- ВАЖНО: строка кошелька создаётся ЛЕНИВО, при первом использовании. У пользователя, который
-- зарегистрировался, но ничего не потратил, её ещё нет — и обычный UPDATE прошёл бы мимо,
-- потеряв перенесённый баланс МОЛЧА. Поймано вхолостую: обновилась 1 строка при 2 начислениях.
INSERT INTO public.wallets (user_id, balance, updated_at)
SELECT m.existing_user_id, m.balance, now()
FROM mig m
WHERE m.existing_user_id IS NOT NULL AND m.balance > 0
ON CONFLICT (user_id) DO UPDATE
SET balance = public.wallets.balance + EXCLUDED.balance, updated_at = now();

INSERT INTO public.ledger_transactions (user_id, type, amount, meta, idempotency_key, created_at)
SELECT m.existing_user_id, 'credit'::ledger_tx_type, m.balance,
       jsonb_build_object('source', 'migration-232-merge', 'srcUserId', m.src_id, 'rate', 20),
       'migration-232:' || m.src_id::text, now()
FROM mig m WHERE m.existing_user_id IS NOT NULL AND m.balance > 0;

SELECT
  (SELECT count(*) FROM newu)                                   AS sozdano,
  (SELECT count(*) FROM mig WHERE existing_user_id IS NOT NULL) AS obedineno,
  (SELECT sum(balance) FROM mig)                                AS vsego_kreditov,
  (SELECT count(*) FROM newu WHERE sub_active
      AND (sub_expires IS NULL OR sub_expires > now()))         AS aktivnyh_podpisok;

COMMIT;
