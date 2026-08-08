# -*- coding: utf-8 -*-
"""База данни (SQLite) на ПачоЛогистик — схема, инициализация и номерация."""
import os
import sqlite3
import secrets
import sys
import time
from datetime import date

from werkzeug.security import check_password_hash, generate_password_hash

import config as appconfig

# В компилираната .exe версия базата данни стои до самия .exe файл,
# а не във временната папка на PyInstaller.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Пътят може да бъде пренасочен към мрежов диск чрез pacho_config.json
# (виж config.py и „Системни настройки“ в програмата).
DB_PATH = appconfig.resolve_db_path(BASE_DIR)
SECRET_PATH = os.path.join(BASE_DIR, ".secret_key")

# WAL се включва само когато базата е на подразбиращото се локално място
# (виж _wal_is_safe_here по-долу за причината) — изчислено веднъж тук,
# по същия начин, по който вече се изчислява самият DB_PATH (не се очаква
# да се променя по време на изпълнение без рестарт на програмата).
_USE_WAL = DB_PATH == os.path.join(BASE_DIR, "pacho_logistic.db")

# Типове документи: префикс за баркода и заглавие на български
DOC_TYPES = {
    "cmr": {"prefix": "CMR", "title": "ЧМР товарителница"},
    "packing": {"prefix": "OPL", "title": "Опаковъчен лист"},
    "pallet": {"prefix": "PAL", "title": "Палетна карта"},
    "waybill": {"prefix": "TOV", "title": "Товарителница (вътрешен превоз)"},
    "dualuse": {"prefix": "DUD", "title": "Декларация за стоки с двойна употреба"},
    "export_it": {"prefix": "EXI", "title": "Декларация за износ (Италия)"},
    # Фактури — отделен тип за всяка държава, защото самите бланки се
    # различават по колони и заглавие (виж invoice_br_print.html /
    # invoice_no_print.html): Бразилия е „INVOICE“ с колона Net weight,
    # Норвегия е „COMMERCIAL INVOICE“ с колони Material Description и
    # Pallet Number. Отделните типове дават и отделна номерация на всяка
    # държава, което е и досегашната практика в приложените образци.
    "invoice_br": {"prefix": "INVBR", "title": "Фактура за Бразилия"},
    "invoice_no": {"prefix": "INVNO", "title": "Фактура за Норвегия"},
}

#: Типовете, които са ФАКТУРИ. Заявка: „в раздела Фактури да има издадени
#: документи и само там да се появяват издадените фактури; фактури да не
#: отиват в [Всички документи]“ + „и от таблото/историята на клиента“.
#: Държи се на ЕДНО място, за да не се разминат петте места, които трябва
#: да ги изключват (списък с документи, статистика на таблото, най-активни
#: клиенти, последни документи, история на клиента) — всяко от тях чете
#: оттук, вместо да изброява типовете само́.
INVOICE_DOC_TYPES = ("invoice_br", "invoice_no")


def non_invoice_doc_types():
    """Типовете за общите екрани (всичко без фактурите)."""
    return tuple(k for k in DOC_TYPES if k not in INVOICE_DOC_TYPES)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'employee',      -- 'admin' или 'employee'
    active INTEGER NOT NULL DEFAULT 1,
    -- Задължава смяна на паролата при следващия вход — задава се на
    -- първоначалния admin акаунт и при всяка парола, зададена от друг
    -- администратор (той я знае, значи не е лична тайна на служителя).
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Индивидуални настройки за всеки служител (тема и др.), не общи за фирмата.
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    postcode TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    eik TEXT NOT NULL DEFAULT '',
    vat TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Пунктове за разтоварване на клиент — неограничен брой на клиент (напр.
-- различни складове/обекти на една и съща фирма), за избор в ЧМР вместо
-- ръчно въвеждане на адреса всеки път.
CREATE TABLE IF NOT EXISTS client_unload_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    postcode TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_unload_points_client ON client_unload_points(client_id);

CREATE TABLE IF NOT EXISTS counters (
    doc_type TEXT NOT NULL,
    year INTEGER NOT NULL,
    last INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (doc_type, year)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type TEXT NOT NULL,
    number TEXT NOT NULL,
    year INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    barcode TEXT NOT NULL UNIQUE,
    -- Случаен (128-битов), НЕ отгатваем токен за публичен преглед БЕЗ вход
    -- през QR код на бланката (заявка: „всеки, който сканира с телефон
    -- баркода..., без да има нужда от домейна, който е в програмата“) —
    -- за разлика от `barcode` (предвидим формат ТИП-ДДММГГГГ-####, годен
    -- само за ВЪТРЕШНО повторно въвеждане в самата програма, никога за
    -- публичен адрес), виж routes_documents.public_document_view и
    -- appcore.save_document. UNIQUE тук е само за чисто нови инсталации —
    -- при вече съществуваща база колоната идва през миграция m002 по-долу
    -- (ALTER TABLE не поддържа добавяне на UNIQUE, затова там е отделен
    -- CREATE UNIQUE INDEX вместо инлайн ограничение).
    public_token TEXT UNIQUE,
    data TEXT NOT NULL DEFAULT '{}',
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_documents_type_year ON documents(doc_type, year, seq);

-- Прикачени снимки/сканирания към издаден документ (напр. снимка на
-- подписана бланка) — заявка: „направи всичко което предлагаш“. Самите
-- файлове стоят на диск до базата (виж attachments.py), тук само
-- метаданните. token е случаен низ, ползван за файловото име на диска
-- (виж attachments.py за причината да не е просто ID-то на реда).
CREATE TABLE IF NOT EXISTS document_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    uploaded_by INTEGER,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    FOREIGN KEY (uploaded_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_attachments_document ON document_attachments(document_id);

-- Справочник материали (ABB part ID → описание → нето тегло кг/бр) —
-- зарежда се ВЕДНЪЖ от Excel файл през раздел „Материали“ и остава в
-- базата (заявка: „да не се зарежда всеки път файла с материалите; като
-- се зареди веднъж, да си остава зареден в програмата“). Ползва се за
-- автоматично попълване на теглото/описанието във фактурите по въведения
-- код на материала — вижте routes_materials.py и routes_invoices.py.
--
-- code е PRIMARY KEY (не отделно AUTOINCREMENT id): кодът на материала е
-- естественият уникален ключ и всяко търсене минава през него, а при
-- повторно зареждане на файла редът просто се презаписва (INSERT ON
-- CONFLICT DO UPDATE) вместо да се дублира. В подадения файл има 44
-- повтарящи се кода — надделява ПОСЛЕДНИЯТ ред от файла.
CREATE TABLE IF NOT EXISTS materials (
    code TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    net_weight TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Адресна книга САМО за фактури — заявка: „в раздел Фактури добави адресна
-- книга; да съдържа данните за фактуриране на клиентите и също да има
-- адрес за доставка“. Нарочно ОТДЕЛНА от таблица clients (адресната книга
-- за ЧМР/палетни карти): фактурата има нужда от два различни адреса
-- едновременно (получател на стоката и получател на фактурата, които при
-- ABB редовно са различни юридически лица в различни държави), а полетата
-- ѝ (напр. адресът като един многоредов блок, както излиза на бланката)
-- не съвпадат с раздробените град/пощенски код полета на clients.
CREATE TABLE IF NOT EXISTS invoice_clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    delivery_name TEXT NOT NULL DEFAULT '',
    delivery_address TEXT NOT NULL DEFAULT '',
    delivery_phone TEXT NOT NULL DEFAULT '',
    billing_name TEXT NOT NULL DEFAULT '',
    billing_address TEXT NOT NULL DEFAULT '',
    billing_phone TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def get_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.isdir(db_dir):
        raise RuntimeError(
            "Папката за базата данни не съществува или мрежовият диск не е "
            "достъпен: %s — проверете пътя в „Системни настройки“." % db_dir
        )
    # timeout=15 задава и SQLite busy_timeout-а (15000ms) на ниво Python
    # драйвер — заявка, заварила базата заключена от друга едновременна
    # връзка, изчаква вместо да гърми веднага с "database is locked".
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    if _USE_WAL:
        # WAL позволява четци да не блокират писачи (и обратно) — значимо
        # по-добра едновременност при няколко служители/връзки едновременно.
        # Настройката е на ниво .db ФАЙЛ (не на връзка), затова повторното
        # ѝ задаване при всяко отваряне е евтин no-op, щом вече е активна.
        # НЕ се включва, когато базата е пренасочена към мрежов диск (виж
        # _USE_WAL по-горе) — SQLite официално предупреждава, че WAL е
        # по-ненадежден от класическия journal върху мрежови файлови
        # системи (SMB/NFS), защото разчита на споделена памет, която там
        # не винаги работи коректно.
        try:
            con.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass  # файлова система без поддръжка на споделена памет и т.н.
    return con


def get_secret_key():
    """Постоянен таен ключ за сесиите, пази се във файл до базата."""
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(SECRET_PATH, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def _ensure_column(con, table, column, coldef):
    """Добавя колона към вече съществуваща таблица, ако липсва.

    `CREATE TABLE IF NOT EXISTS` (виж SCHEMA по-горе) създава новите колони
    само за чисто нови инсталации — при вече съществуваща таблица (стара
    база данни на терен) е no-op и колоната никога не се появява, което би
    счупило всяка заявка, която я очаква. Помощна функция за миграционни
    стъпки по-долу (виж MIGRATIONS/_apply_migrations).

    Имената на таблица/колона идват само от хардкоднати повиквания в кода
    по-долу (никога от потребителски вход), затова директното им вграждане
    в SQL низа тук е безопасно (както другаде в модула, напр. IN (...) в
    get_unload_points_map).
    """
    cols = [r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)]
    if column not in cols:
        con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, coldef))


# ---------------------------------------------------------------------- миграции
# CREATE TABLE/INDEX IF NOT EXISTS (в SCHEMA по-горе) покрива само чисто
# нови инсталации — промяна по вече съществуваща таблица (нова колона,
# презапълване на данни и т.н.) за инсталации на терен изисква изрична,
# подредена, ИДЕМПОТЕНТНА стъпка тук. `PRAGMA user_version` пази в самия
# .db файл номера на последно приложената стъпка — при отваряне на база от
# по-стара версия на програмата, само НЕПРИЛОЖЕНИТЕ стъпки (по ред) се
# изпълняват; при чисто нова база SCHEMA вече създава всичко в найновия си
# вид, но стъпките пак минават безобидно (затова всяка трябва да е
# идемпотентна — обичайно чрез _ensure_column/"IF NOT EXISTS").
MIGRATIONS = []


def _migration(func):
    """Декоратор: регистрира функция като поредната миграционна стъпка,
    по реда на дефиниране в кода (номерът ѝ = позицията ѝ в MIGRATIONS,
    започвайки от 1) — НЕ преименувайте/пренареждайте съществуващи стъпки,
    само добавяйте нови в края."""
    MIGRATIONS.append(func)
    return func


@_migration
def _m001_must_change_password(con):
    """Добавена в поправката по сигурността C1 (задължителна смяна на
    паролата за фабричния admin/admin123 и за пароли, зададени от друг
    администратор) — виж app.py login()/change_password()."""
    _ensure_column(con, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")


@_migration
def _m002_public_token(con):
    """Заявка: „всеки, който сканира с телефон баркода на някой от
    документите, да му се зареди директно документа, без да има нужда от
    домейна, който е в програмата“ — публичен, БЕЗ вход преглед през QR
    код на бланката (routes_documents.public_document_view), с отделен
    непредвидим `public_token` (виж коментара в SCHEMA по-горе за защо не
    се ползва `barcode`).

    _ensure_column не може да добави UNIQUE ограничение през ALTER TABLE
    (SQLite ограничение) — затова тук изричен CREATE UNIQUE INDEX за вече
    съществуващи бази (чисто новите вече го имат инлайн от SCHEMA; вторият
    индекс е безобиден при съвпадение, IF NOT EXISTS го прави идемпотентен
    и на всяко следващо стартиране).

    Съществуващите документи (издадени преди тази версия) нямат токен —
    попълва се тук еднократно за всеки от тях, за да могат и старите
    записи да получат работещ публичен адрес при следващ преглед/печат
    (самата хартия с QR кода се появява едва при следващо разпечатване,
    но самите данни в базата вече са готови за него)."""
    _ensure_column(con, "documents", "public_token", "TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_public_token"
               " ON documents(public_token)")
    rows = con.execute(
        "SELECT id FROM documents WHERE public_token IS NULL"
    ).fetchall()
    for row in rows:
        con.execute("UPDATE documents SET public_token = ? WHERE id = ?",
                   (secrets.token_hex(16), row["id"]))


def _apply_migrations(con):
    applied = con.execute("PRAGMA user_version").fetchone()[0]
    for step_number, step in enumerate(MIGRATIONS, start=1):
        if step_number > applied:
            step(con)
    if MIGRATIONS:
        # SQLite не поддържа bound параметри в PRAGMA — числото идва само
        # от len() тук (не от потребителски вход), безопасно е за форматиране.
        con.execute("PRAGMA user_version = %d" % len(MIGRATIONS))


def init_db():
    con = get_db()
    con.executescript(SCHEMA)
    _apply_migrations(con)
    # Първоначален администраторски акаунт — паролата е публично позната
    # (документирана в README/release бележките), затова задължаваме смяна
    # ѝ веднага при първия вход (виж must_change_password по-горе).
    row = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    if row["c"] == 0:
        con.execute(
            "INSERT INTO users"
            " (username, password_hash, full_name, role, active, must_change_password)"
            " VALUES (?, ?, ?, 'admin', 1, 1)",
            ("admin", generate_password_hash("admin123"), "Администратор"),
        )
    else:
        # Съществуваща база (обновяване от по-стара версия): ако акаунтът
        # 'admin' все още ползва точно фабричната парола 'admin123', я
        # задължаваме за смяна и тук — не само при чисто нова инсталация.
        # Не пипаме останалите потребители: те може вече да са я сменили и
        # нямаме как да различим "все още admin123" от "нарочно избрана
        # проста парола" без да проверим точно тази известна стойност.
        admin_row = con.execute(
            "SELECT id, password_hash FROM users WHERE username = 'admin'"
        ).fetchone()
        if admin_row and check_password_hash(admin_row["password_hash"], "admin123"):
            con.execute(
                "UPDATE users SET must_change_password = 1 WHERE id = ?",
                (admin_row["id"],),
            )
    # Данни на фирмата изпращач по подразбиране (само при чисто нова база —
    # редактируеми по-късно от „Фирма изпращач“). Взети от реални документи
    # на фирмата (ЧМР и декларация за двойна употреба).
    settings_row = con.execute("SELECT COUNT(*) AS c FROM settings").fetchone()
    if settings_row["c"] == 0:
        save_settings(con, {
            "sender_name": "ББС - България ООД / BBS Bulgaria Ltd",
            "sender_address": "ул. Георги Димитров 47",
            "sender_city": "Яворец",
            "sender_postcode": "5334",
            "sender_country": "България",
            "sender_eik": "205284599",
            "sender_vat": "",
            "sender_phone": "",
            "sender_email": "",
            "sender_person": "",
            # Английска версия на данните на фирмата (по избор — редактируема
            # от „Фирма изпращач“) — за БГ/EN превключвателя при попълване на
            # „Изпращач“ в нов документ (виж routes_documents._apply_sender_lang).
            # Взето директно от вече съществуващото двуезично sender_name по-горе.
            "sender_name_en": "BBS Bulgaria Ltd",
            "sender_address_en": "47 Georgi Dimitrov Str.",
            "sender_city_en": "Yavorets",
            "sender_country_en": "Bulgaria",
        })
    con.commit()
    con.close()


def next_number(con, doc_type, max_retries=8):
    """Следващ пореден номер за типа документ в текущата година.

    Годината се взима автоматично от системната дата, така че всяка нова
    година номерацията започва отначало от 0001.
    Връща (number, year, seq, barcode), напр. ('0001/2026', 2026, 1,
    'CMR-07082026-0001') — заявка: „в баркодовете... да се съдържа и
    датата“ — баркодът вече носи ПЪЛНАТА дата на издаване (ДДММГГГГ,
    системната дата към момента на извикването), не само годината, както
    преди. `number`/годишният брояч в `counters` остават непроменени
    (само по година, не по ден) — влияе единствено на съдържанието на
    самия баркод.

    АТОМАРНОСТ (поправка на находка H6): предишната версия правеше
    SELECT last, после UPDATE last+1 без изрична заключваща транзакция —
    две едновременни заявки (реалистично в мрежов режим с няколко
    служители) можеха и двете да прочетат същия "last" ПРЕДИ което и да е
    от двете да commit-не своя UPDATE, да изчислят еднакъв следващ номер,
    и втората да загуби документа си при INSERT (UNIQUE(barcode) гърми).

    Тук изрично започваме транзакцията с BEGIN IMMEDIATE (ако тази връзка
    вече не е в транзакция — виж own_transaction по-долу), което взима
    писателски (RESERVED) катинар ВЕДНАГА, преди SELECT-а. Втора успоредна
    връзka, опитваща се да направи същото, изчаква (до timeout-а на
    връзката — виж get_db) вместо да чете остарялата стойност едновременно.
    При "database is locked"/"busy" грешка пробваме отново с кратка пауза,
    вместо да губим документа на потребителя.

    Транзакцията, започната тук, НЕ се commit-ва в тази функция — тя
    продължава в извикващия код (напр. save_document прави INSERT INTO
    documents на СЪЩАТА връзка и commit-ва накрая), за да остане "запази
    номер + запази документ" една неделима операция, както досега."""
    if doc_type not in DOC_TYPES:
        raise ValueError("Непознат тип документ: %r" % doc_type)
    today = date.today()
    year = today.year
    last_exc = None
    for attempt in range(max_retries):
        own_transaction = not con.in_transaction
        try:
            if own_transaction:
                con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT last FROM counters WHERE doc_type = ? AND year = ?",
                (doc_type, year),
            ).fetchone()
            if row is None:
                seq = 1
                con.execute(
                    "INSERT INTO counters (doc_type, year, last) VALUES (?, ?, 1)",
                    (doc_type, year),
                )
            else:
                seq = row["last"] + 1
                con.execute(
                    "UPDATE counters SET last = ? WHERE doc_type = ? AND year = ?",
                    (seq, doc_type, year),
                )
            number = "%04d/%d" % (seq, year)
            barcode = "%s-%02d%02d%d-%04d" % (
                DOC_TYPES[doc_type]["prefix"], today.day, today.month, today.year, seq,
            )
            return number, year, seq, barcode
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if own_transaction:
                try:
                    con.rollback()
                except sqlite3.OperationalError:
                    pass
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    raise RuntimeError(
        "Не успяхме да генерираме следващия номер — базата данни е заета от "
        "друг едновременен запис (опитайте отново): %s" % last_exc
    )


def get_document_id_by_public_token(con, token):
    """ID на документа с даден public_token, или None — виж
    routes_documents.public_document_view. Нарочно връща само ID (не
    целия ред), извикващият код после минава пак през нормалния
    fetch_document(), за да остане ЕДНО-единствено място, което сглобява
    показваните данни (author join, JSON decode на data), както за
    обичайния преглед."""
    row = con.execute(
        "SELECT id FROM documents WHERE public_token = ?", (token,)
    ).fetchone()
    return row["id"] if row else None


def get_settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


def save_settings(con, values):
    for key, value in values.items():
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_unload_points(con, client_id):
    return con.execute(
        "SELECT * FROM client_unload_points WHERE client_id = ? ORDER BY id",
        (client_id,),
    ).fetchall()


def get_unload_points_map(con, client_ids=None):
    """Всички пунктове за разтоварване, групирани по client_id — за
    еднократно вграждане в JSON списъка с клиенти (за автоматично
    попълване във формите), вместо отделна заявка за всеки клиент."""
    sql = "SELECT * FROM client_unload_points"
    params = ()
    if client_ids is not None:
        client_ids = list(client_ids)
        if not client_ids:
            return {}
        sql += " WHERE client_id IN (%s)" % ",".join("?" * len(client_ids))
        params = tuple(client_ids)
    sql += " ORDER BY id"
    result = {}
    for r in con.execute(sql, params):
        result.setdefault(r["client_id"], []).append(dict(r))
    return result


def save_unload_points(con, client_id, points):
    """Заменя всички пунктове за разтоварване на клиента с подадения
    списък (изтрива старите, вмъква новите) — прост и надежден начин да
    се поддържа неограничен брой редове, подадени от формата като JSON."""
    con.execute("DELETE FROM client_unload_points WHERE client_id = ?", (client_id,))
    for p in points or []:
        if not isinstance(p, dict):
            continue
        row = {k: (p.get(k) or "").strip() for k in
               ("label", "address", "city", "postcode", "country")}
        if not any(row.values()):
            continue
        con.execute(
            "INSERT INTO client_unload_points"
            " (client_id, label, address, city, postcode, country)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (client_id, row["label"], row["address"], row["city"],
             row["postcode"], row["country"]),
        )


THEMES = {
    "light": "Светла (по подразбиране)",
    "dark": "Тъмна",
    "blue": "Синя / корпоративна",
    "green": "Зелена",
    "contrast": "Висок контраст",
    "sepia": "Кафява / топла",
}
DEFAULT_THEME = "light"

# Езици на интерфейса — виж appcore._select_locale() за реда, по който се
# избира: личен избор на потребителя (тук, в user_settings) → избор от
# логин панела ПРЕДИ вход (session, виж routes_auth.login) → BG по
# подразбиране. Печатните документи (ЧМР/опаковъчен лист/...) НЕ се
# засягат от това — те си остават двуезични БГ/EN по закон, независимо от
# избрания език на интерфейса (изрично решение, виж ПЛАН_ЗА_РАЗРАБОТКА.md).
LANGUAGES = {
    "bg": "Български",
    "en": "English",
    "tr": "Türkçe",
}
DEFAULT_LANGUAGE = "bg"


def get_user_settings(con, user_id):
    rows = con.execute(
        "SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_user_theme(con, user_id):
    row = con.execute(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = 'theme'",
        (user_id,),
    ).fetchone()
    theme = row["value"] if row else DEFAULT_THEME
    return theme if theme in THEMES else DEFAULT_THEME


def get_user_language(con, user_id):
    """Личният избор на език на потребителя, или '' ако никога не е
    задаван изрично (за разлика от get_user_theme, тук НЕ връщаме
    подразбиращата се стойност — appcore._select_locale() трябва да може
    да различи „потребителят изрично избра BG“ от „потребителят изобщо
    не е избирал“, за да не презапише избора от логин панела при всеки
    вход с празен избор)."""
    row = con.execute(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = 'language'",
        (user_id,),
    ).fetchone()
    lang = row["value"] if row else ""
    return lang if lang in LANGUAGES else ""


def save_user_settings(con, user_id, values):
    for key, value in values.items():
        con.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, value),
        )
