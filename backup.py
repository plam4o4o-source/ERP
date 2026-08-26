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
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import applog
import db

_auto_thread = {"timer": None}


#: Одит (находка В11): sqlite3.Connection.backup() при SQLITE_BUSY (базата
#: заета от друга едновременна връзка — реалистично в мрежов режим на бавен
#: диск) спи и опитва отново БЕЗ горна граница — connect(timeout=...) НЕ
#: важи за самия backup цикъл, само за първоначалното отваряне. Без изрична
#: горна граница едно заето копиране виси безкрайно — на "Архивирай сега"
#: (замразява една от малкото нишки на сървъра в мрежов режим завинаги) и на
#: часовия локален архив (спира да се възобновява). Виж _bounded_backup
#: по-долу.
_BACKUP_MAX_SECONDS = 25


#: Одит (12.08.2026, находка №9, high): sqlite3.Connection.backup() без
#: изричен `pages=` копира ВСИЧКИ страници в ЕДНА стъпка (pages=-1,
#: подразбирането) — "progress" кука се вика само МЕЖДУ стъпки, значи само
#: ЕДИН ПЪТ, СЛЕД като цялото копиране вече е завършило (или между опитите
#: при SQLITE_BUSY — единственият случай, в който предишната версия на
#: тази защита реално прекъсваше нещо). При генерално БАВНО (но НЕзаето)
#: копиране — напр. към бавен мрежов диск — дедлайнът в _progress никога
#: не се проверяваше по средата, защото „по средата“ просто не съществуваше
#: като момент; същия сценарий, който коментарите тук твърдяха, че решават,
#: оставаше непокрит. Малък брой страници на стъпка кара backup() да вика
#: progress() периодично И по време на нормално (небавно) копиране, не само
#: при retry — дедлайнът вече реално прекъсва бавно копиране по средата.
_BACKUP_PAGES_PER_STEP = 100


def _bounded_backup(src, dst, max_seconds=_BACKUP_MAX_SECONDS):
    """src.backup(dst) с твърда горна граница на времето. Ползва официалната
    "progress" кука на sqlite3 (извиква се периодично между стъпките на
    копирането — вижте _BACKUP_PAGES_PER_STEP по-горе защо стъпките са
    нарочно малки, — включително между опитите при SQLITE_BUSY) — вдигнато
    оттам изключение прекъсва самия backup() цикъл, вместо да го оставя да
    виси."""
    deadline = time.monotonic() + max_seconds

    def _progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError(
                "Архивирането отне повече от %d сек. (базата вероятно е "
                "заета от друга едновременна операция, или копирането към "
                "целта е твърде бавно) — прекратено, за да не остане "
                "заявката/нишката заключена безкрайно." % max_seconds
            )

    src.backup(dst, pages=_BACKUP_PAGES_PER_STEP, progress=_progress, sleep=0.25)


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
    папка или мрежов диск/споделена папка). Използва вградения SQLite
    backup API, за да не копира файл, който в момента се записва."""
    with _local_backup_lock:
        return _local_backup_locked(dest_folder)


def _local_backup_locked(dest_folder):
    if not dest_folder:
        raise ValueError("Не е зададена папка за архив.")
    if not os.path.isdir(dest_folder):
        raise RuntimeError(
            "Папката за архив не съществува или не е достъпна: %s" % dest_folder
        )
    # Одит (находка В12, част 1): преди да добавим поредния файл, проверяваме
    # свободното място — часовите архиви растат неограничено (виж
    # _rotate_local_backups по-долу за самата ротация); при почти пълен диск
    # предпочитаме ясна грешка сега пред трудно обясним провал по средата на
    # копирането (или тих провал на СЛЕДВАЩ, несвързан запис в програмата).
    try:
        free_bytes = shutil.disk_usage(dest_folder).free
        db_size = os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else 0
        if db_size and free_bytes < db_size * 2:
            raise RuntimeError(
                "Малко свободно място в папката за архив (%.1f MB свободни, "
                "базата е %.1f MB) — архивирането е спряно, за да не се "
                "запълни дискът напълно." % (free_bytes / 1e6, db_size / 1e6)
            )
    except OSError:
        pass  # неуспешна проверка на мястото не бива да спира самия архив
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(dest_folder, "pacho_logistic_%s.db" % stamp)
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        _bounded_backup(src, dst)
    except Exception:
        # Одит (12.08.2026, находка №8, high): преди тази поправка неуспешно
        # копиране (прекъсване, timeout от _bounded_backup, грешка при
        # запис) оставяше ЧАСТИЧНИЯ/повреден .db файл на диска, без никаква
        # проверка на цялостта. Такъв файл изглежда като нормален архив
        # (същото име, дата), но е нечетим/непълен — открива се едва при
        # опит за реално възстановяване, най-лошия възможен момент. Сега
        # частичният файл се трие веднага при грешка.
        dst.close()
        src.close()
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise
    else:
        dst.close()
        src.close()
    # Одит (находка №8, продължение): проверка на цялостта на ГОТОВИЯ архив
    # — _bounded_backup може технически да „завърши“ без изключение, но
    # резултатът пак да не е валидна SQLite база (напр. прекъснат мрежов
    # диск точно след последния progress callback). PRAGMA integrity_check
    # хваща точно това.
    check_con = sqlite3.connect(dest_path)
    try:
        row = check_con.execute("PRAGMA integrity_check").fetchone()
        ok = bool(row) and row[0] == "ok"
    except sqlite3.DatabaseError:
        ok = False
    finally:
        check_con.close()
    if not ok:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise RuntimeError(
            "Архивът не мина проверка за цялост след копирането — изтрит е "
            "автоматично, за да не остане на диска повреден файл, който "
            "изглежда наред."
        )
    _rotate_local_backups(dest_folder)
    return dest_path


_BACKUP_NAME_RE = re.compile(r"^pacho_logistic_(\d{8})_(\d{6})\.db$")


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
            try:
                os.remove(path)
            except OSError:
                applog.log_exception("backup._rotate_local_backups: неуспешно изтриване на стар архив %s" % path)


def start_auto_backup(get_settings_func, interval_minutes=60):
    """Стартира фонов таймер, който периодично прави локален архив, ако е
    зададена папка в настройките. Извиква се веднъж при стартиране."""
    def _tick():
        try:
            s = get_settings_func()
            folder = s.get("backup_folder", "").strip()
            if folder and s.get("backup_auto"):
                local_backup(folder)
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
