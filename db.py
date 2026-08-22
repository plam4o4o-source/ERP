# -*- coding: utf-8 -*-
"""База данни (SQLite) на ПачоЛогистик — схема, инициализация и номерация."""
import os
import sqlite3
import secrets
import sys
import time
from datetime import date, datetime

from werkzeug.security import check_password_hash, generate_password_hash

import applog
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

_MOUNTS_PATH = "/proc/mounts"  # изнесено като константа, за да е подменяемо в тест

#: Одит (19.08.2026, находка №47, дребна — втора половина): типове файлови
#: системи, при които SQLite официално предупреждава да НЕ се ползва WAL
#: (разчита на споделена памет между процесите, която мрежовите протоколи
#: не поддържат надеждно).
_NETWORK_FS_TYPES = frozenset((
    "cifs", "smbfs", "smb2", "smb3", "nfs", "nfs4", "afs", "ncpfs",
    "9p", "glusterfs", "ceph", "fuse.sshfs", "fuse.davfs", "davfs",
))


def _is_network_path(path):
    """Одит (19.08.2026, находка №47, дребна): истина ли е, че този път
    стои на МРЕЖОВА файлова система.

    Защо е нужно: `_USE_WAL` по-долу питаше само „базата на
    подразбиращото се място до .exe-то ли е“. Това мълчаливо приемаше, че
    подразбиращото се място е ЛОКАЛНО — а много често срещаната
    инсталация „сложи .exe-то в споделената папка на сървъра и всички го
    пускат оттам“ прави BASE_DIR (значи и подразбиращият се DB_PATH)
    директно върху SMB share. Тогава WAL се включваше точно върху
    мрежовата файлова система — рискът, който else-клонът в get_db()
    цели да избегне.

    Windows: UNC път (`\\\\SERVER\\share\\…`, вкл. разширения префикс
    `\\\\?\\UNC\\…`) или буква на диск, съпоставена към мрежов ресурс
    (`GetDriveTypeW` == DRIVE_REMOTE — точно за случая „.exe-то е на Z:“).
    POSIX: типът на файловата система на най-дългата точка на монтиране,
    покриваща пътя (/proc/mounts).

    Никога не хвърля: при най-малкото съмнение връща False, за да не
    изключим WAL за напълно локална инсталация заради дребна разлика в
    средата (загубата тогава е производителност, не коректност)."""
    try:
        # UNC се разпознава по СУРОВИЯ низ, ПРЕДИ os.path.abspath(): на
        # POSIX abspath би залепил текущата папка отпред и префиксът би
        # изчезнал (важно и за тестовете, които подават Windows път).
        if str(path).startswith("\\\\"):
            return True  # \\SERVER\share\… (включително \\?\UNC\server\share)
        path = os.path.abspath(path)
        if os.name == "nt":
            if path.startswith("//"):
                return True
            drive = os.path.splitdrive(path)[0]
            if not drive:
                return False
            import ctypes  # локален импорт — само на Windows и само тук
            # 4 == DRIVE_REMOTE (мрежов диск, съпоставен с „net use“)
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4
        best_len, best_type = -1, ""
        with open(_MOUNTS_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point, fs_type = parts[1].replace("\\040", " "), parts[2]
                if (path == mount_point or path.startswith(mount_point.rstrip("/") + "/")) \
                        and len(mount_point) > best_len:
                    best_len, best_type = len(mount_point), fs_type
        return best_type.lower() in _NETWORK_FS_TYPES
    except Exception:  # nosec B110 -- диагностична евристика: при неуспех приемаме „локално“ (виж докстринга)
        return False


# WAL се включва само когато базата е на подразбиращото се локално място
# (виж _is_network_path по-горе и коментара в get_db() за причината) —
# изчислено веднъж тук, по същия начин, по който вече се изчислява самият
# DB_PATH (не се очаква да се променя по време на изпълнение без рестарт).
_USE_WAL = (DB_PATH == os.path.join(BASE_DIR, "pacho_logistic.db")
            and not _is_network_path(DB_PATH))
if not _USE_WAL and DB_PATH == os.path.join(BASE_DIR, "pacho_logistic.db"):
    applog.log_warning(
        "db", "базата е на подразбиращото се място, но то е МРЕЖОВО (%s) — "
        "WAL режимът остава изключен нарочно (SQLite не го препоръчва върху "
        "SMB/NFS). Виж одит 19.08.2026, находка №47." % DB_PATH)

# Одит (19.08.2026, находка №2): най-много толкова ЗАЕТИ поредни номера
# прескача next_number, преди да се откаже с ясна грешка — виж пълното
# обяснение там. 1000 е много над всяко реалистично струпване на ръчно
# въведени номера във формата на автоматичните и все пак приключва за
# части от секундата (проста индексирана справка на всеки номер).
_MAX_SEQ_SKIPS = 1000


class NumberingExhaustedError(RuntimeError):
    """Одит (22.08.2026, находка №4): изчерпан таван на прескачането в
    next_number.

    Отделен клас, за да може маршрутът да го разпознае и да покаже на
    оператора КОНКРЕТНОТО съобщение (то обяснява точно какво се е случило и
    какво да направи), вместо генеричното „Възникна неочаквана грешка“.
    Преди това RuntimeError не беше `sqlite3.*`, минаваше по общия клон на
    appcore._handle_unexpected_error и цялото полезно съдържание се губеше —
    тоест находка №2 премести прага от 1 зает номер на 1000, но крайното
    състояние остана същото: типът документ не може да се издава, а
    операторът не научава защо."""

def N_(text):
    """gettext „noop“ маркер — връща низа НЕПРОМЕНЕН, само го прави видим
    за `pybabel extract` (N_ е сред подразбиращите се ключови думи на
    Babel). Нужен е, защото речниците по-долу се строят при ИМПОРТ на
    модула — далеч преди да има заявка и текущ locale, а db.py нарочно не
    зависи от Flask. Реалният превод става на мястото на показване, с
    _() в шаблона (виж documents.html/invoices.html/dashboard.html/
    my_settings.html). Одит (19.08.2026, находка №13)."""
    return text


# Типове документи: префикс за баркода и заглавие на български.
# Одит (19.08.2026, находка №13): заглавията са ИНТЕРФЕЙСЕН текст (колона
# „Вид“ и филтъра в списъците) — при EN/TR интерфейс те оставаха на
# кирилица. Маркирани са с N_() тук и се превеждат с _() в шаблоните.
# ВНИМАНИЕ: износът (име на PDF/Excel файла — routes_documents) нарочно
# продължава да ползва СУРОВОТО българско заглавие, за да не се менят
# имената на вече изнасяни файлове според избрания език на интерфейса.
DOC_TYPES = {
    "cmr": {"prefix": "CMR", "title": N_("ЧМР товарителница")},
    "packing": {"prefix": "OPL", "title": N_("Опаковъчен лист")},
    "pallet": {"prefix": "PAL", "title": N_("Палетна карта")},
    "waybill": {"prefix": "TOV", "title": N_("Товарителница (вътрешен превоз)")},
    "dualuse": {"prefix": "DUD", "title": N_("Декларация за стоки с двойна употреба")},
    "export_it": {"prefix": "EXI", "title": N_("Декларация за износ (Италия)")},
    # Фактури — отделен тип за всяка държава, защото самите бланки се
    # различават по колони и заглавие (виж invoice_br_print.html /
    # invoice_no_print.html): Бразилия е „INVOICE“ с колона Net weight,
    # Норвегия е „COMMERCIAL INVOICE“ с колони Material Description и
    # Pallet Number. Отделните типове дават и отделна номерация на всяка
    # държава, което е и досегашната практика в приложените образци.
    "invoice_br": {"prefix": "INVBR", "title": N_("Фактура за Бразилия")},
    "invoice_no": {"prefix": "INVNO", "title": N_("Фактура за Норвегия")},
    # Дубай: „COMMERCIAL INVOICE“ като Норвегия, но колоните на стоките са
    # НАЙ-простите от трите — HS code, P.O NO, Pos, Material code,
    # Quantity, Unit Price, Total Price. НИТО нето тегло (както Бразилия),
    # НИТО описание/палет № (както Норвегия) — вижте приложения образец
    # 12971.pdf (BBS Bulgaria → ABB INDUSTRIES LLC, Дубай, ОАЕ).
    "invoice_dubai": {"prefix": "INVDU", "title": N_("Фактура за Дубай")},
}

#: Типовете, които са ФАКТУРИ. Заявка: „в раздела Фактури да има издадени
#: документи и само там да се появяват издадените фактури; фактури да не
#: отиват в [Всички документи]“ + „и от таблото/историята на клиента“.
#: Държи се на ЕДНО място, за да не се разминат петте места, които трябва
#: да ги изключват (списък с документи, статистика на таблото, най-активни
#: клиенти, последни документи, история на клиента) — всяко от тях чете
#: оттук, вместо да изброява типовете само́.
INVOICE_DOC_TYPES = ("invoice_br", "invoice_no", "invoice_dubai")


def non_invoice_doc_types():
    """Типовете за общите екрани (всичко без фактурите)."""
    return tuple(k for k in DOC_TYPES if k not in INVOICE_DOC_TYPES)


#: Изглед на анимираната сцена на входния екран (заявка: „подобри
#: анимациите и фонът, направи го реалистично, запази този и добави опция
#: да може да се сменя в настройките“). Пази се в settings (обща за
#: инсталацията — входният екран е ПРЕДИ вход, няма „текущ потребител“,
#: чиято лична настройка да важи), сменя се от Системни настройки.
LOGIN_SCENES = ("realistic", "classic")
DEFAULT_LOGIN_SCENE = "realistic"


def get_login_scene(con):
    """Избраният изглед на входния екран, винаги валидна стойност."""
    scene = get_settings(con).get("login_scene", "")
    return scene if scene in LOGIN_SCENES else DEFAULT_LOGIN_SCENE

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
    alias TEXT NOT NULL DEFAULT '',
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
--
-- Одит (19.08.2026, находка №28б): PRIMARY KEY сравнява ТОЧНО, тоест
-- „abc-77“ и „ABC-77“ бяха два реда с потенциално различно тегло, а
-- materials.lookup и materials.lookup_many връщаха РАЗЛИЧНИ от тях за
-- една и съща фактура. Уникалният индекс по UPPER(code) прави това
-- физически невъзможно, но живее в МИГРАЦИЯ (_m010_materials_code_case_
-- insensitive), а НЕ тук: този скрипт се изпълнява при всеки старт, преди
-- миграциите, така че `CREATE UNIQUE INDEX` върху база с ИСТОРИЧЕСКИ
-- дубликати по регистър би гръмнал още в init_db() — тоест програмата
-- изобщо не би стартирала (във frozen .exe: тиха смърт без прозорец).
-- Миграцията първо събира дубликатите и чак после създава индекса; при
-- чисто нова база тя така или иначе се изпълнява веднага след този скрипт.
CREATE TABLE IF NOT EXISTS materials (
    code TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    net_weight TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Одит (22.08.2026, находка №6, средна): миграцията _m010 по-долу СЛИВА
-- кодовете, различаващи се само по регистър, и ИЗТРИВА излишните редове —
-- необратимо и (доскоро) напълно невидимо за оператора: единствената следа
-- беше ред в `pacho_startup.log`, файл, който потребител на .exe никога не
-- отваря. Проверено с изпълнение: обновяване на реална v3.41 база с
-- „abc-1“/„ABC-1“/„Abc-1“ оставяше ЕДИН ред и нула следи в интерфейса.
-- Тук пазим ПЪЛНО копие на всички варианти от всяка слята група (и
-- запазения, и изтритите), за да може операторът да сравни точно кое НЕТО
-- ТЕГЛО е оцеляло — теглото отива на митническия опаковъчен лист, тоест
-- грешката му не е козметична. Таблицата е малка (само при исторически
-- дубликати) и остава в базата като доказателство, дори след като
-- предупреждението в раздел „Материали“ бъде скрито.
--
-- Отделна таблица (а не колона в `materials`), защото самите изтрити
-- редове вече ги няма в справочника — това е архив на СЛУЧИЛОТО СЕ, не
-- част от действащия справочник.
CREATE TABLE IF NOT EXISTS materials_merged_backup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- UPPER(code) на групата — по него се групират вариантите при показване.
    code_upper TEXT NOT NULL,
    code TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    net_weight TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    -- 1 = този вариант е ОСТАНАЛ в справочника; 0 = изтрит от миграцията.
    kept INTEGER NOT NULL DEFAULT 0,
    -- 1 = вариантите в групата се различаваха по НЕТО ТЕГЛО (по-опасният
    -- случай: оцелялото тегло определя какво пише на бланката).
    weight_conflict INTEGER NOT NULL DEFAULT 0,
    merged_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_materials_merged_backup_code
    ON materials_merged_backup(code_upper);

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


def _ci_contains(haystack, needle):
    """Одит (находка В7, висок риск): SQLite вградената LIKE/COLLATE
    NOCASE НЕ сгъва регистъра на кирилски букви (само ASCII A-Z/a-z) —
    търсене на „иван“ не намираше запазено „Иван“ навсякъде из
    приложението (документи, фактури, история на клиент, справочник
    материали). Python-ото `str.lower()` сгъва Unicode коректно (вкл.
    кирилица), затова регистрираме тази функция чрез
    sqlite3.Connection.create_function и я ползваме В SQL заявката вместо
    `LIKE '%...%'` — филтрирането пак се случва в SQLite (не пренасяме
    цялата таблица в Python), само сравнението на регистъра минава през
    правилната Unicode логика.

    `deterministic=True` при регистрацията е само подсказка за оптимизатора
    (позволява ползване в индекси/generated columns) — тук просто позволява
    на SQLite да третира резултата като чиста функция на входа, каквато е."""
    if haystack is None or needle is None:
        return False
    return needle.lower() in haystack.lower()


def _ci_lower(text):
    """Одит (16.08.2026, находка №15, регресия от 12.08 находка №5):
    SQLite вграденото `LOWER()` сгъва регистъра само за ASCII A-Z/a-z (виж
    _ci_contains по-горе за същото ограничение при LIKE) — потвърдено:
    `LOWER('Иван') == 'Иван'`, непроменено. „Групирай по клиент“
    (routes_documents.documents(), ORDER BY … LOWER(...) …) вече не
    сгъваше регистъра на кирилски имена на клиенти — документи на „Иван“
    и „иван“ спряха да излизат един до друг, макар преди SQL-базираната
    поправка на находка №5 (когато сортирането ставаше в Python с
    str.lower()) да излизаха. Регистрирана функция по същия модел като
    ci_contains — SQL заявката все още прави сортирането (не Python след
    LIMIT/OFFSET), само сравнението на регистъра минава по Unicode."""
    if text is None:
        return None
    return text.lower()


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
    con.create_function("ci_contains", 2, _ci_contains, deterministic=True)
    con.create_function("ci_lower", 1, _ci_lower, deterministic=True)
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
    else:
        # Одит (находка В13): journal_mode е свойство на самия .db ФАЙЛ, не
        # на връзката — ако базата е била създадена/използвана локално (WAL
        # включен) и после е преместена/пренасочена към мрежов диск (смяна
        # на "Системни настройки" → път на базата), самото пропускане на
        # горния PRAGMA НЕ връща файла обратно към DELETE journal mode: WAL
        # си остава активен завинаги, точно рискът, който този else клон
        # цели да предотврати. Затова тук изрично го връщаме към DELETE,
        # когато базата НЕ е на подразбиращото се локално място.
        try:
            mode = con.execute("PRAGMA journal_mode = DELETE").fetchone()
            # Одит (19.08.2026, находка №47): конверсията НЕ винаги успява —
            # SQLite отказва да смени journal_mode, докато КОЯТО И ДА Е
            # друга връзка е отворена към същия файл (връща текущия режим,
            # без да хвърля изключение). Преди това провалът беше НАПЪЛНО
            # безшумен: базата оставаше в WAL върху мрежов диск неопределено
            # дълго при постоянно застъпващи се заявки — точно рискът, който
            # този клон цели да премахне. Сега поне оставя следа, по която
            # проблемът е диагностируем.
            if mode is not None and str(mode[0]).lower() != "delete":
                applog.log_warning(
                    "db.get_db",
                    "базата е на нестандартно/мрежово местоположение, но НЕ можа "
                    "да бъде върната от WAL към DELETE journal (текущ режим: %s) "
                    "— вероятно има друга отворена връзка. Ще бъде опитано пак "
                    "при следващо отваряне." % (mode[0],))
        except sqlite3.OperationalError:
            pass
    return con


def _harden_secret_key_permissions():
    """0600 (четене/запис само за собственика) върху .secret_key.

    Одит (12.08.2026, находка №1, критична): за разлика от
    secrets_store.py (GitHub токена), този файл преди тази поправка се
    „заздравяваше“ (`os.chmod`) САМО в клона, в който се създава за първи
    път — за вече съществуващ файл (всяка инсталация, обновена от
    по-стара версия, или файл, възстановен от архив) правата никога не се
    проверяваха/коригираха повторно. Потвърдено с PoC: файл с
    подразбиращи се права на ОС (обичайно четим и от други локални
    потребители) позволява на всеки друг локален акаунт да прочете ключа
    и да подпише произволна сесийна бисквитка (вкл. role=admin) без да
    знае никаква парола — пълно поемане на администраторски акаунт.

    Затова сега се извиква безусловно при ВСЯКО извикване на
    get_secret_key(), не само при създаване — „заздравява“ и вече
    съществуващи файлове от по-стари инсталации."""
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass  # напр. файлова система без POSIX права (FAT/exFAT на Windows)


def get_secret_key():
    """Постоянен таен ключ за сесиите, пази се във файл до базата."""
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            _harden_secret_key_permissions()
            return key
    key = secrets.token_hex(32)
    with open(SECRET_PATH, "w", encoding="utf-8") as f:
        f.write(key)
    _harden_secret_key_permissions()
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
        try:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, coldef))
        except sqlite3.OperationalError as exc:
            # Одит (находка К7): check-then-act без обща транзакция — в
            # мрежов режим няколко компютъра могат да стартират програмата
            # почти едновременно, всички да прочетат "колоната липсва" ПРЕДИ
            # който и да е от тях да е изпълнил ALTER TABLE, и после всички
            # да опитат да я добавят. _apply_migrations по-долу вече обгражда
            # ЦЯЛАТА миграционна стъпка в BEGIN IMMEDIATE (сериализира
            # едновременните опити), но тази проверка остава като втора
            # линия защита за случая, в който колоната вече е добавена от
            # друга връзка между PRAGMA table_info и самия ALTER TABLE тук
            # (напр. чужда транзакция, извън тази функция).
            if "duplicate column name" not in str(exc).lower():
                raise


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


@_migration
def _m003_client_alias(con):
    """Заявка: „добави за всеки клиент псевдоним в адресната книга и да
    излиза при избор от падащите менюта псевдонима“ — псевдоним (на
    английски/латиница по избор на оператора) за клиента, ползван (а) в
    падащите менюта за избор на клиент във формите (cmr/packing/pallet/
    waybill/dualuse/export_it — вижте _macros.client_select_options), и
    (б) в името на изтегляния PDF/Excel файл при износ на документ (вижте
    client_export._filename_alias) — за клиент с кирилско/дълго/неудобно
    за файлова система име (напр. с интервали/спец. знаци), псевдонимът
    дава кратко, стабилно, латинско име за файла (напр. „ACME“ вместо
    „Ей Си Ем И Инженеринг ООД“). По избор — празен низ по подразбиране,
    навсякъде другаде пада обратно към пълното име, ако не е попълнен."""
    _ensure_column(con, "clients", "alias", "TEXT NOT NULL DEFAULT ''")


@_migration
def _m004_document_number_unique(con):
    """Одит (12.08.2026, находка №13, средна): преди тази поправка
    `documents.number` нямаше НИКАКВО уникално ограничение на ниво база —
    само предупреждение (`_warn_if_number_already_used` в
    routes_documents.py), което ЧЕТЕ базата ПРЕДИ `save_document` да вземе
    писателския катинар. Двама служители, подаващи почти едновременно
    документ/фактура със същия ръчен номер, минаваха проверката и двамата
    (класически TOCTOU race) — записът позволяваше дублиран `number` за
    един и същ `doc_type`.

    Тази миграция добавя `CREATE UNIQUE INDEX` на (doc_type, number),
    което прави дублирането физически невъзможно на ниво SQLite (втората
    INSERT/транзакция гърми с IntegrityError вместо тихо да мине) —
    затваря race прозореца изцяло, не само стеснява го.

    Безопасно за вече съществуващи бази с ИСТОРИЧЕСКИ дублирани номера
    (напр. от преди тази версия, или легитимно сторниране/преиздаване със
    същия номер, което предупреждението по-горе изрично allow-ваше): ако
    CREATE UNIQUE INDEX би гръмнала заради съществуващ дубликат, тук НЕ
    спираме стартирането на програмата (а би било реален риск —
    ALTER/CREATE INDEX в същата миграционна транзакция като всичко
    останало) — вместо това логваме предупреждение с точния брой засегнати
    двойки и продължаваме БЕЗ индекса; migration остава маркирана като
    приложена (идемпотентна), затова индексът не се опитва отново на
    всяко следващо стартиране. Администраторът вижда предупреждението в
    applog и може да почисти историческите дубликати ръчно, след което
    следващият ъпдейт на програмата (нова миграционна стъпка) би могъл да
    добави индекса — засега `_warn_if_number_already_used` си остава
    активна като допълнителна (по-слаба) защита за именно тези бази."""
    dupes = con.execute(
        "SELECT doc_type, number, COUNT(*) AS c FROM documents"
        " GROUP BY doc_type, number HAVING c > 1"
    ).fetchall()
    if dupes:
        applog.log_warning(
            "db._m004_document_number_unique",
            "пропуснато добавяне на UNIQUE(doc_type, number) — намерени %d "
            "съществуващи дублирани двойки в documents (напр. %s/%s). "
            "Прегледайте ги ръчно; предупреждението при ръчно въвеждане на "
            "номер си остава активна защита междувременно." % (
                len(dupes), dupes[0]["doc_type"], dupes[0]["number"]),
        )
        return
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_type_number"
               " ON documents(doc_type, number)")


@_migration
def _m005_document_number_unique_per_year(con):
    """Одит (16.08.2026, находка №16, средна — страничен ефект от находка
    №13/_m004 по-горе): UNIQUE(doc_type, number) БЕЗ компонент „година“
    блокираше два реални, легитимни сценария:

    1. Годишно рестартираща номерация — практиката „номерацията започва
       отначало всяка година“ (ръчен номер „125“ през 2026 и пак „125“
       през 2027 за същия doc_type) се отхвърляше с подвеждащото „номерът
       е зает от друг документ“, макар да са в РАЗЛИЧНИ години.
    2. Празен ръчен номер пада към автоматичния формат `%04d/%d` (номер +
       година) на брояча за същия doc_type — ако оператор ПО-РАНО е
       въвел ръчно номер, съвпадащ буквално с бъдещ автоматичен низ
       (напр. „0005/2026“), броячът, стигайки до seq=5 ПРЕЗ СЪЩАТА 2026
       година, се сблъскваше с него.

    `documents.year` е ВИНАГИ годината, в която документът РЕАЛНО е
    записан (db.next_number по-горе я връща и я пазим дори при ръчен
    номер — вижте appcore.save_document) — НЕ се извлича от самия низ на
    номера, затова е надежден, независим компонент за индекса. Заменя
    предишния (doc_type, number) индекс с (doc_type, year, number) —
    същата защита от състезание при записване, само с правилния обхват:
    уникално В РАМКИТЕ на годината, не завинаги.

    Безопасно за вече съществуващи бази по същия начин като _m004 по-горе
    (виж докстринга ѝ) — исторически дублирани (doc_type, year, number)
    двойки НЕ спират стартирането, а само пропускат създаването на
    индекса с предупреждение в лога."""
    con.execute("DROP INDEX IF EXISTS idx_documents_type_number")
    dupes = con.execute(
        "SELECT doc_type, year, number, COUNT(*) AS c FROM documents"
        " GROUP BY doc_type, year, number HAVING c > 1"
    ).fetchall()
    if dupes:
        applog.log_warning(
            "db._m005_document_number_unique_per_year",
            "пропуснато добавяне на UNIQUE(doc_type, year, number) — намерени "
            "%d съществуващи дублирани двойки в documents (напр. %s/%s/%s). "
            "Прегледайте ги ръчно; предупреждението при ръчно въвеждане на "
            "номер си остава активна защита междувременно." % (
                len(dupes), dupes[0]["doc_type"], dupes[0]["year"], dupes[0]["number"]),
        )
        return
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_type_year_number"
               " ON documents(doc_type, year, number)")


@_migration
def _m006_document_version(con):
    """Одит (16.08.2026, находка №39, средна): преди тази поправка две
    едновременни редакции на СЪЩИЯ документ (напр. двама оператори,
    отворили едновременно старата версия на формата) просто се
    презаписваха взаимно — последният записал „печели“ тихо, без никакво
    предупреждение, а промените на първия изчезваха безследно.

    Добавя оптимистично заключване: `version` се увеличава с 1 при всяка
    успешна редакция (виж routes_documents.edit_document); формата носи
    версията, заредена при отварянето ѝ (скрито поле `edit_doc_version`
    — вижте всеки *_form.html), и при запис се сверява със ТЕКУЩАТА
    версия в базата — при разминаване (друг вече е записал междувременно)
    операторът получава ясно предупреждение вместо тихо презаписване."""
    _ensure_column(con, "documents", "version", "INTEGER NOT NULL DEFAULT 1")


@_migration
def _m007_session_epoch(con):
    """Одит (16.08.2026, находка №5, висока): appcore._session_user_
    deactivated_or_missing (виж находка В3) вече прекратява сесията, ако
    потребителят е деактивиран/изтрит — но НЕ и при обикновена смяна на
    паролата. Ако сесийна бисквитка е открадната (напр. споделен/публичен
    компютър, XSS в трета страна, физически достъп до отключено устройство)
    и собственикът реагира по единствения начин, който познава — смяна на
    паролата, — крадецът с вече открадната бисквитка преди тази поправка
    оставаше логнат НЕОГРАНИЧЕНО, защото auth решенията гледат само
    session["user_id"]/["role"], презаредени от users, а не някаква версия
    на паролата.

    `session_epoch` се увеличава с 1 при ВСЯКА смяна на паролата (собствена
    — routes_auth.change_password, или от администратор — routes_admin.
    admin_user_password); при вход session["session_epoch"] се запазва
    ТОЧНО каквато е БИЛА при издаването на текущата бисквитка. При всяка
    следваща заявка _session_user_deactivated_or_missing сверява със
    ТЕКУЩАТА стойност в базата — разминаване означава, че паролата е
    сменена СЛЕД издаването на тази бисквитка, и прекратява сесията."""
    _ensure_column(con, "users", "session_epoch", "INTEGER NOT NULL DEFAULT 0")


@_migration
def _m008_documents_created_at_index(con):
    """Одит (16.08.2026, находка №22, дребна): списъкът с документи/
    фактури (routes_documents.documents, routes_invoices.invoices_list) и
    таблото (routes_dashboard._dashboard_stats) филтрират по d.created_at
    при всяко зареждане — без индекс всяка от тези заявки е пълно
    сканиране на цялата таблица `documents`. Заедно с тази миграция вижте
    и промяната в самите заявки (routes_dashboard.py/routes_documents.py/
    routes_invoices.py): преди бяха обвити в `date(created_at) >= date(?)`
    — функция върху САМАТА КОЛОНА прави израза НЕ-sargable дори С индекс
    (SQLite не може да ползва индекс, ако трябва да изчисли функция за
    ВСЕКИ ред, преди да сравни) — затова индексът тук е полезен само СЛЕД
    като заявките вече сравняват directno върху текста на колоната."""
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_created_at"
               " ON documents(created_at)")


@_migration
def _m009_public_token_expiry(con):
    """Одит (19.08.2026, находка №20, средна): срок на публичния QR адрес.

    До тази миграция `/p/<token>` беше ВЕЧЕН и неотменяем: човек, сканирал
    бланката веднъж (шофьор, спедитор, външен склад), продължаваше да вижда
    документа — при това ЖИВО, включително всички по-късни редакции —
    завинаги. Единственият начин за отнемане на достъпа беше изтриване на
    целия документ.

    `public_token_expires_at` е TEXT (ISO дата-час, като всички останали
    времена в схемата). NULL означава „безсрочен“ — точно поведението на
    вече издадените документи, за да не спрем изведнъж QR кодове върху
    бланки, които вече са в движение при клиенти. Новите документи получават
    срок (виж appcore.save_document / PUBLIC_TOKEN_TTL_DAYS)."""
    _ensure_column(con, "documents", "public_token_expires_at", "TEXT")


#: Одит (22.08.2026, находка №6): ключът в `settings`, с който миграцията
#: _m010 съобщава на интерфейса, че е слял (и изтрил) редове от справочника
#: материали. Стойността е моментът на сливането; отсъствието на ключа
#: означава „няма непрочетено предупреждение“ — виж merged_materials_notice
#: и routes_materials.materials_list.
MATERIALS_MERGE_NOTICE_KEY = "materials_merge_notice"


@_migration
def _m010_materials_code_case_insensitive(con):
    """Одит (19.08.2026, находка №28б, средна): `materials.code` е PRIMARY
    KEY, а сравнението в SQLite е ТОЧНО — тоест „abc-77“ и „ABC-77“ живеят
    като ДВА отделни реда с потенциално различно тегло. Двата пътя за
    четене на справочника обаче се разминават: `materials.lookup` търси
    първо ТОЧНО съвпадение (връща реда, въведен ръчно), а
    `materials.lookup_many` пита с `UPPER(code) IN (…)` и слива вариантите
    в един речник (`by_upper[...] = r` — печели ПОСЛЕДНИЯТ прочетен ред).
    Резултат: един и същ код, попълнен веднъж ръчно и веднъж през Excel
    импорта, дава РАЗЛИЧНИ килограми в ЕДНА И СЪЩА фактура — а теглото
    отива на официална бланка и в митническия опаковъчен лист.

    Тук се събират вече съществуващите варианти по горен регистър (пази се
    НАЙ-СКОРО обновеният ред — той отразява последния зареден ценоразпис) и
    се добавя УНИКАЛЕН индекс по `UPPER(code)`, така че разминаването да е
    физически невъзможно занапред. Заедно с тази миграция виж и
    `materials.replace_catalog`, която вече обновява СЪЩЕСТВУВАЩИЯ ред при
    разлика само в регистъра, вместо да вмъква втори.

    Безопасно за вече съществуващи бази по СЪЩИЯ модел като _m004/_m005
    по-горе: ако индексът по някаква причина не може да се създаде, не
    спираме стартирането на програмата — логваме и продължаваме."""
    # Одит (22.08.2026, находка №6, средна) — ДВЕ поправки в тялото на тази
    # (иначе непроменена) стъпка:
    #
    # (а) Критерият „най-скоро обновеният“ на практика НЕ работеше.
    #     `updated_at` е DEFAULT datetime('now','localtime') — СЕКУНДНА
    #     точност, а типичният справочник е зареден с ЕДИН Excel импорт,
    #     тоест ВСИЧКИ редове носят една и съща стойност. При равенство
    #     решаваше единствено `rowid DESC`, тоест „последно вмъкнатият“ —
    #     напълно произволен избор по отношение на КАЧЕСТВОТО на данните.
    #     Оцелялото НЕТО ТЕГЛО отива на митническия опаковъчен лист.
    #     Сега при равен `updated_at` предпочитаме реда, който реално НОСИ
    #     информация (непразно тегло, после непразно описание) — празният
    #     ред е строго по-лош избор от попълнения при какъвто и да е
    #     сценарий. Чак накрая, ако и това е равно, остава `rowid DESC`
    #     („последният ред от файла надделява“ — същото правило като в
    #     materials.replace_catalog, вече като СЪЗНАТЕЛЕН избор, а не като
    #     единствен фактически критерий).
    #
    # (б) Изтриването беше необратимо и невидимо (виж
    #     materials_merged_backup в SCHEMA по-горе). Пазим копие на ВСИЧКИ
    #     варианти от всяка група ПРЕДИ DELETE и вдигаме флаг в `settings`,
    #     който раздел „Материали“ показва като видимо предупреждение (виж
    #     merged_materials_notice/routes_materials.materials_list).
    dupes = con.execute(
        "SELECT UPPER(code) AS u, COUNT(*) AS c FROM materials"
        " GROUP BY UPPER(code) HAVING c > 1"
    ).fetchall()
    weight_conflicts = 0
    for row in dupes:
        variants = con.execute(
            "SELECT code, description, net_weight, updated_at FROM materials"
            " WHERE UPPER(code) = ?"
            # (а): истинска подредба по полезност, не по случаен ред на вмъкване.
            " ORDER BY updated_at DESC,"
            "         CASE WHEN TRIM(net_weight) <> '' THEN 0 ELSE 1 END,"
            "         CASE WHEN TRIM(description) <> '' THEN 0 ELSE 1 END,"
            "         rowid DESC",
            (row["u"],),
        ).fetchall()
        if not variants:
            continue
        keep_code = variants[0]["code"]
        # По-опасният случай: вариантите носят РАЗЛИЧНО непразно тегло —
        # тогава сливането реално сменя число на официална бланка, а не
        # просто маха дублиран запис.
        weights = {(v["net_weight"] or "").strip() for v in variants}
        weights.discard("")
        conflict = 1 if len(weights) > 1 else 0
        weight_conflicts += conflict
        for v in variants:
            con.execute(
                "INSERT INTO materials_merged_backup"
                " (code_upper, code, description, net_weight, updated_at, kept,"
                "  weight_conflict) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["u"], v["code"], v["description"], v["net_weight"],
                 v["updated_at"], 1 if v["code"] == keep_code else 0, conflict),
            )
        con.execute("DELETE FROM materials WHERE UPPER(code) = ? AND code <> ?",
                    (row["u"], keep_code))
    if dupes:
        # (б): флагът в `settings` е ЕДИНСТВЕНОТО, което стига до очите на
        # оператора — редът в лога остава само за поддръжката.
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MATERIALS_MERGE_NOTICE_KEY,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        applog.log_warning(
            "db._m010_materials_code_case_insensitive",
            "справочникът материали съдържаше %d кода в няколко варианта по "
            "регистър (напр. „%s“), от които %d с РАЗЛИЧНО нето тегло — оставен е "
            "по един ред за всеки, копие на всички варианти е запазено в "
            "materials_merged_backup, а раздел „Материали“ ще покаже "
            "предупреждение." % (len(dupes), dupes[0]["u"], weight_conflicts),
        )
    try:
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_code_upper"
                    " ON materials(UPPER(code))")
    except sqlite3.OperationalError:
        # Много стар SQLite без индекси по израз — справочникът си остава
        # защитен от replace_catalog (нормализацията при зареждане).
        applog.log_warning(
            "db._m010_materials_code_case_insensitive",
            "този SQLite не поддържа уникален индекс по израз (UPPER(code)) — "
            "нормализацията при зареждане на справочника остава единствената защита")


def _apply_migrations(con):
    """Прилага непроменените миграционни стъпки — вижте MIGRATIONS/
    _migration по-горе за общото обяснение.

    Одит (находки К7/С13), две отделни поправки тук:

    1. **Конкурентност (К7)**: цялата поредица от неприложени стъпки сега
       минава в ЕДНА транзакция с BEGIN IMMEDIATE — взима писателски
       катинар ВЕДНАГА (както next_number по-долу), вместо всяка стъпка да
       работи с автоматично комитнати отделни заявки. Втори процес
       (реалистично: няколко компютъра, стартиращи почти едновременно
       програмата в мрежов режим), опитващ същото, изчаква на катинара
       (до busy_timeout-а от get_db) вместо да чете междинно състояние и да
       се опита да добави същата колона паралелно (_ensure_column вече има
       и собствена защита за duplicate column, но сериализирането тук е
       по-силната гаранция — предотвратява проблема, вместо само да
       преживява симптома му).

    2. **Защита от връщане назад (С13)**: PRAGMA user_version вече се
       записва само ако новата стойност е СТРОГО по-голяма от текущата.
       Преди тази поправка редът беше безусловен — стара версия на
       програмата (по-малко познати стъпки), пусната върху база, вече
       обновена от по-нова версия, би върнала user_version НАЗАД, а при
       следващото стартиране на актуалната версия миграциите след тази
       точка биха се опитали да се приложат повторно. Днешните стъпки са
       идемпотентни (безобидно), но е капан за първата бъдеща стъпка,
       която не е (напр. преизчисление/backfill с натрупващ ефект)."""
    own_transaction = not con.in_transaction
    if own_transaction:
        con.execute("BEGIN IMMEDIATE")
    try:
        applied = con.execute("PRAGMA user_version").fetchone()[0]
        for step_number, step in enumerate(MIGRATIONS, start=1):
            if step_number > applied:
                step(con)
        if MIGRATIONS and len(MIGRATIONS) > applied:
            # SQLite не поддържа bound параметри в PRAGMA — числото идва
            # само от len() тук (не от потребителски вход), безопасно е за
            # форматиране.
            con.execute("PRAGMA user_version = %d" % len(MIGRATIONS))
    except Exception:
        if own_transaction:
            con.rollback()
        raise
    else:
        if own_transaction:
            con.commit()


def unique_number_index_missing(con):
    """Одит (19.08.2026, находка №46, втора половина): истина ли е, че
    уникалният индекс (doc_type, year, number) все още липсва. Изнесено
    като самостоятелна функция, защото освен при старт въпросът се задава
    и от „Настройки“ (виж routes_settings.my_settings) — администраторът
    трябва да ВИДИ, че инсталацията работи без тази защита, а не само да
    има ред в лог файл, който потребител на .exe никога не отваря."""
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index'"
        " AND name='idx_documents_type_year_number'").fetchone() is None


def duplicate_number_rows(con, limit=20):
    """Дублираните (вид, година, номер) — причината индексът да липсва.
    Показват се на администратора, за да знае КОЕ точно да почисти
    (одит 19.08.2026, находка №46)."""
    return con.execute(
        "SELECT doc_type, year, number, COUNT(*) AS c FROM documents"
        " GROUP BY doc_type, year, number HAVING c > 1"
        " ORDER BY c DESC, year DESC, number LIMIT ?", (limit,)).fetchall()


def ensure_unique_number_index(con):
    """Одит (19.08.2026, находка №46): опитва да създаде липсващия уникален
    индекс (doc_type, year, number) при ВСЯКО стартиране, не само веднъж.

    Миграцията `_m005` съзнателно ПРОПУСКА индекса, ако в базата вече има
    исторически дубликати (за да не блокира стартирането) — но тя се
    изпълнява само веднъж и `user_version` остава 8 завинаги. Тоест след
    като администраторът почисти дубликатите, индексът НИКОГА не се
    опитваше отново: инсталацията оставаше трайно без защитата от
    състезание при записване, при това напълно тихо.

    Евтино е: `CREATE UNIQUE INDEX IF NOT EXISTS` при вече съществуващ
    индекс е no-op, а справката за дубликати минава по същия индекс.
    Връща True, ако индексът съществува след извикването."""
    if not unique_number_index_missing(con):
        return True
    dupes = duplicate_number_rows(con, limit=1000)
    if dupes:
        applog.log_warning(
            "db.ensure_unique_number_index",
            "уникалният индекс (doc_type, year, number) все още липсва — в "
            "базата има %d дублирани двойки (напр. %s/%s/%s). Индексът ще бъде "
            "създаден автоматично при следващото стартиране, след като бъдат "
            "почистени." % (len(dupes), dupes[0]["doc_type"], dupes[0]["year"],
                            dupes[0]["number"]))
        return False
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_type_year_number"
               " ON documents(doc_type, year, number)")
    con.commit()
    applog.log_warning(
        "db.ensure_unique_number_index",
        "уникалният индекс (doc_type, year, number) беше създаден допълнително "
        "— дублиранията, попречили при миграцията, вече са отстранени.")
    return True


def init_db():
    con = get_db()
    con.executescript(SCHEMA)
    _apply_migrations(con)
    ensure_unique_number_index(con)  # находка №46 — виж по-горе
    # Първоначален администраторски акаунт — паролата е публично позната
    # (документирана в README/release бележките), затова задължаваме смяна
    # ѝ веднага при първия вход (виж must_change_password по-горе).
    row = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    if row["c"] == 0:
        # Одит (19.08.2026, находка №22): `INSERT OR IGNORE`, не гол INSERT.
        # Тази проверка-и-вмъкване е ИЗВЪН транзакцията на _apply_migrations,
        # тоест при първи старт на няколко компютъра едновременно срещу обща
        # (мрежова) чисто нова база всички виждат count=0 и всички опитват
        # да вмъкнат 'admin'. Проверено с изпълнение: от 5 синхронно
        # стартирани процеса 1 успяваше, а 4 умираха с
        # „UNIQUE constraint failed: users.username“ — тоест програмата
        # изобщо не стартираше на 4 от 5 машини, при това в компилирания
        # .exe без прозорец и без съобщение (само traceback в
        # pacho_startup.log). Загубилият състезанието просто не прави нищо —
        # редът вече съществува, което е точно желаният краен резултат.
        con.execute(
            "INSERT OR IGNORE INTO users"
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
    last_exc = None
    for attempt in range(max_retries):
        # Одит (16.08.2026, находка №35, дребна): преди тази поправка
        # `today = date.today()` се изчисляваше ЕДИНСТВЕН ПЪТ, ПРЕДИ целия
        # цикъл за повторни опити — при "database is locked/busy" и
        # няколко последователни опита (sleep(0.05*(attempt+1)) между тях,
        # до ~1.8с общо за max_retries=8) датата оставаше ЗАМРАЗЕНА от
        # самото начало, докато INSERT INTO documents (в извикващия код,
        # appcore.save_document, СЛЕД връщането оттук) използва
        # `created_at TEXT DEFAULT (datetime('now','localtime'))` —
        # изчислено от SQLite В МОМЕНТА на самия INSERT, не тук. В
        # практически невъзможния, но не нулев случай на превключване на
        # годината (Нова година в 23:59:59 + точно тогава заключена база)
        # `documents.year` (връщан оттук, използван за УНИКАЛНИЯ индекс
        # (doc_type, year, number) — виж _m005 по-горе) би излязъл от
        # СТАРАТА година, докато `created_at` вече е от НОВАТА — противоречи
        # на установеното правило „year е ВИНАГИ годината, в която записът
        # РЕАЛНО е създаден“. Преизчисляваме `today`/`year` при ВСЕКИ опит,
        # възможно най-близо до самия INSERT.
        today = date.today()
        year = today.year
        own_transaction = not con.in_transaction
        try:
            if own_transaction:
                con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT last FROM counters WHERE doc_type = ? AND year = ?",
                (doc_type, year),
            ).fetchone()
            # Записът в `counters` става веднъж, СЛЕД прескачането на заети
            # номера по-долу (upsert) — иначе при прескачане броячът щеше да
            # остане на старата стойност и следващият документ пак щеше да
            # мине през същия цикъл.
            seq = 1 if row is None else row["last"] + 1
            # Одит (19.08.2026, находка №2, КРИТИЧНА): прескачане на вече
            # ЗАЕТИ номера, преди да фиксираме брояча.
            #
            # Защо е нужно: appcore.save_document ВИНАГИ вика тази функция
            # (броячът и баркодът се генерират и за фактури), а чак после
            # подменя `number` с ръчно въведения. Тоест ръчен номер във
            # формата на автоматичните („0005/2026“) заема място, за което
            # броячът по-късно ще стигне сам. Тогава INSERT-ът гърми с
            # UNIQUE constraint failed по индекса (doc_type, year, number)
            # от _m005, извикващият прави rollback() — който връща назад И
            # инкремента на брояча, направен ТУК (една и съща транзакция) —
            # и следващият опит генерира ТОЧНО СЪЩИЯ зает номер. Безкрайно.
            # Резултат преди тази поправка: този тип документ не може да
            # бъде издаван автоматично до края на календарната година, при
            # това със съобщение („издаден точно междувременно от друг
            # потребител“), което вини оператора за нещо, което не е правил.
            #
            # Проверката е вътре в BEGIN IMMEDIATE (писателският катинар е
            # наш), значи между проверката и UPDATE-а никой не може да
            # вмъкне същия номер. Таванът от _MAX_SEQ_SKIPS предпазва от
            # безкраен цикъл при патологични данни (напр. база, в която
            # някой е внесъл хиляди ръчни номера във формата на
            # автоматичните) — по-добре ясна грешка, отколкото увиснало
            # приложение.
            skipped = 0
            while con.execute(
                    "SELECT 1 FROM documents WHERE doc_type = ? AND year = ? AND number = ?",
                    (doc_type, year, "%04d/%d" % (seq, year))).fetchone() is not None:
                seq += 1
                skipped += 1
                if skipped > _MAX_SEQ_SKIPS:
                    raise NumberingExhaustedError(
                        "Не може да бъде отреден свободен номер за %s: първите %d "
                        "поредни номера след текущия брояч вече са заети (вероятно "
                        "от ръчно въведени номера във формата на автоматичните). "
                        "Проверете номерацията на този тип документ."
                        % (doc_type, _MAX_SEQ_SKIPS))
            con.execute(
                "INSERT INTO counters (doc_type, year, last) VALUES (?, ?, ?)"
                " ON CONFLICT(doc_type, year) DO UPDATE SET last = excluded.last",
                (doc_type, year, seq),
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
        except BaseException:
            # Одит (22.08.2026, находка №5): rollback при ВСЯКО друго
            # изключение. Прескачането на заети номера (находка №2 от 19.08)
            # може да вдигне NumberingExhaustedError, а той излизаше оттук,
            # ДОКАТО собствената ни `BEGIN IMMEDIATE` транзакция е активна и
            # писателският катинар е взет. В обикновена заявка Flask teardown
            # затваря връзката и го пуска, но `pallet_bulk_issue` ползва ЕДНА
            # връзка за цяла партида, а фоновите нишки (backup) викат
            # db.get_db() изобщо без teardown — там катинарът би останал
            # държан и би блокирал ВСИЧКИ писатели до рестарт.
            if own_transaction:
                try:
                    con.rollback()
                except Exception:  # nosec B110 -- важното е изключението по-долу
                    pass
            raise
    raise RuntimeError(
        "Не успяхме да генерираме следващия номер — базата данни е заета от "
        "друг едновременен запис (опитайте отново): %s" % last_exc
    )


#: Одит (22.08.2026, находка №8): трите възможни изхода на проверката на
#: публичен токен. "missing" = такъв адрес никога не е съществувал (или е
#: изтрит документ); "expired" = документът е налице, но срокът на
#: публичния му адрес е минал; "ok" = достъпът е валиден.
# nosec B105 по-долу (×3): bandit маркира всяка константа, чието ИМЕ съдържа
# „TOKEN“, като възможна хардкодната парола. Тук стойностите са състояния
# („липсва“/„изтекъл“/„валиден“), не тайни — самият токен е случаен и се
# генерира в appcore.save_document.
PUBLIC_TOKEN_MISSING = "missing"  # nosec B105
PUBLIC_TOKEN_EXPIRED = "expired"  # nosec B105
PUBLIC_TOKEN_OK = "ok"  # nosec B105


def get_public_token_status(con, token):
    """(състояние, document_id) за даден public_token.

    Одит (22.08.2026, находка №8, средна): дотук единственият достъп до
    тази проверка беше get_document_id_by_public_token по-долу, който
    връщаше `None` И за непознат, И за ИЗТЕКЪЛ токен — извикващият нямаше
    как да ги различи и показваше гол 404 в двата случая. Резултатът на
    терен: архивна бланка, сканирана шест месеца по-късно при рекламация
    или митническа проверка, дава „страницата не е намерена“, все едно
    документът никога не е съществувал — а човекът с телефона няма как да
    се досети, че просто трябва да поиска нов линк от издателя.

    Разделянето на състоянията е тук (а не в маршрута), защото проверката
    за изтичане е ЕДНА и трябва да остане на едно място; самият маршрут
    решава какво да покаже (виж routes_documents.public_document_view).

    ВАЖНО за поверителността: „expired“ издава само че адресът Е БИЛ
    валиден — не показва нищо от съдържанието на документа (виж
    templates/public_link_expired.html)."""
    row = con.execute(
        "SELECT id, public_token_expires_at FROM documents WHERE public_token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return PUBLIC_TOKEN_MISSING, None
    # Одит (19.08.2026, находка №20): NULL = безсрочен — така остават вече
    # издадените документи отпреди миграция _m009, за да не спрат изведнъж
    # QR кодове върху бланки, които са в движение при клиенти.
    if public_token_is_expired(row["public_token_expires_at"]):
        return PUBLIC_TOKEN_EXPIRED, row["id"]
    return PUBLIC_TOKEN_OK, row["id"]


def public_token_is_expired(expires_at):
    """Изтекъл ли е срок, записан в `documents.public_token_expires_at`.

    Одит (22.08.2026, находка №8): една-единствена реализация на
    сравнението — ползва се и от проверката при публичния преглед, и от
    изгледа на документа, който показва срока на оператора. Сравнението е
    ЛЕКСИКОГРАФСКО върху "ГГГГ-ММ-ДД ЧЧ:ММ:СС", което за този формат
    съвпада с хронологичното (както навсякъде другаде в схемата)."""
    return bool(expires_at) and expires_at < datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_document_id_by_public_token(con, token):
    """ID на документа с даден public_token, или None — виж
    routes_documents.public_document_view. Нарочно връща само ID (не
    целия ред), извикващият код после минава пак през нормалния
    fetch_document(), за да остане ЕДНО-единствено място, което сглобява
    показваните данни (author join, JSON decode на data), както за
    обичайния преглед.

    Изтекъл токен се държи като несъществуващ (None) — за извикващи,
    които не се интересуват ЗАЩО достъпът е отказан. Който трябва да
    различи двата случая, ползва get_public_token_status по-горе."""
    status, doc_id = get_public_token_status(con, token)
    return doc_id if status == PUBLIC_TOKEN_OK else None


def get_settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


def save_settings(con, values):
    for key, value in values.items():
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def merged_materials_notice(con):
    """Одит (22.08.2026, находка №6): групите слети (и изтрити) кодове от
    справочника материали, ако предупреждението още не е потвърдено от
    оператора — иначе празен списък.

    Всяка група е речник:
      ``{"code_upper", "kept", "removed", "weight_conflict", "merged_at"}``,
    където ``kept`` е оцелелият ред, а ``removed`` — изтритите варианти
    (всички като sqlite3.Row с code/description/net_weight/updated_at).

    Причината да живее ТУК, а не в routes_materials: миграцията, която
    създава записите, също е в този модул — двете страни на едно и също
    решение стоят на едно място. Отделно така тестовете могат да проверят
    поведението без Flask контекст."""
    if MATERIALS_MERGE_NOTICE_KEY not in get_settings(con):
        return []
    rows = con.execute(
        "SELECT code_upper, code, description, net_weight, updated_at, kept,"
        " weight_conflict, merged_at FROM materials_merged_backup"
        " ORDER BY weight_conflict DESC, code_upper, kept DESC, code"
    ).fetchall()
    groups = {}
    order = []
    for r in rows:
        key = r["code_upper"]
        if key not in groups:
            groups[key] = {"code_upper": key, "kept": None, "removed": [],
                           "weight_conflict": bool(r["weight_conflict"]),
                           "merged_at": r["merged_at"]}
            order.append(key)
        if r["kept"]:
            groups[key]["kept"] = r
        else:
            groups[key]["removed"].append(r)
    return [groups[k] for k in order]


def dismiss_merged_materials_notice(con):
    """Скрива предупреждението от находка №6, след като операторът изрично
    е потвърдил, че го е видял. Изтрива САМО флага — самите копия в
    `materials_merged_backup` остават в базата завинаги (те са
    доказателството кое тегло е било изтрито; вижте коментара при
    таблицата в SCHEMA)."""
    con.execute("DELETE FROM settings WHERE key = ?", (MATERIALS_MERGE_NOTICE_KEY,))


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


# Одит (19.08.2026, находка №13): имената на темите са интерфейсен текст
# (виж my_settings.html) — маркирани с N_(), превеждат се с _() в шаблона.
THEMES = {
    "light": N_("Светла (по подразбиране)"),
    "dark": N_("Тъмна"),
    "blue": N_("Синя / корпоративна"),
    "green": N_("Зелена"),
    "contrast": N_("Висок контраст"),
    "sepia": N_("Кафява / топла"),
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
