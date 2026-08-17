# -*- coding: utf-8 -*-
"""Тестове за откриване на конфликт при GitHub синхронизация (M2).

Мрежовите заявки (_github_request) се подменят с фалшиви — тестваме само
логиката за конфликт/базова линия, не реалния GitHub API.
"""
import json
import os

import pytest

import backup


@pytest.fixture
def isolated_db(db_module, tmp_path, monkeypatch):
    """github_backup() чете живата база чрез db.DB_PATH (local_backup_to_temp) —
    вече сочи към временен файл благодарение на db_module fixture-а.
    Тук само гарантираме, че .syncstate.json файлът също е изолиран (той
    се извежда от db.DB_PATH автоматично, но проверяваме изрично)."""
    return db_module.DB_PATH


def _fake_request_factory(remote_sha_sequence, put_responses=None):
    """Връща faker за backup._github_request. remote_sha_sequence: списък
    от sha стойности (или None), връщани при последователни GET заявки за
    съществуващото sha (симулира _existing_sha() повиквания по ред)."""
    state = {"get_calls": 0, "put_calls": 0}
    put_responses = put_responses or []

    def fake(url, token, method="GET", body=None, tolerate_404=False):
        if method == "GET":
            idx = state["get_calls"]
            state["get_calls"] += 1
            sha = remote_sha_sequence[min(idx, len(remote_sha_sequence) - 1)]
            if sha is None:
                return 404, {}
            return 200, {"sha": sha}
        elif method == "PUT":
            idx = state["put_calls"]
            state["put_calls"] += 1
            new_sha = put_responses[min(idx, len(put_responses) - 1)]
            return 201, {"content": {"sha": new_sha, "html_url": "https://example.invalid/x"}}
        raise AssertionError("unexpected method %r" % method)

    fake.state = state
    return fake


def test_first_push_no_baseline_succeeds_and_records_sha(isolated_db, monkeypatch):
    fake = _fake_request_factory(remote_sha_sequence=[None], put_responses=["sha-A"])
    monkeypatch.setattr(backup, "_github_request", fake)

    backup.github_backup("owner", "repo", "tok")

    assert fake.state["put_calls"] == 1  # push happened, no conflict raised
    state = backup._load_local_sync_state(isolated_db)
    assert state["last_known_remote_sha"] == "sha-A"


def test_push_with_unchanged_remote_succeeds(isolated_db, monkeypatch):
    backup._save_local_sync_state({"last_known_remote_sha": "sha-A"}, isolated_db)
    fake = _fake_request_factory(remote_sha_sequence=["sha-A"], put_responses=["sha-B"])
    monkeypatch.setattr(backup, "_github_request", fake)

    backup.github_backup("owner", "repo", "tok")

    assert fake.state["put_calls"] == 1
    assert backup._load_local_sync_state(isolated_db)["last_known_remote_sha"] == "sha-B"


def test_push_with_changed_remote_raises_conflict_and_does_not_push(isolated_db, monkeypatch):
    # Последно познато на тази инсталация: "sha-A". Но GitHub сега връща
    # "sha-Z" — друга инсталация е качила нещо междувременно.
    backup._save_local_sync_state({"last_known_remote_sha": "sha-A"}, isolated_db)
    fake = _fake_request_factory(remote_sha_sequence=["sha-Z"], put_responses=["should-not-happen"])
    monkeypatch.setattr(backup, "_github_request", fake)

    with pytest.raises(backup.RemoteChangedError):
        backup.github_backup("owner", "repo", "tok")

    # Най-важното: НЕ трябва да е направен PUT — конфликтът спира качването
    # ПРЕДИ да презапише чуждите промени.
    assert fake.state["put_calls"] == 0
    # Локалното известно състояние остава непроменено (все още "sha-A").
    assert backup._load_local_sync_state(isolated_db)["last_known_remote_sha"] == "sha-A"


def test_force_bypasses_conflict_check(isolated_db, monkeypatch):
    backup._save_local_sync_state({"last_known_remote_sha": "sha-A"}, isolated_db)
    fake = _fake_request_factory(remote_sha_sequence=["sha-Z"], put_responses=["sha-FORCED"])
    monkeypatch.setattr(backup, "_github_request", fake)

    backup.github_backup("owner", "repo", "tok", force=True)

    assert fake.state["put_calls"] == 1
    assert backup._load_local_sync_state(isolated_db)["last_known_remote_sha"] == "sha-FORCED"


def test_pull_records_baseline_sha(isolated_db, monkeypatch, tmp_path):
    # Одит (находка К1): pull_db вече проверява PRAGMA integrity_check на
    # изтегления файл, преди изобщо да го докосне — затова тук вече трябва
    # да подадем истинско, валидно съдържание на SQLite база, не произволни
    # байтове (те коректно биват отхвърлени сега, вижте
    # test_pull_rejects_corrupted_content по-долу).
    import base64
    import sqlite3
    src_path = os.path.join(str(tmp_path), "_valid_src.db")
    src_con = sqlite3.connect(src_path)
    src_con.execute("CREATE TABLE t(x)")
    src_con.execute("INSERT INTO t VALUES ('данни от github')")
    src_con.commit()
    src_con.close()
    with open(src_path, "rb") as f:
        content = f.read()
    payload = {"content": base64.b64encode(content).decode("ascii"), "sha": "sha-PULLED"}

    def fake(url, token, method="GET", body=None, tolerate_404=False):
        return 200, payload

    monkeypatch.setattr(backup, "_github_request", fake)
    dest = os.path.join(str(tmp_path), "pulled.db")

    ok, err = backup.pull_db("owner", "repo", "tok", "main", "pacho_logistic.db", dest)

    assert ok is True
    assert err is None
    state = backup._load_local_sync_state(dest)
    assert state["last_known_remote_sha"] == "sha-PULLED"


def test_pull_rejects_corrupted_content(isolated_db, monkeypatch, tmp_path):
    """Одит (находка К1): произволни байтове (не истинска SQLite база) НЕ
    бива да заменят каквото и да е — pull_db трябва да откаже с ясна грешка
    вместо да записва невалидно съдържание върху живата база."""
    import base64
    content = b"x" * 200  # над 100 байта (валидацията за размер), но не е SQLite
    payload = {"content": base64.b64encode(content).decode("ascii"), "sha": "sha-BAD"}

    def fake(url, token, method="GET", body=None, tolerate_404=False):
        return 200, payload

    monkeypatch.setattr(backup, "_github_request", fake)
    dest = os.path.join(str(tmp_path), "pulled_bad.db")
    # Симулираме "вече съществуваща база", за да проверим, че тя оцелява
    # непокътната след отхвърления опит за замяна.
    import sqlite3
    pre_con = sqlite3.connect(dest)
    pre_con.execute("CREATE TABLE t(x)")
    pre_con.execute("INSERT INTO t VALUES ('преди опита за изтегляне')")
    pre_con.commit()
    pre_con.close()

    ok, err = backup.pull_db("owner", "repo", "tok", "main", "pacho_logistic.db", dest)

    assert ok is False
    assert err  # има съобщение за грешка
    survive_con = sqlite3.connect(dest)
    rows = survive_con.execute("SELECT * FROM t").fetchall()
    survive_con.close()
    assert rows == [("преди опита за изтегляне",)]
    assert not os.path.exists(dest + ".download")  # не остава недовършен временен файл


@pytest.mark.skipif(os.name == "nt", reason=(
    "сценарият е физически невъзможен на Windows: os.replace() върху файл, "
    "държан отворен от друга SQLite връзка, там гърми с WinError 5 (Access "
    "denied) ПРЕДИ изобщо да се стигне до стар -wal — pull_db го хваща и "
    "връща собствената си ясна грешка „вероятно е отворена от друг процес“ "
    "(отделно покрита), вместо тихата загуба на данни, която този тест "
    "възпроизвежда на POSIX"))
def test_pull_discards_stale_wal_so_downloaded_data_survives(isolated_db, monkeypatch, tmp_path):
    """Одит (находка К1, критичната репродукция): текущата база е в WAL
    режим с активна втора връзка (реалистичен мрежов сценарий) — преди
    поправката изтеглените данни биваха мълчаливо изхвърлени, защото
    старият "-wal" файл се прилагаше върху новото съдържание при следващото
    отваряне. pull_db вече трие "-wal"/"-shm" ПРЕДИ атомарната замяна."""
    import base64
    import sqlite3

    dest = os.path.join(str(tmp_path), "wal_target.db")
    old_con = sqlite3.connect(dest)
    old_con.execute("PRAGMA journal_mode=WAL")
    old_con.execute("CREATE TABLE t(x)")
    old_con.execute("INSERT INTO t VALUES ('стари данни')")
    old_con.commit()
    # Втора активна връзка държи -wal файла "жив" — точно сценарият в
    # мрежов режим с няколко едновременни служителя.
    keep_con = sqlite3.connect(dest)
    keep_con.execute("SELECT * FROM t").fetchall()
    assert os.path.exists(dest + "-wal")

    src_path = os.path.join(str(tmp_path), "_new_from_github.db")
    src_con = sqlite3.connect(src_path)
    src_con.execute("CREATE TABLE t(x)")
    for i in range(20):
        src_con.execute("INSERT INTO t VALUES (?)", ("нов ред %d" % i,))
    src_con.commit()
    src_con.close()
    with open(src_path, "rb") as f:
        content = f.read()
    payload = {"content": base64.b64encode(content).decode("ascii"), "sha": "sha-NEW"}

    def fake(url, token, method="GET", body=None, tolerate_404=False):
        return 200, payload

    monkeypatch.setattr(backup, "_github_request", fake)
    ok, err = backup.pull_db("owner", "repo", "tok", "main", "pacho_logistic.db", dest)
    old_con.close()
    keep_con.close()

    assert ok is True, err
    final_con = sqlite3.connect(dest)
    rows = final_con.execute("SELECT * FROM t").fetchall()
    integrity = final_con.execute("PRAGMA integrity_check").fetchone()
    final_con.close()
    assert len(rows) == 20, "изтеглените 20 реда не бива да бъдат изхвърлени от стар -wal"
    assert integrity == ("ok",)
    assert not os.path.exists(dest + "-wal")


def test_no_baseline_and_no_remote_file_is_not_a_conflict(isolated_db, monkeypatch):
    """Съвсем първо качване някога (няма нито локална база линия, нито
    отдалечен файл) не трябва да се третира като конфликт."""
    fake = _fake_request_factory(remote_sha_sequence=[None], put_responses=["sha-first"])
    monkeypatch.setattr(backup, "_github_request", fake)

    backup.github_backup("owner", "repo", "tok")  # не хвърля RemoteChangedError
    assert fake.state["put_calls"] == 1


def test_missing_credentials_raises_value_error(isolated_db):
    with pytest.raises(ValueError):
        backup.github_backup("", "", "", force=True)
