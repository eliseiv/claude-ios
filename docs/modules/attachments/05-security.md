# Attachments — Security

> ⚠️ **Этот файл описывает ОТЛОЖЕННУЮ двухшаговую модель ([TD-015](../../100-known-tech-debt.md)), а не работающий MVP.** Действующие лимиты и коды ошибок inline-вложений — [chat-orchestrator/02-api-contracts.md §Лимиты вложений](../chat-orchestrator/02-api-contracts.md#лимиты-вложений-adr-089) и [05-security.md §Мультимодальные вложения](../../05-security.md#мультимодальные-вложения--валидация-и-модель-угроз-adr-020). В частности, действующий лимит `document` — **8 MB** (`ATTACHMENT_MAX_BYTES_DOCUMENT`), а не 10 MB из проектных значений ниже.

## Size-лимиты (transport)
- image ≤ 5 MB, document ≤ 10 MB (**проектные значения отложенной модели**, конфигурируемо, [Q-014-2](../../99-open-questions.md); действующие значения MVP — по ссылке выше). Multipart transport-guard на gateway → `413`. Это **отдельный** лимит от JSON `≤512KB` (бинарь не идёт в JSON, [ADR-014](../../adr/ADR-014-multimodal-attachments.md)).
- ≤ 10 вложений на сообщение (проверяется orchestrator при резолве `attachments[]`).

## Media_type allowlist ([Q-014-1](../../99-open-questions.md))
- Старт: `image/jpeg`, `image/png`, `image/gif`, `image/webp`, `application/pdf`, `text/plain`.
- Определяется по **magic bytes содержимого**, не по расширению/заголовку клиента (анти-подмена типа). Вне allowlist → `422`.

## Изоляция и доступ
- Вложение принадлежит `user_id == sub`. Резолв чужого вложения в `/chat/run` → `403`/`404`. GET/DELETE чужого → `404`.
- Сырой бинарь не отдаётся внешним endpoint'ом как загружаемый файл (нет публичной отдачи, в отличие от website-builder preview) — только метаданные. Снижает поверхность отдачи пользовательского контента.

## Логирование
- Байты вложения и `extracted_text` **не логируются**. Имя файла/`media_type`/`size` — допустимы в логах (не секрет).
- Redaction распространяется на содержимое; в audit/трейсы попадают только идентификаторы и метаданные.

## Модель угроз (дополнение к [05-security.md](../../05-security.md))
| Угроза | Митигирование |
|---|---|
| Подмена media_type клиентом (загрузка исполняемого как image) | Определение по magic bytes, строгий allowlist, нет публичной отдачи. |
| Раздувание БД крупными вложениями | Size-лимиты, число/сообщение, orphan-retention ([TD-010](../../100-known-tech-debt.md)). |
| Доступ к чужому вложению через id | Изоляция `user_id == sub` на резолве/GET/DELETE. |
| Утечка содержимого в логи | No-log байтов/extracted_text, redaction. |
| Обход JSON size-лимита через inline base64 | Бинарь идёт multipart-каналом, не JSON ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)). |
