# -*- coding: utf-8 -*-
"""Тестове за рамката за миграции (M1): PRAGMA user_version + подредени,
идемпотентни стъпки (db.MIGRATIONS/_apply_migrations), която замества
ad-hoc единичната поправка от Фаза 1."""


def test_user_version_matches_migration_count_after_init(con, db_module):
    version = con.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(db_module.MIGRATIONS)
    assert version >= 1  # поне must_change_password стъпката съществува


def test_migrations_list_is_not_empty(db_module):
    assert len(db_module.MIGRATIONS) >= 1


def test_reapplying_migrations_is_idempotent(db_module):
    """Повторно извикване на _apply_migrations (симулира рестарт на
    приложението) не трябва да гръмне и не трябва да променя user_version
    отвъд действителния брой регистрирани стъпки."""
    con = db_module.get_db()
    db_module._apply_migrations(con)
    db_module._apply_migrations(con)
    version = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    assert version == len(db_module.MIGRATIONS)


def test_migration_step_not_rerun_once_applied(db_module, monkeypatch):
    """Регистрираме фалшива допълнителна стъпка и проверяваме, че след
    като веднъж е приложена (user_version вдигнат), повторно извикване на
    _apply_migrations НЕ я изпълнява отново."""
    calls = []

    def fake_step(con):
        calls.append(1)

    monkeypatch.setattr(db_module, "MIGRATIONS", list(db_module.MIGRATIONS) + [fake_step])

    con = db_module.get_db()
    db_module._apply_migrations(con)  # трябва да изпълни само новата (последната) стъпка
    assert len(calls) == 1

    db_module._apply_migrations(con)  # втори път — вече приложена, не се препоглежда
    assert len(calls) == 1
    con.close()


def test_ensure_column_adds_missing_column_only_once(con, db_module):
    db_module._ensure_column(con, "clients", "test_extra_col", "TEXT NOT NULL DEFAULT ''")
    cols_after_first = [r["name"] for r in con.execute("PRAGMA table_info(clients)")]
    assert cols_after_first.count("test_extra_col") == 1

    db_module._ensure_column(con, "clients", "test_extra_col", "TEXT NOT NULL DEFAULT ''")
    cols_after_second = [r["name"] for r in con.execute("PRAGMA table_info(clients)")]
    assert cols_after_second.count("test_extra_col") == 1  # не се дублира
