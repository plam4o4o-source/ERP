# -*- coding: utf-8 -*-
"""Резервно копиране на базата данни в локална/мрежова папка.

Настройките за локален архив (папка и дали да е автоматичен) се пазят в
таблица `settings` на самата база данни.

Бележка (25.08.2026): автоматичната синхронизация с частно GitHub хранилище
беше премахната по заявка на потребителя — остана само локалното архивиране
(ръчно „Архивирай сега“ + часовият автоматичен архив). Автоматичното
ОБНОВЯВАНЕ на самата програма от GitHub (updater.py) е отделна функция и НЕ
е засегнато.
"""
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta

import applog
import branding
import db

_auto_thread = {"timer": None}


#: Одит (находка В11): горна граница на архивирането, за да не виси нишка
#: безкрайно на заета база/бавен диск. Одит (01.10.2026, O3): минимумът е
#: 120 с и расте с размера на базата (~2 MB/s най-лош случай за SMB).
_BACKUP_MAX_SECONDS = 120
_BACKUP_MIN_BYTES_PER_SECOND = 2_000_000


def _backup_timeout(db_size):
    return max(_BACKUP_MAX_SECONDS, int(db_size / _BACKUP_MIN_BYTES_PER_SECOND))


def _snapshot_db(dst_path, deadline):
    """Одит (01.10.2026, O3): `VACUUM INTO` — ЕДНА четяща транзакция, която
    чужди записи не рестартират (старият backup() с pages=100 почваше
    отначало при всеки чужд запис и при оживена база никога не свършваше).
    Работи и в WAL, и в DELETE режим. Дедлайнът се следи от progress
    handler-а — върнато True прекъсва заявката."""
    src = sqlite3.connect(db.DB_PATH, timeout=15)
    try:
        src.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        try:
            src.execute("VACUUM INTO ?", (dst_path,))
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise TimeoutError(
                    "Архивирането отне твърде дълго (базата е много голяма или "
                    "дискът е бавен) — прекратено, за да не остане заявката "
                    "заключена безкрайно.") from exc
            raise
    finally:
        src.close()
    # Копието да не носи WAL флага на живата база — иначе отварянето му
    # оставя -wal/-shm в папката за архив.
    fix = sqlite3.connect(dst_path)
    try:
        fix.execute("PRAGMA journal_mode = DELETE")
    finally:
        fix.close()


def _copy_with_deadline(src_path, dst_path, deadline, chunk=4 * 1024 * 1024):
    with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
        while True:
            block = fin.read(chunk)
            if not block:
                break
            fout.write(block)
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "Копирането на архива към папката отне твърде дълго "
                    "(бавен мрежов диск) — прекратено.")
        fout.flush()
        os.fsync(fout.fileno())


def _check_writable(dest_folder):
    """Одит (01.10.2026, O11): ясна грешка за папка без право на запис,
    вместо суровото „unable to open database file“."""
    try:
        fd, probe = tempfile.mkstemp(prefix="pacho_logistic_probe_",
                                     suffix=PARTIAL_SUFFIX, dir=dest_folder)
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        raise RuntimeError(
            "Няма право на запис в папката за архив (%s) — изберете друга папка "
            "или поискайте от администратора на компютъра/мрежата права за "
            "запис. Подробности: %s" % (dest_folder, exc)) from exc


#: Одит (16.08.2026, находка №38, дребна): ръчен архив (бутон „Архивирай
#: сега“) и часовият автоматичен архив (start_auto_backup._tick) вървяха в
#: различни нишки без синхронизация; при съвпадение в ЕДНАТА И СЪЩА секунда
#: (името на файла е с резолюция секунда — stamp по-долу) двата пишеха в
#: ЕДИН И СЪЩ dest_path, а error-пътят на изгубилия състезанието трие
#: dest_path — файла на СПЕЧЕЛИЛИЯ (все още пишещ или вече завършил).
#: Заключването серializира двете операции: ако паднат в една и съща
#: секунда, втората просто презаписва СЪЩИЯ (валиден) архив на първата,
#: вместо да го поврежда/трие.
_local_backup_lock = threading.Lock()


def local_backup(dest_folder):
    """Прави безопасно копие на живата база данни в dest_folder (локална
    папка или мрежов диск/споделена папка) плюс zip с прикачените файлове.
    Резултатът (успех/грешка) се записва за панела и таблото (виж status)."""
    with _local_backup_lock:
        try:
            path = _local_backup_locked(dest_folder)
        except Exception as exc:
            _record_result(error=exc)
            raise
        _record_result(path=path)
        return path


def _local_backup_locked(dest_folder):
    if not dest_folder:
        raise ValueError("Не е зададена папка за архив.")
    if not os.path.isdir(dest_folder):
        raise RuntimeError(
            "Папката за архив не съществува или не е достъпна: %s" % dest_folder
        )
    _check_writable(dest_folder)
    # Одит (находка В12, част 1): проверка на свободното място преди копието
    # (вкл. прикачените файлове и логото — находка №3 от 26.09.2026).
    db_size = os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else 0
    try:
        free_bytes = shutil.disk_usage(dest_folder).free
        needed = db_size + sum(size for _src, _arc, size in _companion_sources())
        if needed and free_bytes < needed * 2:
            raise RuntimeError(
                "Малко свободно място в папката за архив (%.1f MB свободни, "
                "базата е %.1f MB) — архивирането е спряно, за да не се "
                "запълни дискът напълно." % (free_bytes / 1e6, needed / 1e6)
            )
    except OSError:
        pass  # неуспешна проверка на мястото не бива да спира самия архив
    # Одит (29.08.2026, находка №4): уникален суфикс — два компютъра/процеса,
    # архивиращи в една и съща секунда в една папка, не пишат в един файл.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(
        dest_folder, "pacho_logistic_%s_%s.db" % (stamp, secrets.token_hex(3)))
    # Одит (03.09.2026, находка №22): копира се в `.partial` (име, което
    # ротацията и човекът, търсещ „най-новия .db“, не бъркат с архив) и се
    # преименува чак след проверката — рязък изход оставя само `.partial`.
    partial_path = dest_path + PARTIAL_SUFFIX
    deadline = time.monotonic() + _backup_timeout(db_size)
    _sweep_stale_temp_dirs()
    tmp_dir = tempfile.mkdtemp(prefix=_TEMP_PREFIX)
    try:
        snapshot = os.path.join(tmp_dir, "snapshot.db")
        # Снимката е в локалната временна папка — четящата транзакция върху
        # живата база трае колкото локално копие, не колкото мрежов пренос.
        _snapshot_db(snapshot, deadline)
        # Одит (01.10.2026, O6): списъкът с файлове СЛЕД снимката — файл,
        # качен по време на архива, иначе липсваше в zip-а, а редът му е в .db.
        files = _companion_sources()
        try:
            _copy_with_deadline(snapshot, partial_path, deadline)
        except Exception:
            _remove_quietly(partial_path)
            raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    # Одит (находка №8 / №22): проверка на цялостта на КОПИЕТО в папката —
    # и че изобщо има схема (прекъснато копие минава integrity_check празно).
    ok = False
    check_con = None
    try:
        check_con = sqlite3.connect(partial_path)
        row = check_con.execute("PRAGMA integrity_check").fetchone()
        ok = bool(row) and row[0] == "ok"
        if ok:
            tables = check_con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()
            ok = bool(tables) and tables[0] > 0
    except sqlite3.DatabaseError:
        ok = False
    finally:
        if check_con is not None:
            check_con.close()
    if not ok:
        _remove_quietly(partial_path)
        raise RuntimeError(
            "Архивът не мина проверка за цялост след копирането — изтрит е "
            "автоматично, за да не остане на диска повреден файл, който "
            "изглежда наред."
        )
    # Атомарно преименуване: до този ред на диска няма файл с име на архив.
    os.replace(partial_path, dest_path)
    # Одит (26.09.2026, находка №3): до .db-то — zip със същото име на
    # прикачените файлове и логото. Провалът му НЕ трие вече готовия архив
    # на базата, но се съобщава (иначе липсата се открива чак при нужда).
    files_error = None
    try:
        _write_files_companion(dest_path, files)
    except Exception as exc:
        applog.log_exception("backup.local_backup: неуспешен архив на прикачените файлове")
        files_error = exc
    _rotate_local_backups(dest_folder)
    if files_error is not None:
        raise RuntimeError(
            "Базата е архивирана (%s), но прикачените файлове и логото — не: %s"
            % (dest_path, files_error))
    return dest_path


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


#: Временните папки на снимката (VACUUM INTO) — остатък от убит процес се
#: мете при следващия архив.
_TEMP_PREFIX = "pacho_bk_"


def _sweep_stale_temp_dirs(max_age_seconds=3600):
    base = tempfile.gettempdir()
    try:
        names = os.listdir(base)
    except OSError:
        return
    now = time.time()
    for name in names:
        if name.startswith(_TEMP_PREFIX):
            path = os.path.join(base, name)
            try:
                if now - os.path.getmtime(path) > max_age_seconds:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass


#: Одит (26.09.2026, находка №3): окончанието на придружаващия архив с
#: прикачените файлове (`<папка на базата>/attachments/`) и логото на фирмата.
#: Вътре пътищата са относителни към папката на базата — възстановяването е
#: разархивиране там. Когато няма нито едно от двете, zip НЕ се създава.
FILES_SUFFIX = ".files.zip"


def companion_path(db_backup_path):
    """Пътят до zip-а с файловете, придружаващ даден .db архив."""
    return db_backup_path[:-len(".db")] + FILES_SUFFIX


def _companion_sources():
    """[(абсолютен път, име в архива, размер)] — прикачените файлове и логото."""
    base = os.path.dirname(db.DB_PATH)
    result = []
    att_root = os.path.join(base, "attachments")
    for dirpath, _dirnames, filenames in os.walk(att_root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            arc = os.path.relpath(full, base).replace(os.sep, "/")
            result.append((full, arc, size))
    logo = branding.logo_path()
    if logo:
        try:
            result.append((logo, os.path.basename(logo), os.path.getsize(logo)))
        except OSError:
            pass
    return result


def _write_files_companion(dest_path, files):
    """Записва companion_path(dest_path) през `.partial` и проверка, както
    самия .db архив. Файл, изчезнал междувременно (изтрит прикачен файл), се
    пропуска. Връща пътя или None, ако няма какво да се архивира."""
    if not files:
        return None
    final_path = companion_path(dest_path)
    partial_path = final_path + PARTIAL_SUFFIX
    written = 0
    try:
        with zipfile.ZipFile(partial_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src, arc, _size in files:
                try:
                    zf.write(src, arc)
                    written += 1
                except FileNotFoundError:
                    continue
        with zipfile.ZipFile(partial_path) as zf:
            bad = zf.testzip()
            count = len(zf.namelist())
        if bad is not None or count != written:
            raise RuntimeError("архивът с файловете не мина проверка за цялост")
    except Exception:
        try:
            os.remove(partial_path)
        except OSError:
            pass
        raise
    if not written:
        os.remove(partial_path)
        return None
    os.replace(partial_path, final_path)
    return final_path


#: Одит (29.08.2026, находка №4): суфиксът е НЕЗАДЪЛЖИТЕЛЕН в израза, за да
#: се разпознават И вече съществуващите архиви без него (направени от
#: по-стара версия). Ако беше задължителен, ротацията щеше да спре да чисти
#: старите файлове — точно проблемът, който находка В12 затвори — и те щяха
#: да се трупат неограничено на същия мрежов диск.
_BACKUP_NAME_RE = re.compile(r"^pacho_logistic_(\d{8})_(\d{6})(?:_[0-9a-f]{6})?\.db$")

#: Одит (03.09.2026, находка №22): разширението, под което тече самото
#: копиране. НЕ съвпада с `_BACKUP_NAME_RE`, значи прекъснат архив никога не
#: минава за истински — нито пред ротацията, нито пред човека, който търси
#: „най-новия .db файл“, за да възстанови.
PARTIAL_SUFFIX = ".partial"

#: Остатъци, които рязък изход или прекъсната мрежа могат да оставят до
#: архивите. Ротацията ги мете заедно със старите копия — иначе се трупат
#: незабелязано на същия диск, а backup.py спира архивирането при малко
#: свободно място.
_LEFTOVER_SUFFIXES = (PARTIAL_SUFFIX, ".db-journal", ".db-wal", ".db-shm")


def _is_leftover(name):
    """Одит (01.10.2026, O10): и спътниците на `.partial` (`.db.partial-journal`,
    `-wal`, `-shm`), които убит архив оставя — досега ротацията не ги разпознаваше."""
    if not name.startswith("pacho_logistic_"):
        return False
    return name.endswith(_LEFTOVER_SUFFIXES) or (PARTIAL_SUFFIX + "-") in name


def _rotate_local_backups(dest_folder, now=None):
    """Одит (находка В12): часовият автоматичен архив (start_auto_backup,
    по подразбиране на всеки 60 мин) преди тази поправка никога не трieше
    стари копия — при база от 100 MB това е ~2.3 GB/ден, необозримо с
    времето, обичайно на СЪЩИЯ мрежов диск, който вече е под натиск.

    Политика на пазене (проста "дядо-баща-син" ротация, без външни
    зависимости): всичко от последните 48 часа се пази непокътнато (пълна
    часова резолюция за бързо възстановяване веднага след инцидент); от
    48 часа до 30 дни назад — само НАЙ-СТАРИЯТ архив на всеки календарен
    ден; отвъд 30 дни — само най-старият архив на всеки календарен месец.
    Всичко друго извън тези правила се трие.

    Засяга само файлове, отговарящи ТОЧНО на собствения формат на името
    (pacho_logistic_ГГГГММДД_ЧЧММСС.db) — други файлове в папката (напр.
    ръчно направени копия) не се пипат."""
    now = now or datetime.now()
    entries = []
    try:
        names = os.listdir(dest_folder)
    except OSError:
        return
    for name in names:
        m = _BACKUP_NAME_RE.match(name)
        if not m:
            # Одит (03.09.2026, находка №22): остатъци от прекъснато
            # копиране/незатворена база — чистят се, ако са по-стари от час
            # (по-младите може да принадлежат на текущо копиране).
            if _is_leftover(name):
                leftover = os.path.join(dest_folder, name)
                try:
                    if now.timestamp() - os.path.getmtime(leftover) > 3600:
                        os.remove(leftover)
                except OSError:
                    pass
            continue
        try:
            stamp = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        entries.append((stamp, os.path.join(dest_folder, name)))
    entries.sort()  # най-старите първи

    keep = set()
    seen_days = set()
    seen_months = set()
    for stamp, path in entries:
        age = now - stamp
        if age <= timedelta(hours=48):
            keep.add(path)
        elif age <= timedelta(days=30):
            day_key = stamp.date()
            if day_key not in seen_days:
                seen_days.add(day_key)
                keep.add(path)
        else:
            month_key = (stamp.year, stamp.month)
            if month_key not in seen_months:
                seen_months.add(month_key)
                keep.add(path)

    for stamp, path in entries:
        if path not in keep:
            # Одит (26.09.2026, находка №3): zip-ът с файловете си отива
            # заедно със своя .db архив.
            for victim in (path, companion_path(path)):
                if victim != path and not os.path.exists(victim):
                    continue
                try:
                    os.remove(victim)
                except OSError:
                    applog.log_exception("backup._rotate_local_backups: неуспешно изтриване на стар архив %s" % victim)


#: Одит (26.09.2026, находка №7): ключ в `settings` — кога (Unix време) е
#: направен последният АВТОМАТИЧЕН архив от който и да е компютър.
AUTO_BACKUP_LAST_RUN_KEY = "backup_auto_last_run"

#: Папки за архив, за чиято недостъпност вече е писано в лога (веднъж на процес).
_missing_folder_logged = set()


#: Одит (01.10.2026, O4): какво е записал последният успешен claim на ТОЗИ
#: процес — (предишна стойност, нашата стойност), за да може да го върне.
_last_claim = {"prev": None, "mine": None}


def _claim_auto_backup_slot(interval_seconds, now=None):
    """Одит (26.09.2026, находка №7): при споделена база всяка работна
    станция пускаше свой часов архив — N копия на час в една папка (пазят се
    48 ч.). Под BEGIN IMMEDIATE проверяваме кога е последният автоматичен
    архив; ако е по-скоро от ~интервала — пропускаме, иначе го отбелязваме
    и продължаваме. Връща True, ако този компютър трябва да архивира."""
    now = time.time() if now is None else now
    con = db.get_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT value FROM settings WHERE key = ?",
                          (AUTO_BACKUP_LAST_RUN_KEY,)).fetchone()
        try:
            last = float(row[0]) if row else None
        except (TypeError, ValueError):
            last = None
        # 0.9 — поносимост към разминаване на таймерите/часовниците; дата
        # далеч в бъдещето (сбъркан часовник) не бива да спре архивите.
        if last is not None and -interval_seconds < now - last < interval_seconds * 0.9:
            con.rollback()
            return False
        mine = "%d" % int(now)
        db.save_settings(con, {AUTO_BACKUP_LAST_RUN_KEY: mine})
        con.commit()
        _last_claim.update(prev=row[0] if row else None, mine=mine)
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _release_auto_backup_slot():
    """Одит (01.10.2026, O4): неуспешен автоматичен архив освобождава реда —
    иначе блокираше повторния опит на ВСИЧКИ компютри за цял час. Връща
    предишната стойност само ако никой друг не е взел реда междувременно."""
    mine, prev = _last_claim.get("mine"), _last_claim.get("prev")
    if mine is None:
        return
    con = db.get_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT value FROM settings WHERE key = ?",
                          (AUTO_BACKUP_LAST_RUN_KEY,)).fetchone()
        if row and row[0] == mine:
            if prev is None:
                con.execute("DELETE FROM settings WHERE key = ?", (AUTO_BACKUP_LAST_RUN_KEY,))
            else:
                db.save_settings(con, {AUTO_BACKUP_LAST_RUN_KEY: prev})
        con.commit()
    except Exception:
        con.rollback()
        applog.log_exception("backup._release_auto_backup_slot: неуспешно освобождаване")
    finally:
        con.close()
        _last_claim.update(prev=None, mine=None)


def run_scheduled_backup(get_settings_func, interval_minutes=60):
    """Една стъпка на автоматичния архив (виж start_auto_backup). Връща пътя
    на архива или None, ако не е правен. Ръчното „Архивирай сега“ не минава
    оттук и не се влияе от координацията."""
    s = get_settings_func()
    folder = (s.get("backup_folder") or "").strip()
    if not folder or not s.get("backup_auto"):
        return None
    if not os.path.isdir(folder):
        # Находка №7: папка, недостъпна от ТОЗИ компютър (напр. буква на
        # мрежов диск, съществуваща само на сървъра) — веднъж в лога, не
        # всеки час; не заемаме реда, за да архивира станция, която я вижда.
        if folder not in _missing_folder_logged:
            _missing_folder_logged.add(folder)
            applog.log_warning(
                "backup.run_scheduled_backup",
                "папката за автоматичен архив не е достъпна от този компютър: "
                "%s — пропускам (съобщава се веднъж)" % folder)
        return None
    if not _claim_auto_backup_slot(interval_minutes * 60):
        return None
    try:
        return local_backup(folder)
    except Exception:
        _release_auto_backup_slot()
        raise


# ---------------------------------------------------------------- състояние (O4)
#: Одит (01.10.2026, O4): ключове в `settings` — резултатът от последния
#: архив (ръчен или автоматичен, от който и да е компютър).
LAST_OK_AT_KEY = "backup_last_ok_at"
LAST_OK_PATH_KEY = "backup_last_ok_path"
LAST_ERROR_KEY = "backup_last_error"
LAST_ERROR_AT_KEY = "backup_last_error_at"
LAST_RESULT_KEY = "backup_last_result"


def _record_result(path=None, error=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if error is None:
        values = {LAST_OK_AT_KEY: now, LAST_OK_PATH_KEY: path or "", LAST_RESULT_KEY: "ok"}
    else:
        values = {LAST_ERROR_AT_KEY: now, LAST_ERROR_KEY: str(error)[:500],
                  LAST_RESULT_KEY: "error"}
    try:
        con = db.get_db()
        try:
            db.save_settings(con, values)
            con.commit()
        finally:
            con.close()
    except Exception:
        applog.log_exception("backup._record_result: резултатът от архива не е записан")


def status(con):
    """Одит (01.10.2026, O4): състоянието на архивирането за панела и таблото.

    {"configured": папка зададена, "folder", "auto": bool,
     "last_ok_at"/"last_ok_path": последният успешен архив (или None),
     "last_error"/"last_error_at": последната грешка (или None),
     "failing": последният опит е НЕУСПЕШЕН (грешката е по-нова от успеха)}"""
    s = db.get_settings(con)
    ok_at = s.get(LAST_OK_AT_KEY) or None
    err_at = s.get(LAST_ERROR_AT_KEY) or None
    folder = (s.get("backup_folder") or "").strip()
    return {
        "configured": bool(folder),
        "folder": folder,
        "auto": bool(s.get("backup_auto")),
        "last_ok_at": ok_at,
        "last_ok_path": s.get(LAST_OK_PATH_KEY) or None,
        "last_error": s.get(LAST_ERROR_KEY) or None,
        "last_error_at": err_at,
        "failing": s.get(LAST_RESULT_KEY) == "error",
    }


def list_backups(folder, limit=20):
    """Архивите (.db) в папката, най-новите първи: [{"name", "path", "size",
    "created" (datetime), "has_files" (има .files.zip)}]."""
    entries = []
    try:
        names = os.listdir(folder) if folder else []
    except OSError:
        return []
    for name in names:
        m = _BACKUP_NAME_RE.match(name)
        if not m:
            continue
        path = os.path.join(folder, name)
        try:
            created = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            size = os.path.getsize(path)
        except (ValueError, OSError):
            continue
        entries.append({"name": name, "path": path, "size": size, "created": created,
                        "has_files": os.path.exists(companion_path(path))})
    entries.sort(key=lambda e: (e["created"], e["name"]), reverse=True)
    return entries[:limit]


def start_auto_backup(get_settings_func, interval_minutes=60):
    """Стартира фонов таймер, който периодично прави локален архив, ако е
    зададена папка в настройките. Извиква се веднъж при стартиране."""
    def _tick():
        try:
            run_scheduled_backup(get_settings_func, interval_minutes)
        except Exception:
            applog.log_exception("backup._tick: неуспешен автоматичен локален архив")
        finally:
            t = threading.Timer(interval_minutes * 60, _tick)
            t.daemon = True
            t.start()
            _auto_thread["timer"] = t

    t = threading.Timer(60, _tick)  # първи опит минута след стартиране
    t.daemon = True
    t.start()
    _auto_thread["timer"] = t


# ---------------------------------------------------------------- възстановяване (O1)
# Одит (01.10.2026, O1): ръчното „копирай .db върху базата“ след срив оставя
# стария -wal до копието и SQLite го прилага върху него — възстановяването
# тихо не става или базата се поврежда. Затова възстановяването е при СТАРТ,
# преди някой да отвори базата: текущите файлове се местят настрана (никога
# не се трият), архивът се проверява и слага на мястото им.
RESTORE_MARKER_NAME = "pacho_restore_request.json"
RESTORE_RESULT_NAME = "pacho_restore_result.json"
_DB_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


def _db_dir():
    return os.path.dirname(os.path.abspath(db.DB_PATH))


def _restore_marker_path():
    return os.path.join(_db_dir(), RESTORE_MARKER_NAME)


def _restore_result_path():
    return os.path.join(_db_dir(), RESTORE_RESULT_NAME)


def _write_json_atomic(path, data):
    import json
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path):
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _verify_backup_file(path):
    """None, ако файлът е годна база със схема; иначе текст на проблема.
    Отваря се само за четене (query_only)."""
    try:
        con = sqlite3.connect(path)
    except sqlite3.Error as exc:
        return "архивът не може да бъде отворен (%s)" % exc
    try:
        con.execute("PRAGMA query_only = ON")
        row = con.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return "архивът е повреден (integrity_check: %s)" % (row[0] if row else "?")
        tables = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        if not tables:
            return "архивът е празен (няма нито една таблица)"
    except sqlite3.DatabaseError as exc:
        return "архивът е повреден (%s)" % exc
    finally:
        con.close()
    return None


def request_restore(folder, backup_name, requested_by=""):
    """Насрочва възстановяване от `backup_name` (само име на архив в
    настроената папка — не произволен път) за следващото стартиране.
    Хвърля ValueError с ясно съобщение при проблем."""
    if not folder or not _BACKUP_NAME_RE.match(backup_name or ""):
        raise ValueError("Невалиден архив за възстановяване.")
    path = os.path.join(folder, backup_name)
    # Пълната проверка (integrity_check) е при старта — върху копието до
    # базата; тук само бърза проверка, без да се отваря файлът в папката.
    try:
        with open(path, "rb") as fh:
            header = fh.read(16)
    except OSError:
        raise ValueError("Архивът не може да бъде използван: файлът липсва или е недостъпен.")
    if header != b"SQLite format 3\x00":
        raise ValueError("Архивът не може да бъде използван: файлът не е база данни.")
    zip_path = companion_path(path)
    _write_json_atomic(_restore_marker_path(), {
        "backup": os.path.abspath(path),
        "files_zip": os.path.abspath(zip_path) if os.path.exists(zip_path) else "",
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "requested_by": requested_by or "",
    })
    applog.log_audit("насрочено възстановяване от архив", "архив=%s" % path)
    return path


def pending_restore():
    """Насроченото възстановяване (речникът от маркера) или None."""
    return _read_json(_restore_marker_path())


def cancel_restore():
    _remove_quietly(_restore_marker_path())


def _move_aside(src, aside_dir, moved):
    dst = os.path.join(aside_dir, os.path.basename(src))
    os.replace(src, dst)
    moved.append((src, dst))


def _undo_moves(moved):
    for src, dst in reversed(moved):
        try:
            os.replace(dst, src)
        except OSError:
            applog.log_exception("backup.restore: неуспешно връщане на %s" % src)


def _extract_files_zip(zip_path, base, aside_dir):
    """Разархивира прикачените файлове/логото в папката на базата. Нищо не
    се трие: файл, който би бил презаписан (и друго лого), отива в aside_dir."""
    count = 0
    base_abs = os.path.abspath(base)
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        names = [m.filename for m in members]
        if any(os.path.basename(n).startswith("company_logo.") for n in names if "/" not in n):
            for name in os.listdir(base_abs):
                if name.startswith("company_logo."):
                    os.replace(os.path.join(base_abs, name), os.path.join(aside_dir, name))
        for member in members:
            target = os.path.abspath(os.path.join(base_abs, member.filename))
            if not target.startswith(base_abs + os.sep):
                continue  # път извън папката на базата — пропуска се
            if os.path.exists(target):
                keep = os.path.join(aside_dir, "files", os.path.relpath(target, base_abs))
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                os.replace(target, keep)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def apply_pending_restore():
    """Извиква се при СТАРТ, преди базата да бъде отворена (app.py и
    db.init_db). Ако има насрочено възстановяване — изпълнява го. Връща
    резултата {"ok", "message", ...} или None, ако няма какво да се прави."""
    marker = _restore_marker_path()
    if not os.path.exists(marker):
        return None
    req = _read_json(marker) or {}
    backup_path = str(req.get("backup") or "")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = {"ok": False, "backup": backup_path, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "requested_by": req.get("requested_by") or "", "aside": ""}
    staged = db.DB_PATH + ".restore_tmp"
    try:
        if not backup_path:
            raise RuntimeError("маркерът е повреден")
        if not os.path.isfile(backup_path):
            raise RuntimeError("файлът на архива липсва (%s)" % backup_path)
        # Копие до базата — проверява се ТОЧНО това, което ще застане на
        # мястото ѝ; архивът в папката не се отваря и не се променя.
        shutil.copyfile(backup_path, staged)
        problem = _verify_backup_file(staged)
        if problem:
            raise RuntimeError(problem)
        aside_dir = os.path.join(_db_dir(), "pre_restore_%s" % stamp)
        os.makedirs(aside_dir, exist_ok=True)
        moved = []
        try:
            for suffix in ("",) + _DB_SIDE_SUFFIXES:
                if os.path.exists(db.DB_PATH + suffix):
                    _move_aside(db.DB_PATH + suffix, aside_dir, moved)
        except OSError as exc:
            _undo_moves(moved)
            raise RuntimeError(
                "текущата база е заета (вероятно програмата още работи на друг "
                "компютър) — затворете я навсякъде и стартирайте отново (%s)" % exc)
        try:
            os.replace(staged, db.DB_PATH)
        except OSError:
            _undo_moves(moved)
            raise
        result["aside"] = aside_dir
        db._journal_settled.discard(db.DB_PATH)
        if db._USE_WAL:
            # Никой друг не е отворил базата (затворена навсякъде) — локалната
            # инсталация връща WAL, ако базата не е маркирана като споделена.
            try:
                fix = sqlite3.connect(db.DB_PATH)
                try:
                    if not db._setting_present(fix, db.SHARED_DB_KEY):
                        fix.execute("PRAGMA journal_mode = WAL")
                finally:
                    fix.close()
            except sqlite3.Error:
                pass  # само производителност — базата вече е на мястото си
        zip_path = str(req.get("files_zip") or "")
        if zip_path and os.path.exists(zip_path):
            try:
                result["files"] = _extract_files_zip(zip_path, _db_dir(), aside_dir)
            except Exception as exc:
                applog.log_exception("backup.apply_pending_restore: прикачените файлове")
                result["files_error"] = str(exc)
        result["ok"] = True
        result["message"] = (
            "Базата е възстановена от архива %s. Предишната база е запазена в %s."
            % (os.path.basename(backup_path), aside_dir))
        applog.log_audit("възстановена база от архив",
                         "архив=%s; предишната база е в %s" % (backup_path, aside_dir))
    except Exception as exc:
        _remove_quietly(staged)
        result["error"] = str(exc)
        result["message"] = ("Възстановяването от архива %s НЕ е извършено — текущата "
                             "база е непроменена. Причина: %s"
                             % (os.path.basename(backup_path) or "?", exc))
        applog.log_warning("backup.apply_pending_restore", result["message"])
    try:
        _write_json_atomic(_restore_result_path(), result)
    except OSError:
        applog.log_exception("backup.apply_pending_restore: резултатът не е записан")
    _remove_quietly(marker)
    return result


def take_restore_result():
    """Резултатът от последното възстановяване (веднъж — файлът се изтрива),
    за съобщение към администратора след вход. None, ако няма."""
    path = _restore_result_path()
    data = _read_json(path)
    if data is not None:
        _remove_quietly(path)
    return data
