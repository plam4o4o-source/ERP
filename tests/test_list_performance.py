# -*- coding: utf-8 -*-
"""Одит (01.10.2026): бързина и трафик на списъците (F1a/F1b/F1c/F5/F6/F8)
плюс съобщението при пълен диск (O9) и предупреждението за дублиран клиент
(U12). Планът на заявките се проверява с EXPLAIN QUERY PLAN върху
истинските заявки, които приложението изпраща."""
import gzip
import json
import shutil
import sqlite3

import pytest

from conftest import post_with_csrf


class _RecordingConnection(sqlite3.Connection):
    statements = None

    def execute(self, sql, params=()):
        if _RecordingConnection.statements is not None:
            _RecordingConnection.statements.append((sql, tuple(params)))
        return super().execute(sql, params)


@pytest.fixture
def recorded(monkeypatch):
    """Всички (sql, params), изпратени през sqlite3.connect по време на теста."""
    real_connect = sqlite3.connect
    log = []
    _RecordingConnection.statements = log

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", _RecordingConnection)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield log
    _RecordingConnection.statements = None


def _plan(db_module, sql, params):
    con = db_module.get_db()
    try:
        return " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params))
    finally:
        con.close()


def _add_docs(db_module, n, doc_type="cmr", client="Клиент"):
    con = db_module.get_db()
    start = con.execute("SELECT COALESCE(MAX(id), 0) FROM documents").fetchone()[0]
    con.executemany(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data, created_by)"
        " VALUES (?, ?, 2026, ?, ?, ?, ?, 1)",
        [(doc_type, "%04d/2026" % (start + i), start + i, "%s-B-%d" % (doc_type, start + i),
          "t-%d" % (start + i),
          json.dumps({"consignee_name": "%s %d" % (client, i % 7)}, ensure_ascii=False))
         for i in range(1, n + 1)])
    con.commit()
    con.close()


# ---------------------------------------------------------------- F1a табло

def test_dashboard_recent_documents_do_not_sort_the_whole_table(admin_client, db_module, recorded):
    """IN (6 типа) караше планера да сортира всички нефактурни редове
    (114 MB при 20 000 документа) заради 10 на екрана."""
    _add_docs(db_module, 15)
    assert admin_client.get("/").status_code == 200
    recent = [s for s in recorded if "LIMIT 10" in s[0] and "ORDER BY d.id DESC" in s[0]]
    assert recent, "заявката за последните документи не е открита"
    plan = _plan(db_module, *recent[0])
    assert "TEMP B-TREE" not in plan, plan


# ---------------------------------------------------------------- F1b пагинация

def test_paginate_documents_loads_full_rows_only_for_the_page(flask_app, db_module, recorded):
    import appcore
    _add_docs(db_module, 23)
    con = db_module.get_db()
    expected = [r["id"] for r in con.execute(
        "SELECT id FROM documents WHERE doc_type NOT IN ('invoice_br') ORDER BY id DESC")]
    del recorded[:]
    seen = []
    for page in (1, 2, 3, 99):
        docs, real_page, total_pages, total = appcore.paginate_documents(
            con, "WHERE d.doc_type NOT IN (?)", ["invoice_br"], page, page_size=10)
        assert (total_pages, total) == (3, 23)
        if page != 99:
            seen += [d["id"] for d in docs]
        else:
            assert real_page == 3 and [d["id"] for d in docs] == expected[20:]
        assert all("author" in d.keys() and "data" in d.keys() for d in docs)
    con.close()
    assert seen == expected
    full_rows = [s for s in recorded if "d.*" in s[0]]
    assert full_rows and all("OFFSET" not in s[0] and "d.id IN (" in s[0] for s in full_rows), full_rows


def test_deep_page_sorts_ids_from_an_index(flask_app, db_module, recorded, monkeypatch):
    """Дълбока страница: id-тата се сортират от покриващ индекс, вместо да
    се обхождат листата на таблицата (62 → 1 MB при 20 000 документа)."""
    import appcore
    monkeypatch.setattr(appcore, "_DEEP_OFFSET", 10)
    _add_docs(db_module, 30)
    con = db_module.get_db()
    del recorded[:]
    docs = appcore.paginate_documents(con, "WHERE d.doc_type NOT IN (?)", ["invoice_br"], 3,
                                      page_size=10)[0]
    con.close()
    assert [d["number"] for d in docs] == ["%04d/2026" % n for n in range(10, 0, -1)]
    id_query = [s for s in recorded if s[0].startswith("SELECT d.id FROM documents d")]
    assert id_query
    assert "COVERING INDEX" in _plan(db_module, *id_query[0])


# ---------------------------------------------------------------- F1c индекси

def test_client_type_index_replaces_the_single_column_one(flask_app, db_module):
    con = db_module.get_db()
    cols = [r["name"] for r in con.execute("PRAGMA index_info(idx_documents_client_type)")]
    names = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    con.close()
    assert cols == ["client_name", "doc_type"]
    assert "idx_documents_client_name" not in names


def test_client_history_and_grouping_read_only_the_index(admin_client, db_module, recorded):
    """Историята на клиента и „Групирай по клиент“ подреждат по покриващия
    индекс (client_name, doc_type), без да четат ~7 KB JSON на ред."""
    post_with_csrf(admin_client, "/clients/new", {"name": "Клиент 1"}, csrf_source_url="/clients/new")
    _add_docs(db_module, 30)
    con = db_module.get_db()
    client_id = con.execute("SELECT id FROM clients WHERE name = 'Клиент 1'").fetchone()["id"]
    con.close()
    del recorded[:]
    body = admin_client.get("/clients/%d/edit" % client_id).get_data(as_text=True)
    assert "0029/2026" in body or "0022/2026" in body
    history = [s for s in recorded if "ci_lower(client_name) = ci_lower(?)" in s[0]]
    assert history
    assert "COVERING INDEX idx_documents_client_type" in _plan(db_module, *history[0])

    del recorded[:]
    assert admin_client.get("/docs?group=client").status_code == 200
    ids = [s for s in recorded if s[0].startswith("SELECT d.id FROM documents d")]
    assert ids and "COVERING INDEX idx_documents_client_type" in _plan(db_module, *ids[0])


# ---------------------------------------------------------------- F5 шаблони

def _count_calls(monkeypatch, flask_app, name):
    calls = []
    real = flask_app.jinja_env.globals[name]

    def wrapper(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setitem(flask_app.jinja_env.globals, name, wrapper)
    return calls


@pytest.mark.parametrize("url,doc_type", [("/docs", "cmr"), ("/docs?group=client", "cmr"),
                                          ("/invoices", "invoice_br")])
def test_list_rows_do_not_recompute_icons_and_csrf(admin_client, flask_app, db_module,
                                                   monkeypatch, url, doc_type):
    """Иконите и CSRF токенът се смятат веднъж за страницата, не на ред."""
    icons = _count_calls(monkeypatch, flask_app, "icon")
    csrf = _count_calls(monkeypatch, flask_app, "csrf_token")
    _add_docs(db_module, 2, doc_type)
    admin_client.get(url)
    few_icons, few_csrf = len(icons), len(csrf)
    _add_docs(db_module, 40, doc_type)
    del icons[:], csrf[:]
    body = admin_client.get(url).get_data(as_text=True)
    assert body.count('href="/doc/') >= 84
    assert len(icons) - few_icons < 10, len(icons) - few_icons
    assert len(csrf) == few_csrf


# ---------------------------------------------------------------- F6 трафик

def test_html_is_gzipped_when_the_browser_accepts_it(admin_client, db_module):
    _add_docs(db_module, 20)
    plain = admin_client.get("/docs")
    assert "Content-Encoding" not in plain.headers
    zipped = admin_client.get("/docs", headers={"Accept-Encoding": "gzip, deflate, br"})
    assert zipped.headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in zipped.headers.get("Vary", "")
    assert gzip.decompress(zipped.data) == plain.data
    assert len(zipped.data) < len(plain.data) / 3
    refused = admin_client.get("/docs", headers={"Accept-Encoding": "gzip;q=0, identity"})
    assert "Content-Encoding" not in refused.headers


def test_files_and_small_responses_are_not_gzipped(admin_client):
    for url in ("/static/app.js", "/barcode/ABC-123.svg", "/update/pending-restart"):
        resp = admin_client.get(url, headers={"Accept-Encoding": "gzip"})
        assert "Content-Encoding" not in resp.headers, url


def test_static_files_are_versioned_and_cached_long(admin_client):
    from version import __version__
    body = admin_client.get("/").get_data(as_text=True)
    assert "/static/app.js?v=%s" % __version__ in body
    assert "/static/style.css?v=%s" % __version__ in body
    versioned = admin_client.get("/static/app.js?v=%s" % __version__)
    assert versioned.status_code == 200
    assert versioned.cache_control.max_age >= 30 * 24 * 3600
    assert versioned.cache_control.immutable
    plain = admin_client.get("/static/app.js")
    assert not plain.cache_control.max_age


# ---------------------------------------------------------------- F8 памет за прегледи

def test_preview_store_is_capped_by_total_size(flask_app, monkeypatch):
    import appcore
    monkeypatch.setattr(appcore, "_PREVIEW_MAX_BYTES", 10000)
    monkeypatch.setattr(appcore, "_preview_store", type(appcore._preview_store)())
    big = {"items": ["x" * 3000]}
    tokens = [appcore._store_preview("doc", ("cmr", big, None, None)) for _ in range(6)]
    with flask_app.test_request_context():
        assert appcore._get_preview(tokens[-1], "doc") is not None
        assert appcore._get_preview(tokens[0], "doc") is None
    assert sum(e[4] for e in appcore._preview_store.values()) <= 10000
    # Един преглед над тавана все пак се пази — иначе операторът не го вижда.
    huge = appcore._store_preview("doc", ("cmr", {"items": ["y" * 20000]}, None, None))
    with flask_app.test_request_context():
        assert appcore._get_preview(huge, "doc") is not None


# ---------------------------------------------------------------- O9 пълен диск

def _error_client(flask_app, exc):
    def boom():
        raise exc
    flask_app.add_url_rule("/__boom", "boom_test", boom)
    return flask_app.test_client()


@pytest.mark.parametrize("exc", [sqlite3.OperationalError("database or disk is full"),
                                 OSError(28, "No space left on device")])
def test_disk_full_gets_its_own_message(flask_app, exc):
    resp = _error_client(flask_app, exc).get("/__boom")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 503
    assert "Дискът е пълен" in body
    assert "мрежата/споделената папка" not in body


def test_disk_io_error_with_no_free_space_is_disk_full(flask_app, monkeypatch):
    import appcore
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10 ** 9, 10 ** 9, 4096))
    body = _error_client(flask_app, sqlite3.OperationalError("disk I/O error")).get("/__boom").get_data(as_text=True)
    assert "Дискът е пълен" in body


def test_disk_io_error_with_free_space_keeps_the_network_hint(flask_app, monkeypatch):
    import appcore
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10 ** 12, 10, 10 ** 11))
    resp = _error_client(flask_app, sqlite3.OperationalError("disk I/O error")).get("/__boom")
    assert resp.status_code == 503
    assert "Дискът е пълен" not in resp.get_data(as_text=True)


# ---------------------------------------------------------------- U12 адресна книга

def test_duplicate_client_name_warns_but_saves(admin_client, db_module):
    post_with_csrf(admin_client, "/clients/new", {"name": "Фирма Алфа ООД"},
                   csrf_source_url="/clients/new")
    resp = post_with_csrf(admin_client, "/clients/new", {"name": "  фирма алфа оод "},
                          csrf_source_url="/clients/new", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "вече има клиент" in body
    con = db_module.get_db()
    rows = con.execute("SELECT id FROM clients ORDER BY id").fetchall()
    con.close()
    assert len(rows) == 2
    # Повторно записване на СЪЩИЯ клиент не е дубликат.
    resp = post_with_csrf(admin_client, "/clients/%d/edit" % rows[0]["id"],
                          {"name": "Фирма Бета ООД"}, csrf_source_url="/clients/new",
                          follow_redirects=True)
    assert "вече има клиент" not in resp.get_data(as_text=True)


def test_clients_list_uses_the_same_edit_wording(admin_client):
    post_with_csrf(admin_client, "/clients/new", {"name": "Гама"}, csrf_source_url="/clients/new")
    body = admin_client.get("/clients").get_data(as_text=True)
    assert "Редактирай" in body and "Редакция<" not in body
