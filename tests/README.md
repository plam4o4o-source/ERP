# Тестове (PH Logistics)

Автоматични тестове за носещата логика на приложението. Част от предпазната
мрежа (Фаза 0), която позволява безопасен рефакторинг в следващите фази.

## Стартиране

```bash
pip install -r requirements-dev.txt
python -m pytest            # бързият пакет (без e2e)
python -m pytest -m e2e     # end-to-end с истински Chromium (Playwright)
```

## Конвенции

- **Нови тестове отиват във файл по функционалност** (`test_invoices.py`,
  `test_backup_local.py`, нов `test_<функционалност>.py`), **не** в нов датиран
  `test_audit_<дата>.py`. Съществуващите датирани файлове остават, но не се
  разширяват.
- Общите помощници са в `conftest.py` — не ги копирайте във всеки файл:
  `post_with_csrf`, `get_csrf_token`, `get_edit_doc_version`, `issue_cmr`
  (издава ЧМР и връща id), `read_source(*части_от_пътя)`, `app_js_source()`,
  `ROOT`.
- **Пароли:** autouse fixture-ът `_fast_password_hashing` подменя scrypt с
  евтин `pbkdf2:sha256:1` (scrypt беше основната цена на пакета). Тест, който
  трябва да види истинското хеширане, се маркира с
  `@pytest.mark.real_password_hash` (виж `test_password_hashing.py`).
- **e2e:** `live_server` (истински сървър + администратор `e2e_admin`),
  `page` (нов browser context върху ЕДИН Chromium за цялата сесия —
  `e2e_browser`), `e2e_context_factory` (допълнителни context-и, затварят се
  сами) и помощникът `e2e_login(page, base_url)` — всички от `conftest.py`.
  Не пускайте собствен `sync_playwright()`: докато сесийният браузър е жив,
  второ стартиране гърми. За таймери ползвайте `page.clock`, не дълги
  `wait_for_timeout`.

## Изолация

Тестовете **никога** не пипат реалната база данни или конфигурацията
(`ph_config.json`/`pacho_config.json`).
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
  JSON, разрешаване на пътя до базата).
- `test_updater.py` — семантично сравнение на версии, парсване и проверка
  на SHA256SUMS.txt манифеста (H3).
- `test_rename_migration.py` — преходът PachoLogistic/pacho_ → PHLogistics/ph_
  (одит 06.10.2026): двойно четене на имената, преместване/указател/пазач
  срещу нова празна база, архиви и маркери със старите имена, избор на файла
  за обновяване, скриптът за прехода през инсталатора (симулатор на cmd.exe),
  интерфейсът и инсталаторът/CI по изходния код. Истинският Windows —
  `scripts/ci_rename_migration_test.ps1` в release.yml.
- `test_auth.py` — хеширане на пароли, роли, must_change_password (C1,
  вкл. симулация на ъпгрейд от стара база без тази колона).
- `test_db.py` — настройки, пунктове за разтоварване, потребителски теми.
- `test_migrations.py` — рамката за миграции (PRAGMA user_version,
  идемпотентност, _ensure_column) — M1.
- `test_jsonutil.py` — екраниране на JSON за вграждане в `<script>` (H2).
- `test_login_guard.py` — заключване след повторни неуспешни опити (H5).
- `test_web_routes.py` — характеризиращ пакет през реален Flask test client
  (`appcore.create_app()` + всички `routes_*` модули, виж fixture
  `flask_app`/`client`/`admin_client`/`employee_client` в `conftest.py`,
  добавени във Фаза 3): вход/CSRF/задължителна смяна на парола, всичките
  5 документни потока (издаване/преглед/преглед на документ/редакция/
  Excel износ/изтриване), палетни bulk потоци, клиенти, настройки, админ
  панел (M7 — `client_delete` изисква admin), M9 (`MAX_CONTENT_LENGTH`),
  и достъпност (Фаза 4 — `role="alert"`, свързани `label`/`input` двойки,
  липса на твърдо кодиран `#5c6d80`).
- `test_currency_eur.py` — валута само евро на паричните полета
  (`appcore.format_eur_amount`, форма/печат/Excel/PDF износ).
- `test_documents_date_filter.py` — филтър по диапазон от дати в списъка
  с документи (`?from=&to=`, комбиниран с текстово търсене/групиране).
- `test_client_history.py` — история на документите от картата на клиента
  (`routes_clients._client_recent_documents`, точно съвпадение на име,
  не substring).
- `test_dashboard_stats.py` — обобщено табло/статистика (месечни брояч,
  топ клиенти, граници на месеца вкл. декемврийско превъртане).
- `test_document_attachments.py` — прикачване на снимка/скен към издаден
  документ (валиден/невалиден формат, изтегляне, admin-only изтриване).
- `test_e2e_smoke.py` — end-to-end „дим“ тестове с истински Chromium
  (Playwright, маркер `e2e`, изключен от бързия `pytest` по подразбиране,
  пуска се изрично в CI job "e2e") — вход → издаване на ЧМР → печатна
  страница, емулация на print media, история на клиент в реален браузър.
- `test_password_hashing.py` — продукцията хешира със scrypt; тестовете — с
  евтиния метод.
- `test_release_babel_data.py` — .exe-то пакетира CLDR данни само за
  БГ/EN/TR (`pyinstaller_hooks/hook-babel.py`) и приложението работи с тях.

## Обобщение по фаза

| Фаза | Тестове | Общо |
|---|---|---|
| 0–2 (сигурност, цялост на данни) | numbering/barcode/config/updater/auth/db/migrations/backup_sync/jsonutil/login_guard/secrets_store (backup_sync и secrets_store по-късно премахнати с GitHub синхронизацията) | 90 |
| 3 (структурен рефакторинг) | `test_web_routes.py` (fixtures: `flask_app`, `client`, `admin_client`, `employee_client`) | +28 |
| 4 (фронтенд/достъпност) | M7/M9/достъпност регресионни тестове в `test_web_routes.py` | +8 |
| „направи всичко което предлагаш“ (EUR/дати/клиент история/табло/прикачени файлове/XSS баркод в `test_barcode.py`/Playwright CI) | нови файлове по-горе + 2 регресионни теста в `test_barcode.py` (v3.30.0–v3.36.0) | +40 |
| *(междинни версии v3.13.0–v3.29.1, публикувани директно към хранилището между Фаза 4 и горния ред)* | — | +111 |
| **Общо (към v3.36.0)** | | **277** (273 по подразбиране + 4 с маркер `e2e`) |

Към 01.10.2026: ~1250 бързи теста (~3 мин.) и ~110 e2e теста.
