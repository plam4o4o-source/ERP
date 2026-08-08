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


# ---------------------------------------------------------------- m002: public_token
# Заявка: „всеки, който сканира с телефон баркода на някой от документите,
# да му се зареди директно документа, без да има нужда от домейна, който е
# в програмата“ — публичен преглед през нов, непредвидим public_token
# (routes_documents.public_document_view), различен от предвидимия barcode.

def test_public_token_migration_backfills_existing_documents_without_one(con, db_module):
    """Документ, издаден преди тази версия (значи БЕЗ public_token в
    базата), трябва да получи такъв при миграцията — иначе никога не би
    получил работещ публичен адрес дори при следващ преглед/печат."""
    con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data)"
        " VALUES ('cmr', '0001/2020', 2020, 1, 'CMR-OLD-0001', NULL, '{}')"
    )
    con.commit()
    db_module._m002_public_token(con)
    row = con.execute(
        "SELECT public_token FROM documents WHERE barcode = 'CMR-OLD-0001'"
    ).fetchone()
    assert row["public_token"]
    assert len(row["public_token"]) == 32  # secrets.token_hex(16) -> 32 hex символа


def test_public_token_migration_does_not_overwrite_an_existing_token(con, db_module):
    con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data)"
        " VALUES ('cmr', '0002/2020', 2020, 2, 'CMR-OLD-0002', 'already-set-token', '{}')"
    )
    con.commit()
    db_module._m002_public_token(con)
    row = con.execute(
        "SELECT public_token FROM documents WHERE barcode = 'CMR-OLD-0002'"
    ).fetchone()
    assert row["public_token"] == "already-set-token"


def test_public_token_migration_gives_each_document_a_distinct_token(con, db_module):
    for i in range(3):
        con.execute(
            "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data)"
            " VALUES ('cmr', ?, 2020, ?, ?, NULL, '{}')",
            ("000%d/2020" % i, i, "CMR-OLD-DISTINCT-%d" % i),
        )
    con.commit()
    db_module._m002_public_token(con)
    tokens = [r["public_token"] for r in con.execute(
        "SELECT public_token FROM documents WHERE barcode LIKE 'CMR-OLD-DISTINCT-%'"
    )]
    assert len(tokens) == 3
    assert len(set(tokens)) == 3  # без сблъсъци
