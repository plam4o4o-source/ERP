# -*- coding: utf-8 -*-
"""Преход от старите технически имена („PachoLogistic“/„pacho_…“) към новите
(„PHLogistics“/„ph_…“).

Одит (06.10.2026): търговското име е „PH Logistics“ от v3.78.0, а файловете и
папките още носеха старото. Тук са:

* имената на файловете с данни (нови и стари) и правилото за избор между тях
  — ЧЕТЕМ И ДВЕТЕ ЗАВИНАГИ: мрежовите/преносимите инсталации (.exe-то в
  споделена папка, `db_path` към мрежов диск) остават със старите имена,
  защото никой не може безопасно да ги преименува вместо всички компютри;
* еднократното преместване на данните от
  %LOCALAPPDATA%\\Programs\\PachoLogistic в ...\\PHLogistics при първия старт
  на новата инсталация (само локалната инсталация по подразбиране);
* преместването на потребителската папка %LOCALAPPDATA%\\PachoLogistic.

Правилото при съмнение: данните НЕ се местят — новата инсталация получава
ph_config.json, който сочи към базата в старата папка („указател“).

Одит (07.10.2026): довършване на прехода —
* преносима инсталация (.exe извън двете папки по подразбиране) преименува
  файловете си НА МЯСТО (migrate_portable) — само при същите проверки за
  безопасност; в споделена/мрежова папка нищо не се пипа;
* споделена база (изричен db_path): самовъзстановяване на пътя, ако базата
  е преименувана от друг компютър (heal_db_path) — никога нова празна база;
* остатъците от старото име на тази машина (cleanup_legacy_leftovers).

Само стандартна библиотека: модулът се вика най-отгоре в app.py, ПРЕДИ да са
внесени config/db/applog (config изчислява CONFIG_PATH при импорт). Затова тук
няма print/лог — резултатът е речник (и ph_migration.json), който app.py
отпечатва след като логът е отворен.
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta

from version import INSTALL_DIR_NAME, LEGACY_EXE_NAME, LEGACY_INSTALL_DIR_NAME

CONFIG_NAME = "ph_config.json"
LEGACY_CONFIG_NAME = "pacho_config.json"
DB_NAME = "ph_logistics.db"
LEGACY_DB_NAME = "pacho_logistic.db"
SECRET_NAME = ".secret_key"  # nosec B105 -- име на файл, не парола
ATTACHMENTS_NAME = "attachments"
#: Отчет за прехода (и дневник по време на преместването) — в новата папка.
REPORT_NAME = "ph_migration.json"
#: Одит (07.10.2026): придружаващите файлове на базата — едно семейство.
DB_SIDECARS = ("-journal", "-wal", "-shm")
#: Одит (07.10.2026): бележка до споделената база, че администраторът я е
#: преименувал (pacho_logistic.db → ph_logistics.db) — виж heal_db_path.
SHARED_RENAME_MARKER = "ph_shared_rename.json"
#: Одит (07.10.2026): логове от старото име, които преносимата инсталация
#: преименува на място (pacho_startup*.log, pacho_update.log …).
_LEGACY_LOG_RE = re.compile(r"^pacho_[^\\/]*\.log(?:\.\d+)?$")
#: Одит (07.10.2026): потребителската папка съдържа само профила на
#: вградения прозорец — само такава стара папка се маха (виж desktop.py).
_USER_DIR_KNOWN = frozenset(("webview", "appwindowprofile"))
#: Колко дни без промяна правят старата потребителска папка „изоставена“.
STALE_DAYS = 30

#: Наличието на кое да е от тези означава „в папката има данни“.
_DATA_MARKERS = (CONFIG_NAME, LEGACY_CONFIG_NAME, DB_NAME, LEGACY_DB_NAME,
                 SECRET_NAME, ATTACHMENTS_NAME)

#: Маркерите за насрочено възстановяване (backup.py) — двете имена.
_RESTORE_MARKERS = ("ph_restore_request.json", "pacho_restore_request.json")

_UNINS_RE = re.compile(r"^unins\d{3}\.(?:exe|dat|msg)$", re.IGNORECASE)

SHARES_KEY = r"SYSTEM\CurrentControlSet\Services\LanmanServer\Shares"

_runtime = {"db_override": None, "result": None}


# ---------------------------------------------------------------- имена

def resolve_existing(base_dir, new_name, legacy_name):
    """Новото име, ако файлът съществува; иначе старото, ако съществува;
    иначе новото (нов файл се създава само с новото име)."""
    new_path = os.path.join(base_dir, new_name)
    if os.path.exists(new_path):
        return new_path
    legacy_path = os.path.join(base_dir, legacy_name)
    if os.path.exists(legacy_path):
        return legacy_path
    return new_path


def resolve_config_path(base_dir):
    return resolve_existing(base_dir, CONFIG_NAME, LEGACY_CONFIG_NAME)


def resolve_default_db_path(base_dir):
    return resolve_existing(base_dir, DB_NAME, LEGACY_DB_NAME)


def runtime_db_override():
    """Одит (06.10.2026): пазач — път до старата база, когато преходът не е
    успял да запише дори указателя (ph_config.json). Тогава config.
    resolve_db_path връща този път вместо да създаде НОВА празна база в новата
    папка. None в нормалния случай."""
    return _runtime["db_override"]


def last_result():
    """Резултатът от run_at_startup в този процес (или None)."""
    return _runtime["result"]


def new_name_for(name):
    """Името на файл от старата папка в новата: pacho_logistic.db(-wal…) →
    ph_logistics.db(-wal…), pacho_config.json → ph_config.json, други
    pacho_* → ph_*; всичко останало остава."""
    for old, new in ((LEGACY_DB_NAME, DB_NAME), ("pacho_logistic_", "ph_logistics_"),
                     ("pacho_config", "ph_config"), ("pacho_", "ph_")):
        if name.startswith(old):
            return new + name[len(old):]
    return name


def is_program_file(name):
    """Програмни файлове (не данни) — не се местят: *.exe, *.exe.old/.new/…,
    деинсталаторът на Inno Setup и скриптовете за обновяване."""
    low = name.lower()
    return (low.endswith(".exe") or ".exe." in low or bool(_UNINS_RE.match(low))
            or (low.endswith(".bat") and low.startswith(("pacho_update", "ph_update"))))


def _is_legacy_program_leftover(name):
    """Каквото и инсталаторът трие от старата папка ([InstallDelete]) —
    само старото .exe с неговите .old/.new копия, деинсталаторът и скриптовете."""
    low = name.lower()
    exe = LEGACY_EXE_NAME.lower()
    return (low == exe or low.startswith(exe + ".") or bool(_UNINS_RE.match(low))
            or (low.endswith(".bat") and low.startswith(("pacho_update", "ph_update"))))


def machine_name():
    """Одит (07.10.2026): името на компютъра — същото, което хешира
    updater._machine_suffix (platform.node, после COMPUTERNAME/HOSTNAME)."""
    name = ""
    try:
        import platform
        name = platform.node() or ""
    except Exception:  # nosec B110 -- пада към променливите на средата
        pass
    return name or os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "local"


def sibling_db_path(path):
    """Одит (07.10.2026): същата папка, другото име на базата
    (pacho_logistic.db ↔ ph_logistics.db) или None за чуждо име."""
    base = os.path.basename(str(path or "")).lower()
    folder = os.path.dirname(str(path or ""))
    if base == LEGACY_DB_NAME:
        return os.path.join(folder, DB_NAME)
    if base == DB_NAME:
        return os.path.join(folder, LEGACY_DB_NAME)
    return None


def family_moves(src_db, dst_db):
    """[(от, към)] за базата и наличните ѝ -journal/-wal/-shm (базата първа)."""
    moves = [(src_db, dst_db)]
    for suffix in DB_SIDECARS:
        if os.path.lexists(src_db + suffix):
            moves.append((src_db + suffix, dst_db + suffix))
    return moves


def replace_all(moves):
    """Одит (07.10.2026): os.replace подред; при грешка връща направеното.
    Никога не презаписва съществуващ файл. (успех, грешка)."""
    done = []
    try:
        for src, dst in moves:
            if os.path.lexists(dst):
                raise FileExistsError("вече съществува: %s" % dst)
            os.replace(src, dst)
            done.append((src, dst))
    except OSError as exc:
        failed = _undo(done)
        if failed:
            return False, "rollback_failed: %s (не се върнаха: %s)" % (exc, failed)
        return False, str(exc)
    return True, None


def db_has_no_user_data(path):
    """Одит (07.10.2026): базата е празна (0 байта, без схема или без нито
    един документ и клиент) — така изглежда база, създадена наново от стара
    версия след преименуването. При съмнение (грешка) — False."""
    try:
        if os.path.getsize(path) == 0:
            return True
        con = sqlite3.connect(path, timeout=2)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in ("documents", "clients"):
                if table in tables and con.execute(
                        "SELECT 1 FROM %s LIMIT 1" % table).fetchone():  # nosec B608 -- имената са от кода
                    return False
            return True
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return False


def _shared_rename_points_to(new_db):
    data = _read_json(os.path.join(os.path.dirname(new_db), SHARED_RENAME_MARKER))
    return bool(data) and str(data.get("to") or "").lower() == os.path.basename(new_db).lower()


def heal_db_path(path):
    """Одит (07.10.2026): (път за ползване, причина) или (None, None).

    * „missing“ — файлът го няма, а другото име в същата папка го има
      (друг компютър е преименувал споделената база): ползва се то;
    * „recreated“ — старото име е създадено наново (празно) от стара версия
      СЛЕД преименуването (бележката SHARED_RENAME_MARKER го потвърждава):
      ползва се новото. Ако старата база има данни — нищо не се сменя.
    Никога не създава файл; при каквато и да е грешка — (None, None)."""
    sibling = sibling_db_path(path)
    if not sibling:
        return None, None
    try:
        if not os.path.exists(path):
            if os.path.isfile(sibling) and os.path.getsize(sibling) > 0:
                return sibling, "missing"
            return None, None
        if (os.path.basename(path).lower() == LEGACY_DB_NAME
                and os.path.isfile(sibling) and os.path.getsize(sibling) > 0
                and _shared_rename_points_to(sibling) and db_has_no_user_data(path)):
            return sibling, "recreated"
    except OSError:
        pass
    return None, None


def has_data(folder):
    return any(os.path.exists(os.path.join(folder, n)) for n in _DATA_MARKERS)


def legacy_names_in_use(config_path, db_path):
    """Тази инсталация работи със старите имена на файловете (мрежова/
    преносима — там те остават)."""
    return (os.path.basename(config_path or "") == LEGACY_CONFIG_NAME
            or os.path.basename(db_path or "") == LEGACY_DB_NAME)


# ---------------------------------------------------------------- пътища

def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path))).rstrip("\\/")


def same_path(a, b):
    return _norm(a) == _norm(b)


def path_is_inside(path, parent):
    """path е самата parent или е вътре в нея (без значение от регистъра на
    буквите под Windows)."""
    p, root = _norm(path), _norm(parent)
    if not root:
        return False
    return p == root or p.startswith(root + os.sep)


def install_dirs(localappdata):
    """(новата, старата) папка на локалната инсталация по подразбиране."""
    programs = os.path.join(localappdata, "Programs")
    return (os.path.join(programs, INSTALL_DIR_NAME),
            os.path.join(programs, LEGACY_INSTALL_DIR_NAME))


def is_legacy_local_exe(exe_path, localappdata):
    """.exe-то работи от старата локална папка по подразбиране
    (%LOCALAPPDATA%\\Programs\\PachoLogistic)."""
    if not exe_path or not localappdata:
        return False
    return same_path(os.path.dirname(os.path.abspath(exe_path)),
                     install_dirs(localappdata)[1])


# ---------------------------------------------------------------- безопасно ли е да се мести

def parse_share_paths(values):
    """Пътищата на споделените папки от стойностите в регистъра
    (LanmanServer\\Shares: REG_MULTI_SZ с редове „Path=…“)."""
    paths = []
    for data in values:
        if isinstance(data, (list, tuple)):
            lines = [str(x) for x in data]
        elif isinstance(data, str):
            lines = re.split(r"[\r\n\x00]+", data)
        else:
            continue
        for line in lines:
            line = line.strip()
            if line[:5].lower() == "path=" and line[5:].strip():
                paths.append(line[5:].strip())
    return paths


def read_share_paths():
    """Споделените папки на този компютър или None, ако регистърът не може
    да бъде прочетен (тогава се приема „не е безопасно“). Липсващ ключ значи
    „няма нито един дял“."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SHARES_KEY)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    values = []
    try:
        index = 0
        while True:
            try:
                _name, data, _kind = winreg.EnumValue(key, index)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 259:  # ERROR_NO_MORE_ITEMS
                    break
                return None
            values.append(data)
            index += 1
    finally:
        winreg.CloseKey(key)
    return parse_share_paths(values)


def is_network_path(path):
    """UNC път или буква на мрежов диск. При съмнение — True (не местим)."""
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(text))[0]
        return bool(drive) and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4
    except Exception:  # nosec B110 -- при съмнение приемаме „мрежов“
        return True


def is_safe_to_move(folder, share_paths_func=None, network_func=None):
    """(True, None) или (False, код на причината)."""
    share_paths_func = share_paths_func or read_share_paths
    network_func = network_func or is_network_path
    if network_func(folder):
        return False, "network"
    shares = share_paths_func()
    if shares is None:
        return False, "registry"
    for share in shares:
        if path_is_inside(folder, share):
            return False, "shared"
    return True, None


def file_in_use(path, retries=3, delay=0.3):
    """Одит (06.10.2026): работещо .exe не може да бъде отворено за запис под
    Windows — така разбираме, че старата версия още работи от старата папка
    (тя отваря базата само по време на заявка, затова заетата база сама по
    себе си не е достатъчен знак). Нищо не се записва."""
    for attempt in range(retries):
        try:
            with open(path, "r+b"):
                return False
        except FileNotFoundError:
            return False
        except OSError:
            if attempt == retries - 1:
                return True
            time.sleep(delay)
    return True


# ---------------------------------------------------------------- помощни

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


#: Одит (07.10.2026): за db.rename_shared_database (бележката до базата).
write_json_atomic = _write_json_atomic


def _read_config(path):
    """(речник или None, повреден ли е)."""
    if not os.path.exists(path):
        return None, False
    data = _read_json(path)
    return data, data is None


def _inspect_db(db_path):
    """(код на причина или None, папката за архив от настройките).

    Отваря базата както самата програма при старт и прехвърля WAL
    съдържанието в основния файл (така се мести един цял файл)."""
    if not os.path.exists(db_path):
        return None, ""
    try:
        con = sqlite3.connect(db_path, timeout=5)
        try:
            folder = ""
            if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                           " AND name = 'settings'").fetchone():
                row = con.execute("SELECT value FROM settings"
                                  " WHERE key = 'backup_folder'").fetchone()
                folder = str((row[0] if row else "") or "").strip()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            con.close()
    except sqlite3.Error:
        return "db_unreadable", ""
    return None, folder


def _restore_marker_inside(legacy_dir):
    for name in _RESTORE_MARKERS:
        data = _read_json(os.path.join(legacy_dir, name))
        if data is None:
            continue
        for key in ("backup", "files_zip"):
            value = str(data.get(key) or "")
            if value and path_is_inside(value, legacy_dir):
                return True
    return False


def plan_names(names):
    """{старо име: име в новата папка} за данните (без програмните файлове).

    Ако старата папка вече съдържа и новото име (старото .exe е обновено на
    място до нов код, който пише ph_startup*.log, ph_update.log…), файлът
    запазва името си — иначе два файла биха се борили за едно име. Базата и
    конфигурацията се решават ЦЯЛО СЕМЕЙСТВО (pacho_logistic.db с -wal/-shm):
    чужд -wal до друга база би я повредил. Правилото „новото име печели“ в
    resolve_existing дава същия избор и в новата папка."""
    data = [n for n in names if not is_program_file(n)]
    present = set(os.path.normcase(n) for n in data)
    keep_db = any(n.startswith(DB_NAME) for n in data)
    keep_cfg = any(n.startswith("ph_config") for n in data)
    result = {}
    for name in data:
        if name.startswith(LEGACY_DB_NAME):
            target = name if keep_db else new_name_for(name)
        elif name.startswith("pacho_config"):
            target = name if keep_cfg else new_name_for(name)
        else:
            target = new_name_for(name)
            if target != name and os.path.normcase(target) in present:
                target = name
        result[name] = target
    return result


def _plan_moves(legacy_dir, new_dir, db_basename):
    """[(от, към)] в безопасен ред — базата (с -wal/-shm) първа — или None
    при конфликт на имената."""
    moves = sorted(plan_names(os.listdir(legacy_dir)).items())
    targets = [os.path.normcase(t) for _n, t in moves]
    if len(set(targets)) != len(targets):
        return None
    for _name, target in moves:
        if os.path.lexists(os.path.join(new_dir, target)):
            return None

    def order(item):
        name = item[0]
        if name == db_basename:
            return (0, name)
        if name.startswith(db_basename):
            return (1, name)
        if name.startswith(("pacho_config", "ph_config")):
            return (2, name)
        if name == SECRET_NAME:
            return (3, name)
        return (4, name)

    moves.sort(key=order)
    return [(os.path.join(legacy_dir, n), os.path.join(new_dir, t)) for n, t in moves]


def _undo(done):
    """Връща преместеното; връща списъка с неуспешно върнатите."""
    failed = []
    for src, dst in reversed(done):
        try:
            if os.path.lexists(dst) and not os.path.lexists(src):
                os.replace(dst, src)
        except OSError:
            failed.append((src, dst))
    return failed


def _base_report(legacy_dir, new_dir, status, **extra):
    report = {"status": status, "legacy_dir": legacy_dir, "new_dir": new_dir,
              "at": _now(), "shown": False}
    report.update(extra)
    return report


# ---------------------------------------------------------------- преместване / указател

def _execute_moves(plan, report_path, legacy_dir, new_dir):
    """(успех, грешка). Преди първото преместване се записва дневник
    (status=in_progress), за да може прекъснат процес (ток) да бъде върнат
    при следващия старт — виж _recover."""
    journal = _base_report(legacy_dir, new_dir, "in_progress",
                           moves=[[s, d] for s, d in plan])
    try:
        _write_json_atomic(report_path, journal)
    except OSError as exc:
        return False, "journal: %s" % exc
    done = []
    try:
        for src, dst in plan:
            os.replace(src, dst)
            done.append((src, dst))
    except OSError as exc:
        failed = _undo(done)
        if failed:
            # Не всичко се върна — дневникът остава in_progress и следващият
            # старт пробва пак да върне; базата се търси там, където е сега.
            return False, "rollback_failed: %s" % exc
        try:
            os.remove(report_path)
        except OSError:
            pass
        return False, str(exc)
    return True, None


def _cleanup_legacy(legacy_dir):
    """След успешно преместване: старите програмни файлове (каквото трие и
    инсталаторът — иначе старото .exe би създало нова празна база в
    празната папка) и самата папка, ако е останала празна."""
    try:
        names = os.listdir(legacy_dir)
    except OSError:
        return False
    for name in names:
        if _is_legacy_program_leftover(name):
            path = os.path.join(legacy_dir, name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
    try:
        os.rmdir(legacy_dir)  # само празна папка
        return True
    except OSError:
        return False


def _point_to_legacy(legacy_dir, new_dir, report_path, legacy_cfg, legacy_db,
                     reason, error=None):
    """Данните остават; новата папка получава ph_config.json с всички стари
    ключове и db_path към старата база (ако вече има db_path — без промяна) и
    копие на .secret_key (сесиите оцеляват). Прикачените файлове и логото
    следват папката на базата сами."""
    cfg = dict(legacy_cfg or {})
    if not str(cfg.get("db_path") or "").strip():
        cfg["db_path"] = os.path.abspath(legacy_db)
    report = _base_report(legacy_dir, new_dir, "pointer", reason=reason,
                          db_path=cfg["db_path"])
    if error:
        report["error"] = str(error)
    try:
        _write_json_atomic(os.path.join(new_dir, CONFIG_NAME), cfg)
    except OSError as exc:
        # Пазач: без указател програмата би създала нова празна база в
        # новата папка — подаваме пътя направо на config.resolve_db_path.
        _runtime["db_override"] = cfg["db_path"]
        report["status"] = "pointer_runtime"
        report["error"] = "%s; config: %s" % (report.get("error") or "", exc)
    secret_src = os.path.join(legacy_dir, SECRET_NAME)
    secret_dst = os.path.join(new_dir, SECRET_NAME)
    if os.path.exists(secret_src) and not os.path.exists(secret_dst):
        try:
            import shutil
            shutil.copyfile(secret_src, secret_dst)
        except OSError:
            pass  # само сесиите се губят — влиза се наново
    try:
        _write_json_atomic(report_path, report)
    except OSError:
        pass
    return report


def _recover(previous, report_path):
    """Прекъснато преместване (status=in_progress): връща всичко обратно в
    старата папка, после решението се взима наново. (успех, неуспешни)."""
    moves = [(s, d) for s, d in previous.get("moves") or []
             if isinstance(s, str) and isinstance(d, str)]
    failed = _undo(moves)
    if not failed:
        try:
            os.remove(report_path)
        except OSError:
            pass
    return not failed, failed


def migrate_install_dir(exe_dir, localappdata, share_paths_func=None,
                        network_func=None, in_use_func=None):
    """Еднократният преход на данните на локалната инсталация. Връща
    отчета (речник) или None, ако няма какво да се прави. Никога не хвърля."""
    if not localappdata or not exe_dir:
        return None
    new_dir, legacy_dir = install_dirs(localappdata)
    if not same_path(exe_dir, new_dir):
        return None
    if not os.path.isdir(legacy_dir):
        return None  # бързият път: една проверка
    try:
        return _migrate(legacy_dir, new_dir, share_paths_func, network_func,
                        in_use_func or file_in_use)
    except Exception as exc:  # непредвидено — пак никога нова празна база
        report_path = os.path.join(new_dir, REPORT_NAME)
        journal = _read_json(report_path) if os.path.exists(report_path) else None
        in_progress = bool(journal and journal.get("status") == "in_progress")
        if in_progress or (has_data(legacy_dir) and not has_data(new_dir)):
            _runtime["db_override"] = _where_is_db(legacy_dir, new_dir)
        report = _base_report(legacy_dir, new_dir, "error", error=repr(exc))
        if not in_progress:  # дневникът трябва на следващия старт (_recover)
            try:
                _write_json_atomic(report_path, report)
            except OSError:
                pass
        return report


def _where_is_db(legacy_dir, new_dir):
    """Базата там, където е в момента (старата папка с предимство)."""
    legacy_db = resolve_default_db_path(legacy_dir)
    if os.path.exists(legacy_db):
        return legacy_db
    new_db = resolve_default_db_path(new_dir)
    return new_db if os.path.exists(new_db) else legacy_db


def _migrate(legacy_dir, new_dir, share_paths_func, network_func, in_use_func):
    report_path = os.path.join(new_dir, REPORT_NAME)
    previous = _read_json(report_path) if os.path.exists(report_path) else None
    if previous and previous.get("status") == "in_progress":
        ok, failed = _recover(previous, report_path)
        if not ok:
            # Част от данните е тук, част — там; базата се търси където е.
            _runtime["db_override"] = _where_is_db(legacy_dir, new_dir)
            return _base_report(legacy_dir, new_dir, "incomplete",
                                error="не се върнаха: %s" % failed)
        previous = None
    if not has_data(legacy_dir):
        return None
    if has_data(new_dir):
        if previous is None:
            # И двете папки имат данни — нищо не пипаме, само казваме веднъж.
            report = _base_report(legacy_dir, new_dir, "both")
            try:
                _write_json_atomic(report_path, report)
            except OSError:
                pass
            return report
        return None

    legacy_cfg, cfg_broken = _read_config(resolve_config_path(legacy_dir))
    legacy_db = resolve_default_db_path(legacy_dir)
    custom_db = str((legacy_cfg or {}).get("db_path") or "").strip()

    safe, reason = is_safe_to_move(legacy_dir, share_paths_func, network_func)
    if safe and cfg_broken:
        reason = "config_unreadable"
    if not reason and custom_db and path_is_inside(custom_db, legacy_dir):
        reason = "db_path_inside"  # db_path, зададен изрично, не се преименува
    if not reason:
        for name in os.listdir(legacy_dir):
            if name.lower().endswith(".exe") and in_use_func(os.path.join(legacy_dir, name)):
                reason = "running"
                break
    if not reason and not custom_db:
        reason, backup_folder = _inspect_db(legacy_db)
        if not reason and backup_folder and path_is_inside(backup_folder, legacy_dir):
            reason = "backup_inside"
    if not reason and _restore_marker_inside(legacy_dir):
        reason = "restore_inside"
    plan = None
    if not reason:
        plan = _plan_moves(legacy_dir, new_dir, os.path.basename(legacy_db))
        if plan is None:
            reason = "conflict"
    error = None
    if not reason:
        ok, error = _execute_moves(plan, report_path, legacy_dir, new_dir)
        if ok:
            report = _base_report(legacy_dir, new_dir, "moved",
                                  moved=[os.path.basename(s) for s, _d in plan])
            try:
                _write_json_atomic(report_path, report)
            except OSError:
                pass
            report["legacy_removed"] = _cleanup_legacy(legacy_dir)
            return report
        if str(error).startswith("rollback_failed"):
            _runtime["db_override"] = _where_is_db(legacy_dir, new_dir)
            return _base_report(legacy_dir, new_dir, "incomplete", error=error)
        reason = "move_failed"
    return _point_to_legacy(legacy_dir, new_dir, report_path, legacy_cfg,
                            legacy_db, reason, error)


# ---------------------------------------------------------------- преносима инсталация (на място)

def is_portable_dir(exe_dir, localappdata):
    """Одит (07.10.2026): .exe-то НЕ е в нито една от двете папки по
    подразбиране (те минават през migrate_install_dir/инсталатора). Без
    LOCALAPPDATA не можем да ги изключим — тогава „не“ (нищо не се пипа)."""
    if not exe_dir or not localappdata:
        return False
    new_dir, legacy_dir = install_dirs(localappdata)
    return not (same_path(exe_dir, new_dir) or same_path(exe_dir, legacy_dir))


def _recent_other_machines(db_path, machine, days=STALE_DAYS):
    """Одит (07.10.2026): други компютри, отчели се в базата (app_instances)
    през последните `days` дни — значи я ползват и те. [] при липса/грешка
    на таблицата (версиите преди нея не се отчитат)."""
    limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = sqlite3.connect(db_path, timeout=5)
        try:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                               " AND name = 'app_instances'").fetchone():
                return []
            return [r[0] for r in con.execute(
                "SELECT machine FROM app_instances WHERE last_seen >= ?"
                " AND machine <> ?", (limit, machine))]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _rename_legacy_logs(folder):
    """pacho_*.log → ph_*.log, само ако новото име е свободно; грешките се
    пренебрегват (логовете не са данни)."""
    renamed = []
    try:
        names = os.listdir(folder)
    except OSError:
        return renamed
    for name in names:
        if not _LEGACY_LOG_RE.match(name):
            continue
        target = new_name_for(name)
        dst = os.path.join(folder, target)
        if target == name or os.path.lexists(dst):
            continue
        try:
            os.replace(os.path.join(folder, name), dst)
            renamed.append(name)
        except OSError:
            pass
    return renamed


def _write_kept_report(report_path, previous, exe_dir, reason, error=None):
    """Отчет „имената остават“ — само ако се различава от предишния (иначе
    администраторът би го виждал при всеки старт)."""
    if (previous and previous.get("status") == "kept"
            and previous.get("reason") == reason):
        return previous
    report = _base_report(exe_dir, exe_dir, "kept", reason=reason, kind="in_place")
    if error:
        report["error"] = str(error)
    try:
        _write_json_atomic(report_path, report)
    except OSError:
        pass  # папка само за четене — пак нищо не е пипано
    return report


def migrate_portable(exe_dir, exe_path=None, share_paths_func=None, network_func=None,
                     in_use_func=None, machine=None):
    """Одит (07.10.2026): преносима инсталация с данни със старите имена до
    .exe-то — преименуване НА МЯСТО (pacho_config.json → ph_config.json,
    pacho_logistic.db(-wal/-shm/-journal) → ph_logistics.db, pacho_*.log →
    ph_*.log). Самото .exe НЕ се преименува (преките пътища сочат към него).

    Само при същите проверки като migrate_install_dir: папката не е мрежова,
    не е в Windows дял (нечетим регистър = не), конфигурацията се чете,
    другото .exe в папката не работи, базата се чете и в нея не се отчитат
    други компютри. Изричен db_path се пипа само ако сочи точно към базата
    до .exe-то (пренасочва се към новото име); иначе базата не се пипа.
    Дневник преди първото преименуване, връщане при грешка (заета база под
    Windows = неуспешен os.replace), възстановяване при следващия старт.
    Връща отчета или None; никога не хвърля."""
    if not exe_dir:
        return None
    report_path = os.path.join(exe_dir, REPORT_NAME)
    try:
        previous = _read_json(report_path) if os.path.exists(report_path) else None
        if previous and previous.get("status") == "in_progress":
            ok, failed = _recover(previous, report_path)
            if not ok:
                return _base_report(exe_dir, exe_dir, "incomplete", kind="in_place",
                                    error="не се върнаха: %s" % failed)
            previous = None
        if not any(os.path.exists(os.path.join(exe_dir, n))
                   for n in (LEGACY_DB_NAME, LEGACY_CONFIG_NAME)):
            return None  # бързият път: две проверки
        return _rename_in_place(exe_dir, exe_path, report_path, previous,
                                share_paths_func, network_func,
                                in_use_func or file_in_use, machine or machine_name())
    except Exception as exc:  # непредвидено — нищо не е преименувано без дневник
        return _base_report(exe_dir, exe_dir, "error", kind="in_place", error=repr(exc))


def _rename_in_place(exe_dir, exe_path, report_path, previous, share_paths_func,
                     network_func, in_use_func, machine):
    legacy_cfg = os.path.join(exe_dir, LEGACY_CONFIG_NAME)
    new_cfg = os.path.join(exe_dir, CONFIG_NAME)
    legacy_db = os.path.join(exe_dir, LEGACY_DB_NAME)
    new_db = os.path.join(exe_dir, DB_NAME)

    safe, reason = is_safe_to_move(exe_dir, share_paths_func, network_func)
    cfg = None
    active_cfg = legacy_cfg if os.path.exists(legacy_cfg) else new_cfg
    if not reason:
        if os.path.exists(legacy_cfg) and os.path.exists(new_cfg):
            reason = "conflict"
        else:
            cfg, broken = _read_config(active_cfg)
            if broken:
                reason = "config_unreadable"
    custom = str((cfg or {}).get("db_path") or "").strip()
    rename_db = os.path.exists(legacy_db) and (not custom or same_path(custom, legacy_db))
    if not reason and rename_db and any(
            os.path.lexists(new_db + s) for s in ("",) + DB_SIDECARS):
        reason = "conflict"  # и двете имена на базата — нищо не се пипа
    if not reason:
        own = os.path.abspath(exe_path) if exe_path else None
        for name in os.listdir(exe_dir):
            path = os.path.join(exe_dir, name)
            if (name.lower().endswith(".exe") and not (own and same_path(path, own))
                    and in_use_func(path)):
                reason = "running"
                break
    if not reason and rename_db:
        reason, _folder = _inspect_db(legacy_db)  # и прехвърля WAL в основния файл
        if not reason and _recent_other_machines(legacy_db, machine):
            reason = "used_by_others"
    if reason:
        return _write_kept_report(report_path, previous, exe_dir, reason)

    moves = family_moves(legacy_db, new_db) if rename_db else []
    if os.path.exists(legacy_cfg):
        moves.append((legacy_cfg, new_cfg))
    if not moves:
        return None
    ok, error = _execute_moves(moves, report_path, exe_dir, exe_dir)
    if ok and rename_db and custom:
        # db_path сочеше изрично към базата до .exe-то — сочи новото име.
        updated = dict(cfg)
        updated["db_path"] = new_db
        try:
            _write_json_atomic(new_cfg, updated)
        except OSError as exc:
            failed = _undo(moves)
            if failed:
                return _base_report(exe_dir, exe_dir, "incomplete", kind="in_place",
                                    error="config: %s; не се върнаха: %s" % (exc, failed))
            try:
                os.remove(report_path)
            except OSError:
                pass
            ok, error = False, "config: %s" % exc
    if not ok:
        if str(error).startswith("rollback_failed"):
            return _base_report(exe_dir, exe_dir, "incomplete", kind="in_place", error=error)
        return _write_kept_report(report_path, previous, exe_dir, "move_failed", error)
    report = _base_report(exe_dir, exe_dir, "renamed", kind="in_place",
                          renamed=[os.path.basename(s) for s, _d in moves],
                          logs=_rename_legacy_logs(exe_dir))
    try:
        _write_json_atomic(report_path, report)
    except OSError:
        pass
    return report


# ---------------------------------------------------------------- остатъци на тази машина

def _tree_is_stale(folder, days=STALE_DAYS, max_entries=20000):
    """Нищо в папката не е променяно от `days` дни (при съмнение — False)."""
    limit = time.time() - days * 86400
    count = 0
    try:
        if os.lstat(folder).st_mtime > limit:
            return False
        for root, dirs, files in os.walk(folder):
            for name in dirs + files:
                count += 1
                if count > max_entries:
                    return False
                if os.lstat(os.path.join(root, name)).st_mtime > limit:
                    return False
        return True
    except OSError:
        return False


def cleanup_legacy_leftovers(localappdata, programdata):
    """Одит (07.10.2026): остатъците от старото име на ТАЗИ машина —
    %ProgramData%\\PachoLogistic (катинарите на версиите до v3.78: файл,
    държан от работещо старо копие, не може да се изтрие под Windows и
    остава) и %LOCALAPPDATA%\\PachoLogistic, ако новата вече я има и старата
    съдържа само профила на прозореца, непроменян от STALE_DAYS дни.
    Грешките се пренебрегват. Връща списък с махнатите папки."""
    removed = []
    if programdata:
        folder = os.path.join(programdata, LEGACY_INSTALL_DIR_NAME)
        try:
            if os.path.isdir(folder):
                for name in os.listdir(folder):
                    if name.lower().endswith(".lock"):
                        try:
                            os.remove(os.path.join(folder, name))
                        except OSError:
                            pass
                os.rmdir(folder)  # само празна папка
                removed.append(folder)
        except OSError:
            pass
    if localappdata:
        legacy = os.path.join(localappdata, LEGACY_INSTALL_DIR_NAME)
        new = os.path.join(localappdata, INSTALL_DIR_NAME)
        try:
            if os.path.isdir(legacy) and os.path.isdir(new):
                names = os.listdir(legacy)
                if not names:
                    os.rmdir(legacy)
                    removed.append(legacy)
                elif (all(n.lower() in _USER_DIR_KNOWN for n in names)
                      and _tree_is_stale(legacy)):
                    shutil.rmtree(legacy, ignore_errors=True)
                    if not os.path.exists(legacy):
                        removed.append(legacy)
        except OSError:
            pass
    return removed


def migrate_user_dir(localappdata):
    """%LOCALAPPDATA%\\PachoLogistic → %LOCALAPPDATA%\\PHLogistics (профилът на
    вградения прозорец). Само ако новата още я няма; грешките се пренебрегват
    (папката е заета от работещо старо копие — ще се ползва нова)."""
    if not localappdata:
        return False
    legacy = os.path.join(localappdata, LEGACY_INSTALL_DIR_NAME)
    new = os.path.join(localappdata, INSTALL_DIR_NAME)
    try:
        if not os.path.isdir(legacy) or os.path.exists(new):
            return False
        os.rename(legacy, new)
        return True
    except OSError:
        return False


def _is_frozen_windows():
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def run_at_startup():
    """Вика се най-отгоре в app.py (само компилираното .exe под Windows)."""
    if not _is_frozen_windows():
        return None
    localappdata = os.environ.get("LOCALAPPDATA") or ""
    migrate_user_dir(localappdata)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    result = migrate_install_dir(exe_dir, localappdata)
    # Одит (07.10.2026): преносимата инсталация — преименуване на място.
    if result is None and is_portable_dir(exe_dir, localappdata):
        result = migrate_portable(exe_dir, sys.executable)
    try:
        cleanup_legacy_leftovers(localappdata, os.environ.get("ProgramData")
                                 or os.environ.get("ALLUSERSPROFILE") or "")
    except Exception:  # nosec B110 -- почистването е удобство, никога пречка за старта
        pass
    _runtime["result"] = result
    return result


def describe(result):
    """Кратък ред за лога при старт (на български — логът не се превежда)."""
    if not result:
        return ""
    status = result.get("status")
    if status == "moved":
        return "преход към новите имена: данните са преместени от %s в %s" % (
            result.get("legacy_dir"), result.get("new_dir"))
    if status in ("pointer", "pointer_runtime"):
        return ("преход към новите имена: данните остават в %s (причина: %s%s)" % (
            result.get("legacy_dir"), result.get("reason"),
            "; " + result["error"] if result.get("error") else ""))
    if status == "renamed":
        return "преход към новите имена: файловете в %s са преименувани на място (%s)" % (
            result.get("legacy_dir"), ", ".join(result.get("renamed") or []))
    if status == "kept":
        return ("преход към новите имена: файловете в %s запазват старите имена "
                "(причина: %s%s)" % (result.get("legacy_dir"), result.get("reason"),
                                     "; " + result["error"] if result.get("error") else ""))
    return "преход към новите имена: %s (%s)" % (status, result.get("error") or "")


def take_report_for_admin(install_dir):
    """Отчетът от ph_migration.json — ВЕДНЪЖ (отбелязва се като показан).
    None, ако няма какво да се покаже."""
    path = os.path.join(install_dir, REPORT_NAME)
    if not os.path.exists(path):
        return None
    report = _read_json(path)
    if not report or report.get("shown") or report.get("status") == "in_progress":
        return None
    report["shown"] = True
    try:
        _write_json_atomic(path, report)
    except OSError:
        return None  # не можем да го отбележим — по-добре без, отколкото всеки път
    return report
