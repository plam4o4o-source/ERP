# -*- coding: utf-8 -*-
"""Преход PachoLogistic/pacho_ → PHLogistics/ph_ (одит 06.10.2026).

Покрива: избора между новото и старото име (завинаги — мрежовите/преносимите
инсталации остават със старите), еднократното преместване на данните на
локалната инсталация (legacy_migration), указателя при съмнение и пазача
срещу нова празна база, двете имена в архивите/маркерите/обновяването,
скрипта за прехода през инсталатора (симулатор на cmd.exe), интерфейса и
CI/инсталатора (по изходния код — истинският Windows е в release.yml).

Всичко е във временни папки — истинските данни в корена не се пипат."""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest

import backup
import config as appconfig
import legacy_migration as lm
import updater
from conftest import ROOT, post_with_csrf, read_source

NO_SHARES = staticmethod(lambda: [])


# ---------------------------------------------------------------- помощни

def _make_db(path, marker="ok", backup_folder=None):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE marker (v TEXT)")
    con.execute("INSERT INTO marker VALUES (?)", (marker,))
    if backup_folder is not None:
        con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO settings VALUES ('backup_folder', ?)", (backup_folder,))
    con.commit()
    con.close()


def _db_marker(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        con.close()


def _legacy_install(local, cfg=None, with_db=True, **db_kwargs):
    """Фалшива стара инсталация под <local>/Programs/PachoLogistic и празна
    нова (само PHLogistics.exe, както я оставя инсталаторът)."""
    new_dir, legacy = lm.install_dirs(str(local))
    os.makedirs(os.path.join(legacy, "attachments", "1"))
    os.makedirs(new_dir)
    if with_db:
        _make_db(os.path.join(legacy, "pacho_logistic.db"), **db_kwargs)
    with open(os.path.join(legacy, "pacho_config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg if cfg is not None else {"network_port": 5050, "x": "y"}, fh)
    with open(os.path.join(legacy, ".secret_key"), "w") as fh:
        fh.write("ab" * 32)
    with open(os.path.join(legacy, "attachments", "1", "a.txt"), "w") as fh:
        fh.write("прикачен")
    for name, data in (("company_logo.png", b"PNG"), ("pacho_startup_1234abcd.log", b"log"),
                       ("pacho_update.log", b"OK"), ("PachoLogistic.exe", b"MZold"),
                       ("PachoLogistic.exe.old", b"MZolder"), ("unins000.exe", b"MZu"),
                       ("unins000.dat", b"u"), ("pacho_update_1234abcd.bat", b"@echo")):
        with open(os.path.join(legacy, name), "wb") as fh:
            fh.write(data)
    with open(os.path.join(new_dir, "PHLogistics.exe"), "wb") as fh:
        fh.write(b"MZnew")
    return new_dir, legacy


def _migrate(local, shares=(), network=False, in_use=False):
    new_dir, _legacy = lm.install_dirs(str(local))
    return lm.migrate_install_dir(new_dir, str(local),
                                  share_paths_func=lambda: list(shares) if shares is not None else None,
                                  network_func=lambda p: network,
                                  in_use_func=lambda p: in_use)


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    monkeypatch.setitem(lm._runtime, "db_override", None)
    monkeypatch.setitem(lm._runtime, "result", None)
    monkeypatch.setattr(updater, "_rename_safety_cache", {})


# ---------------------------------------------------------------- имена и избор

def test_resolve_prefers_new_then_legacy_then_new(tmp_path):
    base = str(tmp_path)
    assert lm.resolve_config_path(base) == os.path.join(base, "ph_config.json")
    (tmp_path / "pacho_config.json").write_text("{}")
    assert lm.resolve_config_path(base) == os.path.join(base, "pacho_config.json")
    (tmp_path / "ph_config.json").write_text("{}")
    assert lm.resolve_config_path(base) == os.path.join(base, "ph_config.json")
    assert lm.resolve_default_db_path(base) == os.path.join(base, "ph_logistics.db")
    (tmp_path / "pacho_logistic.db").write_bytes(b"")
    assert lm.resolve_default_db_path(base) == os.path.join(base, "pacho_logistic.db")


def _import_in_subprocess(exe_dir, code):
    """config/db внесени „като компилираното .exe“ от папка exe_dir."""
    script = ("import sys; sys.frozen = True; sys.executable = %r\n" % os.path.join(exe_dir, "X.exe")
              + "sys.path.insert(0, %r)\n" % ROOT + code)
    out = subprocess.run([sys.executable, "-c", script], cwd=exe_dir, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_config_and_db_pick_legacy_names_at_import_and_keep_wal(tmp_path):
    """Мрежова/преносима инсталация със стари имена: CONFIG_PATH и базата
    по подразбиране са старите файлове, а WAL остава (сравнява се с ИЗБРАНИЯ
    път по подразбиране — с твърдо „ph_logistics.db“ WAL би изчезнал)."""
    exe_dir = str(tmp_path)
    (tmp_path / "pacho_config.json").write_text('{"network_port": 5001}')
    _make_db(str(tmp_path / "pacho_logistic.db"))
    line = _import_in_subprocess(exe_dir, "import config, db\n"
                                 "print(config.CONFIG_PATH, db.DB_PATH, db._USE_WAL)")
    cfg, dbp, wal = line.split(" ")
    assert cfg == os.path.join(exe_dir, "pacho_config.json")
    assert dbp == os.path.join(exe_dir, "pacho_logistic.db")
    assert wal == "True"


def test_new_install_uses_new_names_at_import(tmp_path):
    line = _import_in_subprocess(str(tmp_path), "import config, db\n"
                                 "print(config.CONFIG_PATH, db.DB_PATH, db._USE_WAL)")
    cfg, dbp, wal = line.split(" ")
    assert cfg.endswith("ph_config.json") and dbp.endswith("ph_logistics.db")
    assert wal == "True"
    assert not os.path.exists(dbp), "внасянето не бива да създава база"


def test_app_runs_the_migration_before_log_config_and_db():
    src = read_source("app.py")
    call = src.index("legacy_migration.run_at_startup()")
    for later in ("_open_startup_log(_base_dir_early)", "import config as appconfig",
                  "import db\n", "import applog\n"):
        assert call < src.index(later), "преходът трябва да е преди %r" % later
    # legacy_migration не внася нищо от програмата, което чете пътищата.
    lm_src = read_source("legacy_migration.py")
    assert not re.search(r"^import (config|db|applog)\b", lm_src, re.M)


def test_name_mapping_and_program_files():
    assert lm.new_name_for("pacho_logistic.db-wal") == "ph_logistics.db-wal"
    assert lm.new_name_for("pacho_config.json") == "ph_config.json"
    assert lm.new_name_for("pacho_startup_1a2b3c4d.log") == "ph_startup_1a2b3c4d.log"
    assert lm.new_name_for("pacho_logistic_20260101_010101.db") == "ph_logistics_20260101_010101.db"
    assert lm.new_name_for(".secret_key") == ".secret_key"
    for name in ("PachoLogistic.exe", "PachoLogistic.exe.old", "PachoLogistic.exe.1234abcd.new",
                 "unins000.dat", "unins000.msg", "pacho_update_1234abcd.bat", "ph_update_x.bat"):
        assert lm.is_program_file(name), name
    for name in ("pacho_logistic.db", "attachments", "company_logo.png", "pacho_update.log"):
        assert not lm.is_program_file(name), name


def test_plan_keeps_names_that_would_collide_and_db_family_together():
    plan = lm.plan_names(["pacho_startup_x.log", "ph_startup_x.log", "pacho_logistic.db",
                          "pacho_logistic.db-wal", "ph_logistics.db", "pacho_config.json"])
    assert plan["pacho_startup_x.log"] == "pacho_startup_x.log"
    # Новата база вече е тук → старото семейство остава ЦЯЛО със старото име
    # (чужд -wal до друга база би я повредил).
    assert plan["pacho_logistic.db"] == "pacho_logistic.db"
    assert plan["pacho_logistic.db-wal"] == "pacho_logistic.db-wal"
    assert plan["pacho_config.json"] == "ph_config.json"


def test_share_paths_parsing_and_inside_check(tmp_path):
    values = [["CSCFlags=0", "MaxUses=4294967295", "Path=%s" % tmp_path, "ShareName=Data"],
              "path=/other\x00Remark=", 42]
    assert lm.parse_share_paths(values) == [str(tmp_path), "/other"]
    inner = os.path.join(str(tmp_path), "Programs", "PachoLogistic")
    assert lm.path_is_inside(inner, str(tmp_path))
    assert not lm.path_is_inside(str(tmp_path) + "X", str(tmp_path))
    assert lm.is_safe_to_move(inner, lambda: [str(tmp_path)], lambda p: False) == (False, "shared")
    assert lm.is_safe_to_move(inner, lambda: None, lambda p: False) == (False, "registry")
    assert lm.is_safe_to_move(inner, lambda: [], lambda p: True) == (False, "network")
    assert lm.is_safe_to_move(inner, lambda: ["/other"], lambda p: False) == (True, None)
    assert lm.is_network_path(r"\\SERVER\share\x")


# ---------------------------------------------------------------- преместване

def test_safe_local_install_is_moved_with_new_names(tmp_path):
    new_dir, legacy = _legacy_install(tmp_path, marker="данни")
    report = _migrate(tmp_path)
    assert report["status"] == "moved", report
    assert _db_marker(os.path.join(new_dir, "ph_logistics.db")) == "данни"
    with open(os.path.join(new_dir, "ph_config.json"), encoding="utf-8") as fh:
        assert json.load(fh) == {"network_port": 5050, "x": "y"}
    assert open(os.path.join(new_dir, ".secret_key")).read() == "ab" * 32
    assert open(os.path.join(new_dir, "attachments", "1", "a.txt")).read() == "прикачен"
    for name in ("company_logo.png", "ph_startup_1234abcd.log", "ph_update.log"):
        assert os.path.exists(os.path.join(new_dir, name)), name
    for name in ("pacho_logistic.db", "pacho_config.json", "PachoLogistic.exe", "unins000.dat"):
        assert not os.path.exists(os.path.join(new_dir, name)), name
    assert not os.path.exists(legacy), "празната стара папка се маха"
    on_disk = json.load(open(os.path.join(new_dir, lm.REPORT_NAME), encoding="utf-8"))
    assert on_disk["status"] == "moved" and on_disk["shown"] is False
    # Новата инсталация вече намира данните по новите имена.
    assert lm.resolve_config_path(new_dir).endswith("ph_config.json")
    assert lm.resolve_default_db_path(new_dir).endswith("ph_logistics.db")


def test_migration_is_idempotent_and_cheap_when_done(tmp_path, monkeypatch):
    new_dir, _legacy = _legacy_install(tmp_path)
    assert _migrate(tmp_path)["status"] == "moved"
    before = sorted(os.listdir(new_dir))
    calls = []
    real_isdir = os.path.isdir
    monkeypatch.setattr(lm.os.path, "isdir", lambda p: calls.append(p) or real_isdir(p))
    assert _migrate(tmp_path) is None
    assert len(calls) == 1, "бързият път е една проверка"
    assert sorted(os.listdir(new_dir)) == before


def test_nothing_happens_outside_the_new_default_folder(tmp_path):
    _new_dir, legacy = _legacy_install(tmp_path)
    other = tmp_path / "Programs" / "Custom"
    other.mkdir()
    assert lm.migrate_install_dir(str(other), str(tmp_path)) is None
    assert lm.migrate_install_dir(legacy, str(tmp_path)) is None
    assert os.path.exists(os.path.join(legacy, "pacho_logistic.db"))


def test_wal_content_is_kept_when_moving(tmp_path):
    new_dir, legacy = _legacy_install(tmp_path, with_db=False)
    db = os.path.join(legacy, "pacho_logistic.db")
    _make_db(db, marker="first")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("UPDATE marker SET v = 'in-wal'")
    con.commit()
    holder = sqlite3.connect(db)  # пречи на автоматичния checkpoint при затваряне
    holder.execute("SELECT 1 FROM marker").fetchone()
    con.close()
    holder.close()
    assert _migrate(tmp_path)["status"] == "moved"
    assert _db_marker(os.path.join(new_dir, "ph_logistics.db")) == "in-wal"


@pytest.mark.parametrize("kwargs,reason", [
    ({"shares": None}, "registry"),
    ({"network": True}, "network"),
    ({"in_use": True}, "running"),
])
def test_doubt_leaves_data_in_place_and_points_to_it(tmp_path, kwargs, reason):
    new_dir, legacy = _legacy_install(tmp_path)
    report = _migrate(tmp_path, **kwargs)
    assert report["status"] == "pointer" and report["reason"] == reason
    _assert_pointer(new_dir, legacy)


def test_shared_folder_is_never_moved(tmp_path):
    new_dir, legacy = _legacy_install(tmp_path)
    report = _migrate(tmp_path, shares=[str(tmp_path / "Programs")])
    assert report["reason"] == "shared"
    _assert_pointer(new_dir, legacy)


def _assert_pointer(new_dir, legacy, db_name="pacho_logistic.db"):
    for name in ("pacho_logistic.db", "pacho_config.json", ".secret_key", "PachoLogistic.exe"):
        assert os.path.exists(os.path.join(legacy, name)), "%s е пипнат" % name
    assert os.path.exists(os.path.join(legacy, "attachments", "1", "a.txt"))
    cfg = json.load(open(os.path.join(new_dir, "ph_config.json"), encoding="utf-8"))
    assert cfg["network_port"] == 5050 and cfg["x"] == "y", "старите ключове се пазят"
    assert cfg["db_path"] == os.path.join(legacy, db_name)
    assert open(os.path.join(new_dir, ".secret_key")).read() == "ab" * 32
    assert not os.path.exists(os.path.join(new_dir, "ph_logistics.db"))
    assert not os.path.exists(os.path.join(new_dir, "pacho_logistic.db"))


def test_pointer_makes_the_app_open_the_legacy_db(tmp_path, monkeypatch):
    new_dir, legacy = _legacy_install(tmp_path)
    _migrate(tmp_path, network=True)
    monkeypatch.setattr(appconfig, "CONFIG_PATH", lm.resolve_config_path(new_dir))
    assert appconfig.resolve_db_path(new_dir) == os.path.join(legacy, "pacho_logistic.db")


def test_explicit_db_path_elsewhere_is_kept_when_moving(tmp_path):
    new_dir, legacy = _legacy_install(tmp_path, cfg={"db_path": r"\\SERVER\share\pacho_logistic.db"})
    assert _migrate(tmp_path)["status"] == "moved"
    cfg = json.load(open(os.path.join(new_dir, "ph_config.json"), encoding="utf-8"))
    assert cfg["db_path"] == r"\\SERVER\share\pacho_logistic.db"


def test_explicit_db_path_into_the_old_folder_is_never_renamed(tmp_path):
    local = tmp_path
    _new, legacy = lm.install_dirs(str(local))
    target = os.path.join(legacy, "pacho_logistic.db")
    new_dir, legacy = _legacy_install(local, cfg={"db_path": target, "network_port": 5050, "x": "y"})
    report = _migrate(local)
    assert report["status"] == "pointer" and report["reason"] == "db_path_inside"
    _assert_pointer(new_dir, legacy)


def test_backup_folder_inside_the_old_folder_keeps_data_in_place(tmp_path):
    _new, legacy = lm.install_dirs(str(tmp_path))
    new_dir, legacy = _legacy_install(tmp_path, backup_folder=os.path.join(legacy, "архиви"))
    assert _migrate(tmp_path)["reason"] == "backup_inside"
    _assert_pointer(new_dir, legacy)


def test_unreadable_db_keeps_data_in_place(tmp_path):
    new_dir, legacy = _legacy_install(tmp_path, with_db=False)
    with open(os.path.join(legacy, "pacho_logistic.db"), "wb") as fh:
        fh.write(b"not a database at all, but some bytes" * 100)
    assert _migrate(tmp_path)["reason"] == "db_unreadable"
    _assert_pointer(new_dir, legacy)


def test_failure_in_the_middle_rolls_everything_back(tmp_path, monkeypatch):
    new_dir, legacy = _legacy_install(tmp_path)
    before = sorted(os.listdir(legacy))
    real_replace = os.replace
    count = {"n": 0}

    def flaky(src, dst):
        if os.path.dirname(dst) == new_dir and not dst.endswith((".tmp", ".json")):
            count["n"] += 1
            if count["n"] == 4:
                raise PermissionError("файлът е зает")
        return real_replace(src, dst)

    monkeypatch.setattr(lm.os, "replace", flaky)
    report = _migrate(tmp_path)
    assert count["n"] == 4
    assert report["status"] == "pointer" and report["reason"] == "move_failed"
    assert sorted(os.listdir(legacy)) == before, "всичко се връща със старите имена"
    _assert_pointer(new_dir, legacy)


def test_guard_no_fresh_empty_db_when_even_the_pointer_cannot_be_written(tmp_path, monkeypatch):
    new_dir, legacy = _legacy_install(tmp_path)
    real_write = lm._write_json_atomic

    def no_config(path, data):
        if os.path.basename(path) == lm.CONFIG_NAME:
            raise PermissionError("няма право на запис")
        return real_write(path, data)

    monkeypatch.setattr(lm, "_write_json_atomic", no_config)
    report = _migrate(tmp_path, network=True)
    assert report["status"] == "pointer_runtime"
    assert not os.path.exists(os.path.join(new_dir, "ph_config.json"))
    # Програмата (config/db) отваря старата база, не нова празна.
    monkeypatch.setattr(appconfig, "CONFIG_PATH", lm.resolve_config_path(new_dir))
    assert appconfig.resolve_db_path(new_dir) == os.path.join(legacy, "pacho_logistic.db")
    assert not os.path.exists(os.path.join(new_dir, "ph_logistics.db"))


def test_unexpected_error_still_guards_the_db(tmp_path, monkeypatch):
    new_dir, legacy = _legacy_install(tmp_path)
    monkeypatch.setattr(lm, "_inspect_db", lambda p: 1 / 0)
    report = _migrate(tmp_path)
    assert report["status"] == "error"
    assert lm.runtime_db_override() == os.path.join(legacy, "pacho_logistic.db")
    assert os.path.exists(lm.runtime_db_override())


def test_interrupted_migration_is_recovered_on_next_start(tmp_path):
    """Ток по средата: дневникът (in_progress) връща преместеното и
    преходът се прави наново — нищо не остава разделено."""
    new_dir, legacy = _legacy_install(tmp_path, marker="ток")
    plan = lm._plan_moves(legacy, new_dir, "pacho_logistic.db")
    with open(os.path.join(new_dir, lm.REPORT_NAME), "w", encoding="utf-8") as fh:
        json.dump({"status": "in_progress", "moves": [[s, d] for s, d in plan]}, fh)
    for src, dst in plan[:3]:  # „преместени“ преди спирането на тока
        os.replace(src, dst)
    report = _migrate(tmp_path)
    assert report["status"] == "moved"
    assert _db_marker(os.path.join(new_dir, "ph_logistics.db")) == "ток"
    assert os.path.exists(os.path.join(new_dir, "attachments", "1", "a.txt"))
    assert not os.path.exists(legacy)


def test_both_folders_with_data_are_left_alone_and_reported_once(tmp_path):
    new_dir, legacy = _legacy_install(tmp_path)
    (tmp_path / "Programs" / "PHLogistics" / "ph_config.json").write_text("{}")
    report = _migrate(tmp_path)
    assert report["status"] == "both"
    assert os.path.exists(os.path.join(legacy, "pacho_logistic.db"))
    assert _migrate(tmp_path) is None


def test_report_is_taken_once_for_the_admin(tmp_path):
    new_dir, _legacy = _legacy_install(tmp_path)
    _migrate(tmp_path)
    first = lm.take_report_for_admin(new_dir)
    assert first and first["status"] == "moved"
    assert lm.take_report_for_admin(new_dir) is None


def test_user_dir_is_moved_once(tmp_path):
    old = tmp_path / "PachoLogistic" / "webview"
    old.mkdir(parents=True)
    (old / "Cookies").write_text("c")
    assert lm.migrate_user_dir(str(tmp_path)) is True
    assert (tmp_path / "PHLogistics" / "webview" / "Cookies").read_text() == "c"
    (tmp_path / "PachoLogistic").mkdir()
    assert lm.migrate_user_dir(str(tmp_path)) is False, "новата вече съществува"
    assert lm.migrate_user_dir("") is False


# ---------------------------------------------------------------- архиви и маркери

def test_backups_with_both_prefixes_are_listed_rotated_and_new_ones_use_ph(db_module, tmp_path):
    folder = tmp_path / "архиви"
    folder.mkdir()
    old_name = "pacho_logistic_20200101_010101.db"
    (folder / old_name).write_bytes(b"SQLite format 3\x00")
    (folder / "pacho_logistic_20200101_020202.db").write_bytes(b"SQLite format 3\x00")
    names = [b["name"] for b in backup.list_backups(str(folder))]
    assert old_name in names
    path = backup.local_backup(str(folder))
    assert os.path.basename(path).startswith("ph_logistics_")
    assert os.path.basename(path) in [b["name"] for b in backup.list_backups(str(folder))]
    backup._rotate_local_backups(str(folder))
    left = sorted(os.listdir(folder))
    assert old_name in left, "най-старият архив на месеца се пази"
    assert "pacho_logistic_20200101_020202.db" not in left, "старите се ротират"


def test_legacy_restore_marker_and_result_are_honoured(db_module, tmp_path, monkeypatch):
    folder = tmp_path / "архиви"
    folder.mkdir()
    src = backup.local_backup(str(folder))
    db_dir = os.path.dirname(db_module.DB_PATH)
    legacy_marker = os.path.join(db_dir, "pacho_restore_request.json")
    with open(legacy_marker, "w", encoding="utf-8") as fh:
        json.dump({"backup": src, "files_zip": ""}, fh)
    assert backup.pending_restore()["backup"] == src
    result = backup.apply_pending_restore()
    assert result["ok"], result
    assert not os.path.exists(legacy_marker)
    assert os.path.exists(os.path.join(db_dir, "ph_restore_result.json"))
    # Резултат, оставен със старото име, също се показва (веднъж).
    os.replace(os.path.join(db_dir, "ph_restore_result.json"),
               os.path.join(db_dir, "pacho_restore_result.json"))
    assert backup.take_restore_result()["ok"]
    assert backup.take_restore_result() is None


def test_new_restore_marker_is_written_with_the_new_name(db_module, tmp_path):
    folder = tmp_path / "архиви"
    folder.mkdir()
    src = backup.local_backup(str(folder))
    backup.request_restore(str(folder), os.path.basename(src))
    db_dir = os.path.dirname(db_module.DB_PATH)
    assert os.path.exists(os.path.join(db_dir, "ph_restore_request.json"))
    assert not os.path.exists(os.path.join(db_dir, "pacho_restore_request.json"))
    backup.cancel_restore()
    assert backup.pending_restore() is None


# ---------------------------------------------------------------- обновяване

class _Resp:
    def __init__(self, data):
        self._data = data if isinstance(data, bytes) else data.encode("utf-8")
        self.headers = {"Content-Length": str(len(self._data))}
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release(names, sums_lines):
    assets = [{"name": n, "browser_download_url": "https://example.invalid/" + n} for n in names]
    if sums_lines is not None:
        assets.append({"name": "SHA256SUMS.txt",
                       "browser_download_url": "https://example.invalid/SHA256SUMS.txt"})
    release = {"tag_name": "v99.0.0", "assets": assets, "html_url": "https://example.invalid/r"}

    def fake_urlopen(req, timeout=8):
        url = req.full_url
        if url.endswith("SHA256SUMS.txt"):
            return _Resp("\n".join(sums_lines))
        return _Resp(json.dumps(release))
    return fake_urlopen


def test_update_prefers_the_new_exe_name_and_reads_its_checksum(monkeypatch):
    sums = ["a" * 64 + "  PHLogistics.exe", "b" * 64 + "  PHLogistics-Setup.exe",
            "c" * 64 + "  PachoLogistic.exe", "d" * 64 + "  PachoLogistic-Setup.exe"]
    monkeypatch.setattr(updater.net, "urlopen", _release(
        ["PachoLogistic.exe", "PHLogistics.exe", "PHLogistics-Setup.exe",
         "PachoLogistic-Setup.exe"], sums))
    info = updater.check_for_update()
    assert info["download"].endswith("/PHLogistics.exe")
    assert info["expected_sha256"] == "a" * 64
    assert info["setup_download"].endswith("/PHLogistics-Setup.exe")
    assert info["setup_sha256"] == "b" * 64


def test_update_falls_back_to_the_legacy_exe_name(monkeypatch):
    monkeypatch.setattr(updater.net, "urlopen", _release(
        ["PachoLogistic.exe"], ["c" * 64 + "  PachoLogistic.exe"]))
    info = updater.check_for_update()
    assert info["download"].endswith("/PachoLogistic.exe")
    assert info["expected_sha256"] == "c" * 64
    assert info["setup_download"] is None, "без проверен инсталатор — само на място"


def test_setup_without_checksum_line_is_not_offered(monkeypatch):
    monkeypatch.setattr(updater.net, "urlopen", _release(
        ["PHLogistics.exe", "PHLogistics-Setup.exe"], ["a" * 64 + "  PHLogistics.exe"]))
    info = updater.check_for_update()
    assert info["setup_download"] is None and info["setup_sha256"] is None
    assert updater.setup_of(info) is None


def test_failed_markers_of_both_names_are_read_and_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "PachoLogistic.exe"))
    legacy = tmp_path / updater._legacy_failed_marker_name()
    legacy.write_text("3.79.0 \n")
    assert updater.read_failed_install_version() == "3.79.0"
    (tmp_path / updater._failed_marker_name()).write_text("3.80.0\n")
    assert updater.read_failed_install_version() == "3.80.0", "новото име е с предимство"
    updater.clear_failed_install_marker()
    assert updater.read_failed_install_version() is None
    assert not legacy.exists()


def _as_legacy_local(tmp_path, monkeypatch, shares=()):
    local = tmp_path / "local"
    new_dir, legacy = _legacy_install(local)
    os.remove(os.path.join(new_dir, "PHLogistics.exe"))
    os.rmdir(new_dir)  # инсталаторът още не е минал
    exe = os.path.join(legacy, "PachoLogistic.exe")
    monkeypatch.setattr(updater.sys, "executable", exe)
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(lm, "read_share_paths", lambda: list(shares))
    monkeypatch.setattr(lm, "is_network_path", lambda p: False)
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(temp))
    return local, new_dir, legacy, exe


def test_legacy_local_detection_and_blockers(tmp_path, monkeypatch):
    local, _new, legacy, exe = _as_legacy_local(tmp_path, monkeypatch)
    assert updater.is_legacy_local_install()
    assert updater.can_complete_rename()
    assert updater.rename_status() == {"can_complete": True, "failure": None}
    updater._rename_safety_cache.clear()
    monkeypatch.setattr(lm, "read_share_paths", lambda: [str(local)])
    assert updater.rename_blocker() == "shared"
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "share" / "PachoLogistic.exe"))
    assert updater.rename_blocker() == "not_legacy"
    assert updater.rename_status() is None


def test_setup_path_is_used_once_per_version(tmp_path, monkeypatch):
    _as_legacy_local(tmp_path, monkeypatch)
    assert updater.use_setup_for_update("3.80.0")
    with open(updater._rename_failed_marker_path(), "w") as fh:
        fh.write("3.80.0 setup \n")
    assert updater.read_rename_failure() == {"version": "3.80.0", "reason": "setup"}
    assert not updater.use_setup_for_update("3.80.0"), "провалилият се преход не се върти"
    assert updater.use_setup_for_update("3.81.0")


def test_auto_update_uses_the_setup_for_a_legacy_local_install(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "use_setup_for_update", lambda v: True)
    monkeypatch.setattr(updater, "install_via_setup", lambda *a, **k: calls.append(("setup", a)))
    monkeypatch.setattr(updater, "install_update", lambda *a, **k: calls.append(("exe", a)))
    updater._schedule_auto_install("u", "s", "9.9.9", warning_seconds=0, setup=("su", "ss"))
    assert calls == [("setup", ("su", "ss"))]
    monkeypatch.setattr(updater, "use_setup_for_update", lambda v: False)
    updater._schedule_auto_install("u", "s", "9.9.9", warning_seconds=0, setup=("su", "ss"))
    assert calls[-1] == ("exe", ("u", "s"))


# ---------------------------------------------------------------- скриптът за прехода (симулатор)

class _RenameBatch:
    """Симулатор на подмножеството cmd.exe, което скриптът ползва."""

    def __init__(self, path, args, env, running, setup, on_start):
        self.path, self.args, self.env = path, args, dict(env)
        self.running, self.setup, self.on_start = running, setup, on_start
        self.started, self.errorlevel = [], 0
        self.lines = open(path, encoding="ascii", newline="").read().split("\r\n")

    def _expand(self, line):
        # Единствените „\\“ в скрипта са разделители на пътища (%~3\PHLogistics.exe).
        line = line.replace("\\", os.sep)
        line = line.replace("%~dp0", os.path.dirname(self.path) + os.sep)
        line = line.replace("%~f0", self.path)
        line = line.replace("%~nx2", os.path.basename(self.args[1]))
        line = line.replace("%~dp2", os.path.dirname(self.args[1]) + os.sep)
        for i, value in enumerate(self.args, start=1):
            line = line.replace("%%~%d" % i, value)
        return re.sub(r"%([A-Z_]+)%", lambda m: self.env.get(m.group(1), ""), line)

    def _cmd(self, cmd):
        cmd = re.sub(r"\s*(>nul 2>&1|2>nul|>nul)$", "", cmd.strip())
        m = re.match(r'copy /y "([^"]+)" "([^"]+)"$', cmd)
        if m:
            if not os.path.exists(m.group(1)):
                return False, None
            with open(m.group(1), "rb") as src, open(m.group(2), "wb") as dst:
                dst.write(src.read())
            return True, None
        m = re.match(r'start "" /wait "([^"]+)" (.*)$', cmd)
        if m:
            self.errorlevel = self.setup(m.group(1), m.group(2))
            return True, None
        m = re.match(r'start "" "([^"]+)"$', cmd)
        if m:
            self.started.append(m.group(1))
            self.on_start(m.group(1), dict(self.env))
            return True, None
        m = re.match(r'echo (.*?)\s?> "([^"]+)"$', cmd)
        if m:
            if os.path.isdir(os.path.dirname(m.group(2))):
                with open(m.group(2), "w", encoding="ascii") as fh:
                    fh.write(m.group(1) + "\n")
            return True, None
        m = re.match(r'del "([^"]+)"$', cmd)
        if m:
            if os.path.isfile(m.group(1)):
                os.remove(m.group(1))
            return True, None
        m = re.match(r'rd "([^"]+)"$', cmd)
        if m:
            try:
                os.rmdir(m.group(1))
            except OSError:
                pass
            return True, None
        m = re.match(r"goto (\w+)$", cmd)
        if m:
            return True, m.group(1)
        m = re.match(r"set /a (\w+)\+=1$", cmd)
        if m:
            self.env[m.group(1)] = str(int(self.env.get(m.group(1), "0")) + 1)
            return True, None
        m = re.match(r"set (\w+)=(.*)$", cmd)
        if m:
            if m.group(2):
                self.env[m.group(1)] = m.group(2)
            else:
                self.env.pop(m.group(1), None)
            return True, None
        if cmd.startswith(("ping ", "@echo off", "setlocal")):
            return True, None
        raise AssertionError("непозната команда в скрипта: %r" % cmd)

    def _run(self, line):
        line = line.strip()
        m = re.match(r'tasklist /nh /fi "PID eq (\d+)" /fi "IMAGENAME eq ([^"]+)" 2>nul \| '
                     r'find /i "([^"]+)" >nul && goto (\w+)$', line)
        if m:
            assert m.group(2) == m.group(3)
            return m.group(4) if self.running(int(m.group(1))) else None
        m = re.match(r'if (not )?exist "([^"]+)" (.*)$', line)
        if m:
            if bool(m.group(1)) != os.path.exists(m.group(2)):
                return self._run(m.group(3))
            return None
        m = re.match(r"if errorlevel (\d+) (.*)$", line)
        if m:
            return self._run(m.group(2)) if self.errorlevel >= int(m.group(1)) else None
        m = re.match(r"if (\d+) LSS (\d+) (.*)$", line)
        if m:
            return self._run(m.group(3)) if int(m.group(1)) < int(m.group(2)) else None
        for op in (" && ", " || "):
            if op in line:
                first, rest = line.split(op, 1)
                ok, target = self._cmd(first)
                if ok == (op == " && "):
                    return self._run(rest)
                return target
        return self._cmd(line)[1]

    def run(self, max_steps=20000):
        labels = {l[1:]: i for i, l in enumerate(self.lines) if l.startswith(":")}
        pc = 0
        for _ in range(max_steps):
            if pc >= len(self.lines):
                return
            line = self.lines[pc]
            pc += 1
            if not line or line.startswith(":"):
                continue
            target = self._run(self._expand(line))
            if target:
                pc = labels[target] + 1
        raise AssertionError("скриптът не приключи")


SETUP_PAYLOAD = b"MZ" + b"\x00" * 1_100_000


def _start_rename(tmp_path, monkeypatch):
    local, new_dir, legacy, exe = _as_legacy_local(tmp_path, monkeypatch)
    captured = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda args, **kw: captured.update(args=args, **kw))
    monkeypatch.setattr(updater.threading, "Timer",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _Resp(SETUP_PAYLOAD))
    updater.install_via_setup("https://example.invalid/PHLogistics-Setup.exe",
                              hashlib.sha256(SETUP_PAYLOAD).hexdigest(), version="3.79.0")
    args = re.findall(r'"([^"]*)"', captured["args"][len('cmd.exe /d /s /c "'):-1])
    return local, new_dir, legacy, exe, captured, args


def _installer(new_dir, legacy, exit_code=0):
    """Като Inno Setup: [InstallDelete] на старите програмни файлове, после
    новото .exe в новата папка."""
    calls = []

    def setup(path, params):
        calls.append(params)
        assert open(path, "rb").read() == SETUP_PAYLOAD
        for name in ("PachoLogistic.exe", "PachoLogistic.exe.old", "unins000.exe", "unins000.dat"):
            if os.path.exists(os.path.join(legacy, name)):
                os.remove(os.path.join(legacy, name))
        if exit_code == 0:
            os.makedirs(new_dir, exist_ok=True)
            with open(os.path.join(new_dir, "PHLogistics.exe"), "wb") as fh:
                fh.write(b"MZnew")
        return exit_code
    return setup, calls


def test_rename_command_line_and_script_follow_the_update_rules(tmp_path, monkeypatch):
    local, new_dir, legacy, exe, captured, args = _start_rename(tmp_path, monkeypatch)
    bat, setup, old_exe, target, version, pid, ppid = args
    assert captured["args"].startswith('cmd.exe /d /s /c ""') and captured["args"].endswith('""')
    assert old_exe == exe and target == new_dir and version == "3.79.0"
    assert int(pid) == os.getpid() and int(ppid) == os.getppid()
    assert os.path.dirname(setup) == os.path.dirname(bat) != legacy, "работната папка е в TEMP"
    assert captured["env"][updater.STARTED_MARKER_ENV].startswith(os.path.dirname(bat))
    raw = open(bat, "rb").read()
    assert raw.isascii() and b"\r\n" in raw and b"\r\r\n" not in raw
    text = raw.decode("ascii")
    assert text.splitlines()[1] == "setlocal DisableDelayedExpansion"
    assert "echo %~4>" not in text and "echo %~4 " in text
    assert "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER" in text
    assert '/DIR="%~3"' in text


def test_rename_script_success_moves_data_and_never_starts_the_old_exe(tmp_path, monkeypatch):
    local, new_dir, legacy, exe, captured, args = _start_rename(tmp_path, monkeypatch)
    setup, calls = _installer(new_dir, legacy)
    polls = {"n": 0}

    def running(pid):  # процесът излиза след два опита
        polls["n"] += 1
        return polls["n"] <= 2

    def on_start(path, env):
        if path == os.path.join(new_dir, "PHLogistics.exe"):
            # Новото .exe: преходът при старта, после „стартирах“.
            report = lm.migrate_install_dir(new_dir, str(local), share_paths_func=lambda: [],
                                            network_func=lambda p: False,
                                            in_use_func=lambda p: False)
            assert report["status"] == "moved", report
            monkeypatch.setattr(updater.os, "environ", env)
            assert updater.confirm_started()

    sim = _RenameBatch(args[0], args[1:], captured["env"], running, setup, on_start)
    sim.run()
    work = os.path.dirname(args[0])
    assert sim.started == [os.path.join(new_dir, "PHLogistics.exe")]
    assert len(calls) == 1
    assert "OK" in open(os.path.join(work, "ph_rename.log")).read()
    assert _db_marker(os.path.join(new_dir, "ph_logistics.db")) == "ok"
    assert not os.path.exists(legacy)
    for leftover in ("old_exe.bak", "PHLogistics-Setup.exe", "ph_rename_started.txt"):
        assert not os.path.exists(os.path.join(work, leftover)), leftover
    assert not os.path.exists(args[0]), "скриптът се самоизтрива"


def test_rename_script_restores_and_restarts_the_old_exe_when_setup_fails(tmp_path, monkeypatch):
    local, new_dir, legacy, exe, captured, args = _start_rename(tmp_path, monkeypatch)
    setup, _calls = _installer(new_dir, legacy, exit_code=2)
    sim = _RenameBatch(args[0], args[1:], captured["env"], lambda pid: False, setup,
                       lambda path, env: None)
    sim.run()
    assert sim.started == [exe]
    assert open(exe, "rb").read() == b"MZold", "старото .exe е върнато"
    assert "PACHO_UPDATE_STARTED_MARKER" not in sim.env
    assert updater.read_rename_failure() == {"version": "3.79.0", "reason": "setup"}
    assert os.path.exists(os.path.join(legacy, "pacho_logistic.db")), "данните не са пипани"
    assert not updater.use_setup_for_update("3.79.0")


def test_rename_script_never_starts_the_old_exe_after_a_successful_setup(tmp_path, monkeypatch):
    """Новото .exe не потвърждава старта — СТАРОТО не се пуска: новото може
    вече да е преместило данните, а старото би направило нова празна база."""
    local, new_dir, legacy, exe, captured, args = _start_rename(tmp_path, monkeypatch)
    setup, _calls = _installer(new_dir, legacy)
    sim = _RenameBatch(args[0], args[1:], captured["env"], lambda pid: False, setup,
                       lambda path, env: None)
    sim.run()
    assert sim.started == [os.path.join(new_dir, "PHLogistics.exe")]
    work = os.path.dirname(args[0])
    assert "FAILED" in open(os.path.join(work, "ph_rename.log")).read()
    assert os.path.exists(os.path.join(work, "old_exe.bak")), "копието остава за ръчно връщане"


def test_rename_script_gives_up_when_the_program_does_not_close(tmp_path, monkeypatch):
    local, new_dir, legacy, exe, captured, args = _start_rename(tmp_path, monkeypatch)
    setup, calls = _installer(new_dir, legacy)
    sim = _RenameBatch(args[0], args[1:], captured["env"], lambda pid: True, setup,
                       lambda path, env: None)
    sim.run()
    assert calls == [] and sim.started == []
    assert updater.read_rename_failure()["reason"] == "busy"


def test_install_via_setup_requires_a_checksum_and_a_safe_legacy_install(tmp_path, monkeypatch):
    _as_legacy_local(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="контролна сума"):
        updater.install_via_setup("https://example.invalid/s.exe", None, version="3.79.0")
    monkeypatch.setattr(updater.net, "urlopen", lambda req, timeout=120: _Resp(SETUP_PAYLOAD))
    with pytest.raises(RuntimeError, match="повреден"):
        updater.install_via_setup("https://example.invalid/s.exe", "0" * 64, version="3.79.0")
    updater._rename_safety_cache.clear()
    monkeypatch.setattr(lm, "read_share_paths", lambda: None)
    with pytest.raises(RuntimeError, match="не е възможен"):
        updater.install_via_setup("https://example.invalid/s.exe", "0" * 64)


def test_cli_hook_uses_the_local_setup(tmp_path, monkeypatch):
    _local, _new, legacy, _exe = _as_legacy_local(tmp_path, monkeypatch)
    launched = []
    monkeypatch.setattr(updater, "_launch_rename", lambda path, version: launched.append(path))
    source = tmp_path / "PHLogistics-Setup.exe"
    source.write_bytes(SETUP_PAYLOAD)
    assert updater.run_complete_rename_cli(["x.exe", updater.RENAME_CLI_FLAG, str(source)]) == 0
    assert launched and open(launched[0], "rb").read() == SETUP_PAYLOAD
    assert os.path.dirname(launched[0]) != legacy
    assert updater.run_complete_rename_cli(["x.exe", updater.RENAME_CLI_FLAG]) == 2
    monkeypatch.setattr(updater, "is_frozen_windows", lambda: False)
    assert updater.run_complete_rename_cli(["x.exe", updater.RENAME_CLI_FLAG, str(source)]) == 2
    src = read_source("app.py")
    assert src.index("updater.RENAME_CLI_FLAG in sys.argv") < src.index("single_instance.acquire()")


def test_complete_rename_fetches_the_setup_of_the_current_version(monkeypatch):
    sums = ["b" * 64 + "  PHLogistics-Setup.exe"]
    seen = []
    fake = _release(["PHLogistics-Setup.exe"], sums)

    def urlopen(req, timeout=8):
        seen.append(req.full_url)
        return fake(req, timeout)
    monkeypatch.setattr(updater.net, "urlopen", urlopen)
    called = {}
    monkeypatch.setattr(updater, "install_via_setup",
                        lambda url, sha, **kw: called.update(url=url, sha=sha, **kw))
    updater.complete_rename()
    assert seen[0].endswith("/releases/tags/v%s" % updater.__version__)
    assert called["url"].endswith("/PHLogistics-Setup.exe") and called["sha"] == "b" * 64
    assert called["version"] == updater.__version__ and called["ignore_failed_marker"]


# ---------------------------------------------------------------- интерфейс

def test_dashboard_offers_the_rename_only_to_admins(admin_client, employee_client, monkeypatch):
    monkeypatch.setattr(updater, "rename_status",
                        lambda: {"can_complete": True, "failure": None})
    page = admin_client.get("/").get_data(as_text=True)
    assert "завършете преминаването към новите имена" in page
    assert 'action="/update/complete-rename"' in page
    page = employee_client.get("/").get_data(as_text=True)
    assert "завършете преминаването" not in page


def test_dashboard_shows_the_previous_rename_failure(admin_client, monkeypatch):
    monkeypatch.setattr(updater, "rename_status", lambda: {
        "can_complete": True, "failure": {"version": "3.79.0", "reason": "busy"}})
    page = admin_client.get("/").get_data(as_text=True)
    assert "програмата не се затвори навреме" in page


def test_complete_rename_route(admin_client, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "can_complete_rename", lambda: True)
    monkeypatch.setattr(updater, "complete_rename", lambda: calls.append(1))
    resp = post_with_csrf(admin_client, "/update/complete-rename", {})
    assert resp.status_code == 200 and calls == [1]
    assert "Обновяване до версия" in resp.get_data(as_text=True)
    monkeypatch.setattr(updater, "can_complete_rename", lambda: False)
    resp = post_with_csrf(admin_client, "/update/complete-rename", {}, follow_redirects=True)
    assert calls == [1]
    assert "не е възможно от тази инсталация" in resp.get_data(as_text=True)


def test_complete_rename_route_needs_admin(employee_client, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "complete_rename", lambda: calls.append(1))
    post_with_csrf(employee_client, "/update/complete-rename", {})
    assert calls == []


def test_update_now_uses_the_setup_for_a_legacy_local_install(admin_client, monkeypatch):
    monkeypatch.setattr(updater, "check_for_update", lambda: {
        "available": True, "latest": "9.9.9", "current": "1.0.0", "download": "u",
        "expected_sha256": "s", "setup_download": "su", "setup_sha256": "ss"})
    monkeypatch.setattr(updater, "use_setup_for_update", lambda v: True)
    calls = []
    monkeypatch.setattr(updater, "install_via_setup", lambda *a, **k: calls.append(("setup", a)))
    monkeypatch.setattr(updater, "install_update", lambda *a, **k: calls.append(("exe", a)))
    post_with_csrf(admin_client, "/update/install", {})
    assert calls == [("setup", ("su", "ss"))]


def test_migration_report_is_shown_once_to_the_admin(admin_client, monkeypatch):
    import routes_admin
    monkeypatch.setattr(routes_admin, "_migration_report_checked", set())
    folder = os.path.dirname(appconfig.CONFIG_PATH)
    with open(os.path.join(folder, lm.REPORT_NAME), "w", encoding="utf-8") as fh:
        json.dump({"status": "pointer", "reason": "shared", "legacy_dir": r"C:\Old\PachoLogistic",
                   "new_dir": folder, "shown": False}, fh)
    page = admin_client.get("/").get_data(as_text=True)
    assert "старата папка е споделена в мрежата" in page
    assert "данните остават в C:\\Old\\PachoLogistic" in page
    monkeypatch.setattr(routes_admin, "_migration_report_checked", set())
    assert "споделена в мрежата" not in admin_client.get("/").get_data(as_text=True)


def test_system_page_says_names_stay_for_legacy_named_installs(admin_client, monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "can_complete_rename", lambda: False)
    page = admin_client.get("/admin/system").get_data(as_text=True)
    assert "запазват старите си имена" not in page
    monkeypatch.setattr(appconfig, "CONFIG_PATH", str(tmp_path / "pacho_config.json"))
    page = admin_client.get("/admin/system").get_data(as_text=True)
    assert "запазват старите си имена" in page


# ---------------------------------------------------------------- инсталатор и CI (по изходния код)

def test_installer_moves_to_the_new_folder_and_deletes_only_old_program_files():
    src = read_source("installer.iss")
    assert '#define MyAppExeName "PHLogistics.exe"' in src
    assert "OutputBaseFilename=PHLogistics-Setup" in src
    assert "UsePreviousAppDir=no" in src
    assert "AppId={{6C6E1F0E-6E52-4B90-9B7B-9E7F2B6E6A21}" in src
    assert 'Source: "dist\\PHLogistics.exe"' in src
    section = src[src.index("[InstallDelete]"):src.index("[UninstallDelete]")]
    legacy_lines = [l for l in section.splitlines() if "LegacyDir" in l]
    assert len(legacy_lines) == 6
    for line in legacy_lines:
        assert "Check: LegacyProgramFilesRemovable" in line
        assert re.search(r'Name: "\{#LegacyDir\}\\(\{#LegacyExeName\}(\.old|\.new)?|unins000\.(exe|dat|msg))"',
                         line), line
    for data in ("pacho_logistic", "pacho_config", ".secret_key", "attachments", "Type: filesandordirs"):
        assert data not in section, "инсталаторът не бива да трие данни (%s)" % data
    assert "LegacyDirIsShared" in src and "LanmanServer\\Shares" in src


def test_release_publishes_new_names_legacy_aliases_and_gates_on_migration():
    src = read_source(".github", "workflows", "release.yml")
    assert "--name PHLogistics" in src
    for name in ("PHLogistics.exe", "PHLogistics-Setup.exe", "PachoLogistic.exe",
                 "PachoLogistic-Setup.exe"):
        assert '"  %s"' % name in src, "SHA256SUMS.txt без ред за %s" % name
    files = src[src.index("files: |"):src.index("generate_release_notes")]
    for path in ("dist/PHLogistics.exe", "dist_installer/PHLogistics-Setup.exe",
                 "dist/PachoLogistic.exe", "dist_installer/PachoLogistic-Setup.exe",
                 "SHA256SUMS.txt"):
        assert path in files
    publish = src.index("- name: Publish release")
    for step in ("-Scenario setup", "-Scenario cli", "Copy-Item dist\\PHLogistics.exe dist\\PachoLogistic.exe"):
        assert src.index(step) < publish, step
    assert src.index("- name: Legacy download names") < src.index("- name: Generate SHA256SUMS.txt")
    vt = src[src.index("- name: VirusTotal scan"):]
    assert "dist/PHLogistics.exe dist_installer/PHLogistics-Setup.exe" in vt
    vt_manual = read_source(".github", "workflows", "virustotal.yml")
    assert '--pattern "PHLogistics.exe"' in vt_manual


def test_ci_migration_script_checks_real_sqlite_data(tmp_path):
    """Помощникът на CI сам по себе си: create → (ръчно „преместване“) → check."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import ci_rename_fixture as fx
    finally:
        sys.path.pop(0)
    legacy, new = tmp_path / "PachoLogistic", tmp_path / "PHLogistics"
    legacy.mkdir()
    new.mkdir()
    fx.create(str(legacy))
    assert fx.check(str(new), str(legacy)) == 1, "празната нова папка не бива да минава"
    for old, newname in (("pacho_logistic.db", "ph_logistics.db"), ("pacho_config.json", "ph_config.json"),
                         (".secret_key", ".secret_key"), ("attachments", "attachments")):
        os.replace(str(legacy / old), str(new / newname))
    os.remove(str(legacy / "unins000.dat"))
    (new / "ph_migration.json").write_text('{"status": "moved"}')
    assert fx.check(str(new), str(legacy)) == 0
    ps1 = read_source("scripts", "ci_rename_migration_test.ps1")
    assert "--complete-rename-with" in ps1 and "/VERYSILENT" in ps1
    assert "PACHO_DISABLE_AUTO_UPDATE" in ps1
