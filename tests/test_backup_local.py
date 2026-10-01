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
    assert remaining == [os.path.basename(early)], "оцелява най-старият в месеца"
    assert not os.path.exists(late)


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

def test_local_backup_removes_partial_file_when_snapshot_fails(dest_dir, db_module, monkeypatch):
    """Одит (находка №8): неуспешно копиране не оставя частичен файл.
    Одит (01.10.2026, O3): копирането вече е VACUUM INTO + копие — и двете
    стъпки могат да се провалят."""
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()

    def failing_snapshot(dst_path, deadline):
        raise TimeoutError("симулирана грешка по средата на копирането")

    monkeypatch.setattr(backup, "_snapshot_db", failing_snapshot)
    with pytest.raises(TimeoutError):
        backup.local_backup(dest_dir)
    assert os.listdir(dest_dir) == []


def test_local_backup_removes_partial_file_when_copy_fails(dest_dir, db_module, monkeypatch):
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()

    def failing_copy(src_path, dst_path, deadline, chunk=0):
        with open(dst_path, "wb") as fh:
            fh.write(b"half")
        raise TimeoutError("бавен мрежов диск")

    monkeypatch.setattr(backup, "_copy_with_deadline", failing_copy)
    with pytest.raises(TimeoutError):
        backup.local_backup(dest_dir)
    assert os.listdir(dest_dir) == []


def test_local_backup_removes_file_that_fails_integrity_check(dest_dir, db_module, monkeypatch):
    """Одит (находка №8, продължение): копие, „завършило“ без изключение, но
    невалидно (прекъснат мрежов диск) — проверката на цялостта го хваща."""
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()

    def corrupting_copy(src_path, dst_path, deadline, chunk=0):
        with open(dst_path, "wb") as f:
            f.write(b"NOT A VALID SQLITE FILE" * 100)

    monkeypatch.setattr(backup, "_copy_with_deadline", corrupting_copy)
    with pytest.raises(RuntimeError, match="цялост"):
        backup.local_backup(dest_dir)
    assert os.listdir(dest_dir) == []


# ---------------------------------------------------------------- снимка: реален таван на времето

def test_snapshot_interrupts_on_deadline(tmp_path, db_module):
    """Одит (находка №9 / 01.10.2026, O3): изтекъл срок прекъсва VACUUM INTO
    през progress handler-а, вместо нишката да виси."""
    import time as _time
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5000)])
    con.commit()
    con.close()
    with pytest.raises(TimeoutError, match="твърде дълго"):
        backup._snapshot_db(str(tmp_path / "out.db"), _time.monotonic() - 1)


def test_backup_timeout_scales_with_database_size():
    """Одит (01.10.2026, O3): 25 с не стигаха за голяма база по мрежа."""
    assert backup._backup_timeout(0) >= 120
    assert backup._backup_timeout(2_000_000_000) > backup._backup_timeout(0)


# Бележка (25.08.2026): тестът за backup.local_backup_to_temp отпадна —
# самата функция се ползваше само от GitHub качването (github_backup) и беше
# премахната заедно с GitHub синхронизацията. Локалният архив (local_backup)
# си има собствено почистване на частичен файл при грешка, покрито по-горе.
