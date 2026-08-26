# -*- coding: utf-8 -*-
"""Регресионни тестове за ЛОКАЛНИЯ архив (local_backup/_rotate_local_backups/
_bounded_backup) — одит (12.08.2026, находка №12).

Преди тази поправка нито един тест не пипаше _rotate_local_backups (логика,
която ТРИЕ файлове от диска на потребителя — политика 48ч/30дни/месечно) и
самата local_backup (проверка за свободно място, timeout на копирането) —
само GitHub push/pull пътят имаше покритие (виж test_backup_sync.py). Точно
тази функционалност е причинявала реален производствен проблем преди
(находка В12: часовият автоматичен архив не трieше нищо — ~2.3 GB/ден).

Тестовете тук покриват границите на ротацията (47ч59м/48ч01м, 29/31 дни),
отхвърлянето при малко свободно място, и че _bounded_backup реално
прекратява копиране, надвишило max_seconds — вместо само "happy path"."""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

import backup


def _touch_backup_file(folder, stamp):
    """Създава празен файл със ИМЕТО на архив с точно тази дата/час (само
    името има значение за _rotate_local_backups — не съдържанието)."""
    name = "pacho_logistic_%s.db" % stamp.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(b"x")
    return path


# ---------------------------------------------------------------- _rotate_local_backups: граници

def test_rotation_keeps_everything_within_48_hours(tmp_path):
    now = datetime(2026, 8, 12, 12, 0, 0)
    folder = str(tmp_path)
    kept = _touch_backup_file(folder, now - timedelta(hours=1))
    also_kept = _touch_backup_file(folder, now - timedelta(hours=47, minutes=59))
    backup._rotate_local_backups(folder, now=now)
    assert os.path.exists(kept)
    assert os.path.exists(also_kept)


def test_rotation_boundary_at_exactly_48_hours_is_kept(tmp_path):
    """age <= timedelta(hours=48) държи границата ВКЛЮЧИТЕЛНО — точно на
    48-ия час архивът все още се пази непокътнат."""
    now = datetime(2026, 8, 12, 12, 0, 0)
    folder = str(tmp_path)
    at_boundary = _touch_backup_file(folder, now - timedelta(hours=48))
    backup._rotate_local_backups(folder, now=now)
    assert os.path.exists(at_boundary)


def test_rotation_past_48_hours_keeps_only_oldest_per_day(tmp_path):
    """От 48ч до 30 дни назад — само НАЙ-СТАРИЯТ архив на всеки календарен
    ден оцелява, останалите от СЪЩИЯ ден се трият."""
    now = datetime(2026, 8, 12, 12, 0, 0)
    folder = str(tmp_path)
    day = now - timedelta(hours=49)  # базова дата, отвъд границата на 48ч
    # И двата часа по-долу трябва да останат > 48ч стари спрямо `now`
    # (01:00 → 59ч, 10:00 → 50ч) — иначе (виж regression, хванат при първо
    # писане на този тест) по-новият лесно случайно пада ПРЕДИ границата
    # от 48ч и тестът проверява грешното нещо.
    early = _touch_backup_file(folder, day.replace(hour=1))
    late = _touch_backup_file(folder, day.replace(hour=10))
    backup._rotate_local_backups(folder, now=now)
    assert os.path.exists(early)   # най-стар в деня — пази се
    assert not os.path.exists(late)  # по-нов в СЪЩИЯ ден — трие се


def test_rotation_boundary_at_exactly_30_days_uses_daily_rule(tmp_path):
    now = datetime(2026, 8, 12, 12, 0, 0)
    folder = str(tmp_path)
    at_boundary = _touch_backup_file(folder, now - timedelta(days=30))
    backup._rotate_local_backups(folder, now=now)
    # На точно 30 дни (<= 30 дни) архивът все още е под дневното правило —
    # единствен в деня си, значи се пази.
    assert os.path.exists(at_boundary)


def test_rotation_past_30_days_keeps_only_oldest_per_month(tmp_path):
    now = datetime(2026, 8, 12, 12, 0, 0)
    folder = str(tmp_path)
    month = now - timedelta(days=31)
    early = _touch_backup_file(folder, month.replace(day=1, hour=1) if month.day > 1 else month)
    late = _touch_backup_file(folder, month + timedelta(hours=5))
    backup._rotate_local_backups(folder, now=now)
    # И двата са в един и същ (за деня им ирелевантен, само месечен)
    # месец — трябва да оцелее само НАЙ-СТАРИЯТ.
    remaining = os.listdir(folder)
    assert len(remaining) == 1


def test_rotation_ignores_files_not_matching_naming_pattern(tmp_path):
    """Засяга само файлове, отговарящи ТОЧНО на собствения формат на
    името — други файлове в папката (напр. ръчно направени копия) не се
    пипат, дори да са много стари."""
    folder = str(tmp_path)
    manual_copy = os.path.join(folder, "моят_ръчен_архив.db")
    with open(manual_copy, "wb") as f:
        f.write(b"x")
    backup._rotate_local_backups(folder, now=datetime(2030, 1, 1))
    assert os.path.exists(manual_copy)


# ---------------------------------------------------------------- local_backup: свободно място
#
# ЗАБЕЛЕЖКА: dest_folder за local_backup() тук е ВИНАГИ отделна поддиректория
# на tmp_path, НЕ самият tmp_path — conftest.tmp_db_path слага живата
# (изходна) база директно в tmp_path (`tmp_path/test_pacho.db`); ако dest
# се препокрие със същата директория, `os.listdir(dest)` виждащ изходния
# .db файл дава грешно положителен резултат в тестове, проверяващи, че
# ДЕСТИНАЦИЯТА е празна при отказан backup.

@pytest.fixture
def dest_dir(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    return str(d)


def test_local_backup_rejects_when_disk_almost_full(dest_dir, db_module, monkeypatch):
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()

    def fake_disk_usage(path):
        class _U:
            free = 1000  # много под 2× размера на базата
        return _U()

    monkeypatch.setattr(backup.shutil, "disk_usage", fake_disk_usage)
    with pytest.raises(RuntimeError, match="свободно място"):
        backup.local_backup(dest_dir)
    # Нищо не трябва да е записано в папката при отказ преди копирането.
    assert os.listdir(dest_dir) == []


def test_local_backup_succeeds_with_plenty_of_free_space(dest_dir, db_module):
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    dest = backup.local_backup(dest_dir)
    assert os.path.exists(dest)
    check = sqlite3.connect(dest)
    assert check.execute("SELECT x FROM t").fetchone()[0] == 1
    check.close()


# ---------------------------------------------------------------- local_backup: находка №8 (частичен файл при грешка)

def test_local_backup_removes_partial_file_when_bounded_backup_fails(dest_dir, db_module, monkeypatch):
    """Одит (находка №8): преди поправката неуспешно копиране оставяше
    ЧАСТИЧНИЯ .db файл на диска, без проверка на цялостта. Симулираме
    грешка по средата на _bounded_backup — dest_path НЕ трябва да остане."""
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()

    def failing_bounded_backup(src, dst, max_seconds=25):
        raise TimeoutError("симулирана грешка по средата на копирането")

    monkeypatch.setattr(backup, "_bounded_backup", failing_bounded_backup)
    with pytest.raises(TimeoutError):
        backup.local_backup(dest_dir)
    # Частичният .db файл трябва да е изтрит, не оставен на диска.
    assert os.listdir(dest_dir) == []


def test_local_backup_removes_file_that_fails_integrity_check(dest_dir, db_module, monkeypatch):
    """Одит (находка №8, продължение): _bounded_backup може технически да
    „завърши" без изключение, но резултатът пак да не е валидна SQLite
    база (напр. прекъснат мрежов диск точно след последния progress
    callback) — проверката на цялостта СЛЕД копирането трябва да хване и
    този случай, не само изключение по време на самото копиране.
    Симулираме го, като заместваме готовия файл с невалидни байтове ПРЕДИ
    local_backup да стигне до собствената си integrity_check стъпка."""
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()

    def corrupting_bounded_backup(src, dst, max_seconds=25):
        # Взимаме реалния път на файла зад тази връзка, затваряме я (за да
        # освободим файла), после презаписваме съдържанието му с невалидни
        # байтове — резултатът е файл, който съществува, но НЕ е валидна
        # SQLite база, точно сценарият, който integrity_check трябва да
        # хване СЛЕД (привидно) успешно "завършилото" копиране.
        row = dst.execute("PRAGMA database_list").fetchone()
        file_path = row[2]
        dst.close()
        with open(file_path, "wb") as f:
            f.write(b"NOT A VALID SQLITE FILE" * 100)

    monkeypatch.setattr(backup, "_bounded_backup", corrupting_bounded_backup)
    with pytest.raises(RuntimeError, match="цялост"):
        backup.local_backup(dest_dir)
    # Повреденият файл трябва да е изтрит, не оставен на диска.
    assert os.listdir(dest_dir) == []


# ---------------------------------------------------------------- _bounded_backup: находка №9 (реален timeout)

def test_bounded_backup_interrupts_on_timeout(tmp_path, db_module):
    """Одит (находка №9): преди поправката pages=-1 (подразбиране) караше
    progress() да се вика само ЕДИН път, СЛЕД пълното копиране — deadline
    проверката никога не се стигаше „по средата". max_seconds=-1 тук кара
    ВСЯКО извикване на progress() (вкл. първото) да е вече след deadline —
    ако pages=_BACKUP_PAGES_PER_STEP (малък чанк) не беше приложено,
    единствената реалистична разлика би била дали TimeoutError изобщо
    успява да се вдигне ПРЕДИ backup() да е копирал всичко за микроскопична
    тестова база — затова проверяваме директно, че грешката се вдига."""
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    for i in range(500):
        con.execute("INSERT INTO t VALUES (?)", (i,))
    con.commit()
    con.close()

    src = sqlite3.connect(db_module.DB_PATH)
    dst_path = str(tmp_path / "out.db")
    dst = sqlite3.connect(dst_path)
    try:
        with pytest.raises(TimeoutError, match="повече от"):
            backup._bounded_backup(src, dst, max_seconds=-1)
    finally:
        dst.close()
        src.close()


def test_bounded_backup_uses_small_page_chunks():
    """Одит (находка №9): pages=-1 (подразбиране на sqlite3) би копирал
    всичко в ЕДНА стъпка — progress() само след пълно завършване.
    _BACKUP_PAGES_PER_STEP трябва да е малко положително число, за да
    вика progress() периодично И по време на нормално (небавно)
    копиране, не само при SQLITE_BUSY retry."""
    assert 0 < backup._BACKUP_PAGES_PER_STEP < 10000


# Бележка (25.08.2026): тестът за backup.local_backup_to_temp отпадна —
# самата функция се ползваше само от GitHub качването (github_backup) и беше
# премахната заедно с GitHub синхронизацията. Локалният архив (local_backup)
# си има собствено почистване на частичен файл при грешка, покрито по-горе.
