# Тестове (ПачоЛогистик)

Автоматични тестове за носещата логика на приложението. Част от предпазната
мрежа (Фаза 0), която позволява безопасен рефакторинг в следващите фази.

## Стартиране

```bash
pip install -r requirements-dev.txt
pytest
```

## Изолация

Тестовете **никога** не пипат реалната база данни или `pacho_config.json`.
Всеки тест, който има нужда от база, получава чисто нова временна SQLite база
във временна папка (виж fixture-ите в `conftest.py`: `db_module`, `con`,
`tmp_db_path`).

## Какво се покрива засега

- `test_numbering.py` — номериране на документи (формат, поредност, годишен
  ресет, уникален баркод, непознат тип) + стрес тест за едновременност
  (H6: 12 нишки × реални SQLite връзки, нула дублирани номера).
- `test_barcode.py` — Code128-B SVG генератор (структура, контролна сума,
  отхвърляне на не-ASCII, responsive режим).
- `test_config.py` — bootstrap конфигурация (defaults, save/load, повреден
  JSON, разрешаване на пътя до базата, шифроване на gh_token — H4).
- `test_updater.py` — семантично сравнение на версии, парсване и проверка
  на SHA256SUMS.txt манифеста (H3).
- `test_auth.py` — хеширане на пароли, роли, must_change_password (C1,
  вкл. симулация на ъпгрейд от стара база без тази колона).
- `test_db.py` — настройки, пунктове за разтоварване, потребителски теми.
- `test_migrations.py` — рамката за миграции (PRAGMA user_version,
  идемпотентност, _ensure_column) — M1.
- `test_backup_sync.py` — откриване на конфликт при GitHub синхронизация
  (RemoteChangedError, force override, базова линия при pull) — M2.
- `test_jsonutil.py` — екраниране на JSON за вграждане в `<script>` (H2).
- `test_login_guard.py` — заключване след повторни неуспешни опити (H5).
- `test_secrets_store.py` — шифроване на токена „в покой“ (H4).
- `test_web_routes.py` — характеризиращ пакет през реален Flask test client
  (`appcore.create_app()` + всички `routes_*` модули, виж fixture
  `flask_app`/`client`/`admin_client`/`employee_client` в `conftest.py`,
  добавени във Фаза 3): вход/CSRF/задължителна смяна на парола, всичките
  5 документни потока (издаване/преглед/преглед на документ/редакция/
  Excel износ/изтриване), палетни bulk потоци, клиенти, настройки, админ
  панел (M7 — `client_delete` изисква admin), M9 (`MAX_CONTENT_LENGTH`),
  и достъпност (Фаза 4 — `role="alert"`, свързани `label`/`input` двойки,
  липса на твърдо кодиран `#5c6d80`).

## Обобщение по фаза

| Фаза | Тестове | Общо |
|---|---|---|
| 0–2 (сигурност, цялост на данни) | numbering/barcode/config/updater/auth/db/migrations/backup_sync/jsonutil/login_guard/secrets_store | 90 |
| 3 (структурен рефакторинг) | `test_web_routes.py` (fixtures: `flask_app`, `client`, `admin_client`, `employee_client`) | +28 |
| 4 (фронтенд/достъпност) | M7/M9/достъпност регресионни тестове в `test_web_routes.py` | +8 |
| **Общо** | | **126** |
