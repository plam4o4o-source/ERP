# ПачоЛогистик — документация за разработчици

Този документ е за хора, които поддържат или разширяват кода на
ПачоЛогистик — не за крайни потребители (за тях виж
`docs/РЪКОВОДСТВО_ЗА_ПОТРЕБИТЕЛЯ.md` и `README.md`). Описва архитектурата
след структурния рефакторинг от Фаза 3 (виж `ПЛАН_ЗА_РАЗРАБОТКА.md`).

## 1. Архитектурен преглед

```
┌─────────────────────────────────────────────────────────────────┐
│  app.py  (тънка входна точка)                                    │
│  ├─ извиква appcore.create_app()                                 │
│  ├─ регистрира всички routes_* модули (register(app))            │
│  └─ пази десктоп bootstrap блока (pywebview/фонов Flask сървър)   │
└───────────────┬─────────────────────────────────────────────────┘
                │
   ┌────────────▼─────────────┐
   │  appcore.py               │  Flask app-фабрика (create_app), общи
   │  (ядро)                   │  decorator-и (login_required/admin_required),
   │                            │  CSRF, задължителна смяна на парола,
   │                            │  DOCUMENT_FLOWS регистър, общи помощни
   │                            │  функции (form_data/save_document/...).
   └────────────┬──────────────┘
                │  всеки routes_*.py модул внася appcore и регистрира
                │  своите endpoint-и с ТОЧНО оригиналните имена/пътища
   ┌────────────┼──────────────────────────────────────────────────┐
   │            │                                                  │
routes_auth  routes_dashboard  routes_documents  routes_pallet_extra
routes_clients  routes_settings  routes_admin
   │
   └─ всеки борави с db.py (SQLite), templates/*.html (Jinja2),
      и споделените backup.py/updater.py/config.py/branding.py и др.
```

**Защо не буквални Flask blueprints:** blueprint-ите преименуват
endpoint-ите (`blueprint.endpoint`), което би изисквало промяна на всяко
`url_for(...)` в 24-те Jinja шаблона. Вместо това всеки `routes_*.py`
модул има функция `register(app)`, която регистрира маршрутите му директно
върху споделения `app` обект с `add_url_rule` — endpoint имената остават
непроменени спрямо оригиналния монолитен `app.py` (преди Фаза 3).

## 2. Карта на модулите

| Модул | Отговорност |
|---|---|
| `app.py` | Входна точка: `create_app()` + регистрация на routes_* + десктоп bootstrap (pywebview/фонов сървър/автообновяване). |
| `appcore.py` | Flask фабрика, decorator-и, CSRF, hook-ове, `DOCUMENT_FLOWS`, общи помощни функции, preview store. |
| `routes_auth.py` | `/login`, `/logout`, `/password` (смяна на парола). |
| `routes_dashboard.py` | `/` (табло), `/scan`, `/barcode/<code>.svg`. |
| `routes_documents.py` | Списък/преглед/редакция/Excel износ/изтриване на документи + генеричното издаване/преглед (`_document_new`/`_document_preview`) на 6-те типа документи. |
| `routes_pallet_extra.py` | Импорт от Excel (единичен и bulk), preview/issue/result за bulk палетни карти, `packing_pull_pallet`. |
| `routes_clients.py` | Адресна книга (списък/редакция/изтриване). |
| `routes_settings.py` | Настройки на фирмата, лого, лични настройки/тема + системни настройки, вградени в `/my-settings`. |
| `routes_admin.py` | Системни настройки (мрежа/архив/GitHub), отдалечен достъп, админ панел (служители), проверка/инсталиране на обновления. |
| `db.py` | SQLite схема, миграции (`PRAGMA user_version`), номериране на документи (`next_number`, atomic), CRUD помощни функции. |
| `config.py` | `pacho_config.json` bootstrap настройки (път на базата, мрежов режим, GitHub данни) — четат се ПРЕДИ базата да съществува. |
| `secrets_store.py` | Fernet шифроване на `gh_token` в покой (ключ в `<config>.key`). |
| `login_guard.py` | Rate-limiting/lockout при неуспешни опити за вход (в паметта, не в базата). |
| `backup.py` | Локален архив, GitHub push/pull, конфликт-проверка при синхронизация (`RemoteChangedError`), асинхронно ръчно качване (`trigger_sync_now`). |
| `updater.py` | Проверка/изтегляне/инсталиране на нова версия от GitHub Releases, проверка на SHA256 контролна сума. |
| `branding.py` | Лого на фирмата (качване/премахване/показване). |
| `barcode128.py` | Генериране на Code128 SVG баркодове. |
| `icons.py` | Вграден SVG набор от икони (`render_icon`/`icon()` в шаблоните). |
| `jsonutil.py` | Безопасно вграждане на JSON в `<script>` блокове (escape на `</script>` и др. — виж H2). |
| `desktop.py` | Отваряне на настолен прозорец (pywebview/WebView2) или fallback към браузър. |
| `remote_tunnel.py` | Cloudflare Quick Tunnel за отдалечено сканиране с телефон. |
| `net.py` | Общ HTTP wrapper (сертификати/timeout) за заявки към GitHub API. |
| `version.py` | Текуща версия (`__version__`) — вдигането ѝ пуска release CI. |

## 3. Схема на базата данни

SQLite, дефинирана в `db.SCHEMA` (създава се идемпотентно с
`CREATE TABLE IF NOT EXISTS`; промени по вече съществуваща инсталация
минават през `db.MIGRATIONS`, не през промяна на `SCHEMA` — виж раздел 4).

| Таблица | Основни колони | Бележка |
|---|---|---|
| `users` | `username` (уникален), `password_hash`, `full_name`, `role` (`admin`/`employee`), `active`, `must_change_password` | Задължителна смяна на парола при първоначалния `admin` (ако още е с паролата по подразбиране) и при всяка парола, зададена от друг администратор. |
| `settings` | `key`, `value` | Настройки на фирмата изпращач (име, адрес, ЕИК...) + локален архив — key/value двойки, споделени за цялата инсталация. |
| `user_settings` | `user_id`, `key`, `value` | Лични настройки по потребител (в момента: избраната тема). |
| `clients` | `name`, `address`, `city`, `postcode`, `country`, `eik`, `vat`, `phone`, `email`, `contact` | Адресна книга. |
| `client_unload_points` | `client_id`, `label`, `address`, `city`, `postcode`, `country` | Неограничен брой пунктове за разтоварване на клиент (за ЧМР поле 3). |
| `counters` | `doc_type`, `year`, `last` | Последен използван пореден номер по тип документ и година — четен/писан атомарно през `next_number()` (виж H6). |
| `documents` | `doc_type`, `number`, `year`, `seq`, `barcode` (уникален), `data` (JSON blob), `created_by` | Всеки издаден документ от всичките 6 типа — `data` пази пълния речник с полетата на формата (виж `DOCUMENT_FLOWS`/`_XLSX_FIELDS` за точния списък по тип). |

`DOC_TYPES` (в `db.py`) е регистърът на шестте типа документи с представка
за номера и заглавие: `cmr` (CMR), `packing` (OPL), `pallet` (PAL),
`waybill` (TOV, товарителница за вътрешен превоз по Наредба № 33 на МТСИТ),
`dualuse` (DUD), `export_it` (EXI).

## 4. Миграции на схемата

`db.MIGRATIONS` е подреден списък от идемпотентни Python функции,
прилагани веднъж през `_apply_migrations()` при `init_db()`, следени чрез
вградения на SQLite `PRAGMA user_version` (брояч, не таблица — оцелява
дори при повредена/липсваща таблица за версии). За добавяне на нова
промяна по схемата:

1. Напиши функция `_mNNN_кратко_име(con)` в `db.py`, декорирана с `@_migration`.
2. Функцията трябва да е идемпотентна (безопасно за повторно изпълнение) —
   ползвай `_ensure_column()` за нови колони вместо суров `ALTER TABLE`.
3. Добави ѝ кратък коментар защо е нужна (виж съществуващия
   `_m001_must_change_password` за пример).

НЕ променяй директно `SCHEMA` за инсталации, които вече съществуват на
терен — само нови миграции; `SCHEMA` е само за чисто нови бази.

## 5. Маршрути (route → предназначение)

| Endpoint | URL | Модул | Предназначение |
|---|---|---|---|
| `login` / `logout` | `/login`, `/logout` | routes_auth | Вход/изход. |
| `change_password` | `/password` | routes_auth | Смяна на парола (изключен от enforce, за да не блокира сам себе си). |
| `dashboard` | `/` | routes_dashboard | Табло, последно издадени, наличности за годината, известие за обновяване. |
| `scan` | `/scan` | routes_dashboard | Зареждане на документ по сканиран баркод/номер. |
| `barcode_svg` | `/barcode/<code>.svg` | routes_dashboard | Генериране на баркод изображение. |
| `documents` | `/docs` | routes_documents | Списък с филтър по тип/търсене. |
| `view_document` / `edit_document` | `/doc/<id>`, `/doc/<id>/edit` | routes_documents | Преглед за печат / редакция (без нов номер). |
| `export_document_xlsx` | `/doc/<id>/export.xlsx` | routes_documents | Износ в Excel. |
| `delete_document` | `/doc/<id>/delete` | routes_documents | Изтриване (само admin). |
| `<type>_new` / `<type>_preview` (× 6) | `/cmr/new`, `/packing/new`, `/waybill/new`, ... | routes_documents | Издаване/преглед по тип — генерично през `DOCUMENT_FLOWS`. |
| `packing_pull_pallet` | `/packing/pull-pallet` | routes_pallet_extra | Издърпва обобщен ред от палетна карта в опаковъчен лист. |
| `pallet_import` / `pallet_bulk_*` | `/pallet/import`, `/pallet/bulk-*` | routes_pallet_extra | Импорт от Excel (единичен/bulk от справка за поръчки). |
| `clients_list` / `client_edit` / `client_delete` | `/clients*` | routes_clients | Адресна книга (изтриване — само admin, виж M7). |
| `settings_page` / `settings_logo_*` | `/settings*` | routes_settings | Данни на фирмата изпращач + лого. |
| `my_settings` | `/my-settings` | routes_settings | Лична тема + (за admin) вградени системни настройки. |
| `system_settings` / `system_backup_*` / `system_pull_now` | `/admin/system*` | routes_admin | Мрежа, локален архив, GitHub синхронизация (само admin). |
| `system_remote_*` | `/admin/system/remote-*` | routes_admin | Cloudflare тунел за сканиране от телефон (само admin). |
| `admin_users` / `admin_user_*` | `/admin/users*` | routes_admin | Управление на служители (само admin). |
| `update_check` / `update_install` | `/update/*` | routes_admin | Проверка/инсталиране на нова версия. |
| `preview_document` | `/preview/<token>` | app.py (директно) | Показва предварителен преглед по временен токен — споделен между всичките 6 типа документи, затова е в `app.py`, не в `routes_documents.py`. |

## 6. Конфигурационни/данни файлове (на терен, до .exe-то)

| Файл | Съдържание | В `.gitignore`? |
|---|---|---|
| `pacho_logistic.db` | Основната SQLite база (документи, клиенти, служители). | Да |
| `pacho_logistic.db.syncstate.json` | Последно познато GitHub SHA за проверка на конфликт при синхронизация (M2) — НЕ данни на фирмата. | Да |
| `.secret_key` | Flask session secret key, генерира се автоматично при първо стартиране. | Да |
| `pacho_config.json` | Bootstrap настройки: път на базата, мрежов режим/порт, GitHub данни (виж `config.DEFAULTS`). | Да |
| `pacho_config.json.key` | Fernet ключ за декриптиране на `gh_token` в `pacho_config.json`. | Да |
| `pacho_startup.log` | Лог файл — активен САМО когато `.exe`-то е билднато с `--windowed` (без конзола) и `sys.stdout`/`stderr` са `None`. | Да |

Всички горни файлове са специфични за ВСЯКА инсталация (терен) — никога
не се качват в git хранилището; вижте `.gitignore`.

## 7. Ритуал за release

1. Слей одобрените промени в `develop`, после в `main` (само с изрично
   одобрение на собственика — виж координационния раздел на
   `ПЛАН_ЗА_РАЗРАБОТКА.md`).
2. Вдигни `__version__` в `version.py` (семантично: голяма.средна.малка).
3. Push към `main` — `.github/workflows/release.yml` се задейства
   автоматично САМО при промяна на `version.py`: компилира
   `PachoLogistic.exe` (PyInstaller, Windows runner), генерира
   `SHA256SUMS.txt` (за проверка на контролната сума при автообновяване —
   виж H3/`updater.parse_sha256sums`), гради Windows инсталатор (Inno
   Setup, `installer.iss`), и публикува GitHub Release с версийния таг.
4. Всяка вече инсталирана копия проверява за нова версия (`updater.py`,
   фонов цикъл или ръчен бутон), сравнява SHA256 преди инсталиране, и се
   рестартира автоматично (освен в мрежов/сървърен режим — там е ръчно,
   за да не се прекъсва работата на другите).
5. Добави запис в `CHANGELOG.md` за новата версия (кратко описание, взето
   от commit съобщението, което вдига версията).

`ci.yml` (различен от `release.yml`) се пуска на ВСЯКО push/PR:
`pytest` е твърда бариера (гърми build-а при провален тест); `bandit` +
`pip-audit` са само за отчет (`continue-on-error: true`) — виж
`tests/README.md` за покритието по находка.
