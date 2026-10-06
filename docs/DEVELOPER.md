# PH Logistics — документация за разработчици

Този документ е за хора, които поддържат или разширяват кода на
PH Logistics — не за крайни потребители (за тях виж
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
| `config.py` | `ph_config.json` (при стара инсталация `pacho_config.json`) bootstrap настройки (път на базата, мрежов режим) — четат се ПРЕДИ базата да съществува. |
| `legacy_migration.py` | Имената на файловете с данни (нови `ph_…` и стари `pacho_…`), изборът между тях и еднократният преход на локалната инсталация към `PHLogistics` (виж раздел 8). Само стандартна библиотека — вика се най-отгоре в `app.py`. |
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
| `update_complete_rename` | `/update/complete-rename` | routes_admin | Старата локална инсталация завършва прехода към новите имена (инсталаторът на текущата версия). |
| `preview_document` | `/preview/<token>` | app.py (директно) | Показва предварителен преглед по временен токен — споделен между всичките 6 типа документи, затова е в `app.py`, не в `routes_documents.py`. |

## 6. Конфигурационни/данни файлове (на терен, до .exe-то)

| Файл | Съдържание | В `.gitignore`? |
|---|---|---|
| `ph_logistics.db` (стара инсталация: `pacho_logistic.db`) | Основната SQLite база (документи, клиенти, служители). | Да |
| `.secret_key` | Flask session secret key, генерира се автоматично при първо стартиране. | Да |
| `ph_config.json` (стара инсталация: `pacho_config.json`) | Bootstrap настройки: път на базата, мрежов режим/порт (виж `config.DEFAULTS`). | Да |
| `ph_startup*.log` (по-рано `pacho_startup*.log`) | Лог файл — активен САМО когато `.exe`-то е билднато с `--windowed` (без конзола) и `sys.stdout`/`stderr` са `None`. | Да |
| `ph_migration.json` | Отчет (и дневник по време на преместването) от прехода към новите имена — виж раздел 8. | Да |
| `ph_update*.log`, `ph_update_*.bat`, `ph_update_failed_*.txt` | Скриптът/логът/маркерът на автоматичното обновяване (старите `pacho_update*` също се четат). | Да |

До базата (папката на `DB_PATH`) стоят още `attachments/`, `company_logo.*` и
маркерите за възстановяване `ph_restore_request.json`/`ph_restore_result.json`
(старите `pacho_restore_*.json` също се четат). Архивите в папката за архив
са `ph_logistics_ГГГГММДД_ЧЧММСС_xxxxxx.db` (старите `pacho_logistic_…db` се
разпознават за възстановяване и ротация).

Всички горни файлове са специфични за ВСЯКА инсталация (терен) — никога
не се качват в git хранилището; вижте `.gitignore`.

## 7. Ритуал за release

1. Слей одобрените промени в `develop`, после в `main` (само с изрично
   одобрение на собственика — виж координационния раздел на
   `ПЛАН_ЗА_РАЗРАБОТКА.md`).
2. Вдигни `__version__` в `version.py` (семантично: голяма.средна.малка).
3. Push към `main` — `.github/workflows/release.yml` се задейства
   автоматично САМО при промяна на `version.py`: компилира
   `PHLogistics.exe` (PyInstaller, Windows runner), проверява прехода от
   старите имена на истински Windows (`scripts/ci_rename_migration_test.ps1`,
   два сценария — твърда бариера), публикува и копия
   `PachoLogistic.exe`/`PachoLogistic-Setup.exe` (за версиите до v3.78), генерира
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

## 8. Преходът към новите технически имена (PachoLogistic → PHLogistics)

Търговското име е „PH Logistics“ от v3.78.0; след нея и файловете/папките
следват: `PHLogistics.exe`, `PHLogistics-Setup.exe`, папка
`%LOCALAPPDATA%\Programs\PHLogistics`, `ph_config.json`, `ph_logistics.db`,
`ph_startup*.log`, `ph_update*`, архиви `ph_logistics_…`, маркери
`ph_restore_*.json`, потребителска папка `%LOCALAPPDATA%\PHLogistics`.
Константите са във `version.py` (имена на файлове за изтегляне и папки) и
`legacy_migration.py` (файлове с данни).

**Двойно четене — ЗАВИНАГИ, не само за един релийз.** Навсякъде, където
програмата търси данни, първо гледа новото име, после старото, а нов файл
се създава само с новото (`legacy_migration.resolve_existing`): конфигурацията,
базата по подразбиране (а `db._USE_WAL` сравнява с ИЗБРАНИЯ път, не с твърдо
име), архивите (`backup._BACKUP_NAME_RE`), маркерите за възстановяване,
маркерите за провалено обновяване. Причината: мрежовите и преносимите
инсталации (`.exe` в споделена папка, `db_path` към мрежов диск, няколко
компютъра) НЕ се преименуват — никой компютър не може безопасно да
преименува файл, който другите държат отворен или търсят по име. Махането на
старите имена би направило тези бази „невидими“ (нова празна база с
admin/admin123). `db_path`, зададен изрично, никога не се преименува.

**Еднократното преместване** (`legacy_migration.migrate_install_dir`, вика се
най-отгоре в `app.py`, преди лога/`config`/`db`) важи само когато новото
`.exe` работи от `%LOCALAPPDATA%\Programs\PHLogistics`, старата папка
`…\PachoLogistic` има данни, а новата — още не. Решение:

| Положение | Какво става |
|---|---|
| Старата папка безопасна (не е в Windows дял по `LanmanServer\Shares`, не е мрежова), старото `.exe` не работи, базата се чете, архивът/`db_path`/възстановяването не сочат вътре в нея, няма конфликт на имената | Всичко без програмните файлове се мести с `os.replace` (базата с `-wal`/`-shm` първа) и се преименува (`pacho_logistic.db` → `ph_logistics.db`, `pacho_config.json` → `ph_config.json`, `pacho_*` → `ph_*`); преди първото преместване се пише дневник (`ph_migration.json`, `in_progress`), при грешка всичко се връща; старите програмни файлове се трият, празната стара папка — също. |
| Папката е споделена/мрежова, регистърът не се чете, старата версия работи, файл е зает, повредена конфигурация/база, `db_path`/архив/възстановяване вътре в старата папка, конфликт на имената | Нищо не се мести. Новата папка получава `ph_config.json` (всички стари ключове + `db_path` към старата база) и копие на `.secret_key`; прикачените файлове и логото следват базата. |
| Дори указателят не може да бъде записан | `legacy_migration.runtime_db_override()` — `config.resolve_db_path` връща старата база (никога нова празна в новата папка). |
| Прекъснато преместване (ток) | Следващият старт връща преместеното по дневника и решава наново. |
| И двете папки имат данни | Нищо не се пипа; администраторът вижда известие веднъж. |

Администраторът вижда резултата веднъж (`routes_admin._flash_migration_report`).

**Как стига старата локална инсталация дотам:** (а) потребителят пуска
новия инсталатор (`UsePreviousAppDir=no`, същият `AppId`; `[InstallDelete]`
трие само старите програмни файлове и само ако старата папка не е споделена);
(б) бутонът „Завърши преминаването“ на таблото или (в) следващото
обновяване — `updater.install_via_setup` сваля проверения (SHA-256)
`PHLogistics-Setup.exe` и пуска скрипт, който чака процеса да излезе, пази
копие на старото `.exe`, пуска инсталатора с `/DIR=…\PHLogistics` и
стартира новото `.exe`. Ако инсталаторът се провали — връща и пуска старото
и пише `ph_rename_failed_*.txt` (без цикъл; обновяването продължава на
място). След успешен инсталатор старото `.exe` НЕ се пуска никога (новото
може вече да е преместило данните). Ръчен/CI вход:
`PHLogistics.exe --complete-rename-with <инсталатор>`.

**Какво НЕ се сменя нарочно:** `PACHO_UPDATE_STARTED_MARKER` (скриптът на
v3.78 я подава на новата версия — с друго име обновяването би се връщало
назад) и `PACHO_DISABLE_AUTO_UPDATE`; вътрешни идентификатори, които не са
имена на файлове (бисквитката `pacho_pdf_ready`, `/pacho-fix-db-path`,
`PACHO_DB_UNAVAILABLE`, шрифтовете `PachoDejaVuSans` в PDF). Релийзът
публикува и `PachoLogistic.exe`/`PachoLogistic-Setup.exe` с редове в
`SHA256SUMS.txt` — версиите до v3.78 обновяват само по тези имена.

