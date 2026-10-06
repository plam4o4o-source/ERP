# -*- coding: utf-8 -*-
"""Одит (06.10.2026): данни за CI проверката на прехода към новите имена
(release.yml → scripts/ci_rename_migration_test.ps1, само на windows-latest).

    python scripts/ci_rename_fixture.py create <старата папка>
    python scripts/ci_rename_fixture.py check <новата папка> <старата папка>
    python scripts/ci_rename_fixture.py create-portable <папка>
    python scripts/ci_rename_fixture.py check-portable <папка>
    python scripts/ci_rename_fixture.py check-shared <папка>

`create` прави фалшива стара инсталация с истинска SQLite база (таблица с
маркер), pacho_config.json, .secret_key и прикачен файл; `check` проверява,
че всичко е преместено в новата папка с новите имена, а старите програмни
файлове са махнати. Изход 1 при провал (с обяснение).

Одит (07.10.2026): преносима инсталация (.exe извън папките по
подразбиране): `create-portable` прави същите данни (без деинсталатор, с
pacho_startup_ci.log); `check-portable` — всичко е преименувано НА МЯСТО
(ph_logistics.db, ph_config.json, ph_startup_ci.log), самото .exe — не;
`check-shared` — папката е споделена, нищо не е преименувано, а отчетът
казва защо."""
import json
import os
import sqlite3
import sys

MARKER = "migration-ok"
SECRET = "c1" * 32  # 64 hex символа — валиден ключ, не се „поправя“ от db.py
CONFIG = {"network_port": 5000, "ci_marker": MARKER}
ATTACHMENT = "ci attachment"


def create(legacy_dir):
    os.makedirs(os.path.join(legacy_dir, "attachments", "1"), exist_ok=True)
    con = sqlite3.connect(os.path.join(legacy_dir, "pacho_logistic.db"))
    con.execute("CREATE TABLE ci_marker (value TEXT)")
    con.execute("INSERT INTO ci_marker (value) VALUES (?)", (MARKER,))
    con.commit()
    con.close()
    with open(os.path.join(legacy_dir, "pacho_config.json"), "w", encoding="utf-8") as fh:
        json.dump(CONFIG, fh)
    with open(os.path.join(legacy_dir, ".secret_key"), "w", encoding="utf-8") as fh:
        fh.write(SECRET)
    with open(os.path.join(legacy_dir, "attachments", "1", "a.txt"), "w", encoding="utf-8") as fh:
        fh.write(ATTACHMENT)
    with open(os.path.join(legacy_dir, "unins000.dat"), "wb") as fh:
        fh.write(b"fake uninstaller data")
    print("fake legacy install created in %s" % legacy_dir)


LEGACY_LOG = "pacho_startup_ci.log"
NEW_LOG = "ph_startup_ci.log"


def create_portable(folder):
    create(folder)
    os.remove(os.path.join(folder, "unins000.dat"))
    with open(os.path.join(folder, LEGACY_LOG), "w", encoding="utf-8") as fh:
        fh.write("стар лог\n")
    print("fake portable install created in %s" % folder)


def _db_rows(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT value FROM ci_marker").fetchall()
    except sqlite3.Error as exc:
        return [("error: %s" % exc,)]
    finally:
        con.close()


def _read_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _common_portable(folder, need):
    secret = os.path.join(folder, ".secret_key")
    need(os.path.exists(secret) and open(secret, encoding="utf-8").read().strip() == SECRET,
         ".secret_key changed or missing")
    att = os.path.join(folder, "attachments", "1", "a.txt")
    need(os.path.exists(att) and open(att, encoding="utf-8").read() == ATTACHMENT,
         "attachments\\1\\a.txt changed or missing")
    need(os.path.exists(os.path.join(folder, "PachoLogistic.exe")),
         "the exe itself must keep its name (shortcuts point to it)")


def _finish(problems, ok_text):
    if problems:
        print("CHECK FAILED:")
        for text in problems:
            print("  - " + text)
        return 1
    print(ok_text)
    return 0


def check_portable(folder):
    problems = []

    def need(cond, text):
        if not cond:
            problems.append(text)

    db_path = os.path.join(folder, "ph_logistics.db")
    need(os.path.exists(db_path), "ph_logistics.db is missing")
    if os.path.exists(db_path):
        rows = _db_rows(db_path)
        need(rows == [(MARKER,)], "marker row not found in ph_logistics.db: %r" % rows)
    cfg = _read_json(os.path.join(folder, "ph_config.json"))
    need(cfg.get("ci_marker") == MARKER, "ph_config.json is not the renamed config: %r" % cfg)
    need(not cfg.get("db_path"), "ph_config.json points elsewhere (db_path=%r)" % cfg.get("db_path"))
    for name in ("pacho_logistic.db", "pacho_logistic.db-wal", "pacho_logistic.db-shm",
                 "pacho_logistic.db-journal", "pacho_config.json", LEGACY_LOG):
        need(not os.path.exists(os.path.join(folder, name)), "%s was not renamed" % name)
    need(os.path.exists(os.path.join(folder, NEW_LOG)), "%s is missing" % NEW_LOG)
    _common_portable(folder, need)
    report = _read_json(os.path.join(folder, "ph_migration.json"))
    need(report.get("status") == "renamed" and report.get("kind") == "in_place",
         "ph_migration.json does not say renamed in place: %r" % report)
    return _finish(problems, "portable check OK: %s" % report)


def check_shared(folder):
    problems = []

    def need(cond, text):
        if not cond:
            problems.append(text)

    db_path = os.path.join(folder, "pacho_logistic.db")
    need(os.path.exists(db_path), "pacho_logistic.db is missing (it must keep its name)")
    if os.path.exists(db_path):
        rows = _db_rows(db_path)
        need(rows == [(MARKER,)], "marker row not found in pacho_logistic.db: %r" % rows)
    cfg = _read_json(os.path.join(folder, "pacho_config.json"))
    need(cfg.get("ci_marker") == MARKER, "pacho_config.json is missing or changed: %r" % cfg)
    for name in ("ph_logistics.db", "ph_config.json", NEW_LOG):
        need(not os.path.exists(os.path.join(folder, name)),
             "%s exists - something was renamed in a shared folder" % name)
    need(os.path.exists(os.path.join(folder, LEGACY_LOG)), "%s was renamed" % LEGACY_LOG)
    _common_portable(folder, need)
    report = _read_json(os.path.join(folder, "ph_migration.json"))
    need(report.get("status") == "kept" and report.get("reason") == "shared",
         "ph_migration.json does not say kept/shared: %r" % report)
    return _finish(problems, "shared check OK: %s" % report)


def check(new_dir, legacy_dir):
    problems = []

    def need(cond, text):
        if not cond:
            problems.append(text)

    db_path = os.path.join(new_dir, "ph_logistics.db")
    need(os.path.exists(db_path), "ph_logistics.db is missing in the new folder")
    if os.path.exists(db_path):
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute("SELECT value FROM ci_marker").fetchall()
        except sqlite3.Error as exc:
            rows = [("error: %s" % exc,)]
        finally:
            con.close()
        need(rows == [(MARKER,)], "marker row not found in ph_logistics.db: %r" % rows)
    cfg_path = os.path.join(new_dir, "ph_config.json")
    need(os.path.exists(cfg_path), "ph_config.json is missing in the new folder")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        need(cfg.get("ci_marker") == MARKER, "ph_config.json is not the moved config: %r" % cfg)
        need(not cfg.get("db_path"), "ph_config.json points elsewhere (db_path=%r)"
             % cfg.get("db_path"))
    secret = os.path.join(new_dir, ".secret_key")
    need(os.path.exists(secret) and open(secret, encoding="utf-8").read().strip() == SECRET,
         ".secret_key was not moved")
    att = os.path.join(new_dir, "attachments", "1", "a.txt")
    need(os.path.exists(att) and open(att, encoding="utf-8").read() == ATTACHMENT,
         "attachments\\1\\a.txt was not moved")
    for name in ("pacho_logistic.db", "pacho_config.json"):
        need(not os.path.exists(os.path.join(new_dir, name)),
             "%s kept its old name in the new folder" % name)
        need(not os.path.exists(os.path.join(legacy_dir, name)),
             "%s is still in the old folder" % name)
    for name in ("PachoLogistic.exe", "unins000.dat", "unins000.exe"):
        need(not os.path.exists(os.path.join(legacy_dir, name)),
             "old program file %s was not removed" % name)
    report_path = os.path.join(new_dir, "ph_migration.json")
    report = {}
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    need(report.get("status") == "moved", "ph_migration.json does not say moved: %r" % report)
    if problems:
        print("MIGRATION CHECK FAILED:")
        for text in problems:
            print("  - " + text)
        return 1
    print("migration check OK: %s" % report)
    return 0


def main(argv):
    if len(argv) >= 3 and argv[1] == "create":
        create(argv[2])
        return 0
    if len(argv) >= 4 and argv[1] == "check":
        return check(argv[2], argv[3])
    if len(argv) >= 3 and argv[1] == "create-portable":
        create_portable(argv[2])
        return 0
    if len(argv) >= 3 and argv[1] == "check-portable":
        return check_portable(argv[2])
    if len(argv) >= 3 and argv[1] == "check-shared":
        return check_shared(argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
