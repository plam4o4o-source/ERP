# -*- coding: utf-8 -*-
"""Одит (15.10.2026, PERF2): връзката на заявката се преизползва между
заявките (PERF2-11 — SQLite пази кеша си от страници) и по-бърз PDF на голяма
фактура (PERF2-12)."""
import io
import os
import re
import sqlite3
import subprocess
import sys
import textwrap

import pytest


@pytest.fixture
def opens(db_module, monkeypatch):
    """Брои отварянията на НОВИ връзки за заявките (db._open_connection)."""
    real = db_module._open_connection
    log = []

    def counting(*args, **kwargs):
        log.append(kwargs.get("check_same_thread", True))
        return real(*args, **kwargs)

    monkeypatch.setattr(db_module, "_open_connection", counting)
    db_module.close_cached_connections()
    yield log
    db_module.close_cached_connections()


def _to_delete_journal(db_module):
    db_module.close_cached_connections()
    con = sqlite3.connect(db_module.DB_PATH)
    assert con.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    con.close()


def _add_client(db_module, name):
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("INSERT INTO clients (name, city) VALUES (?, 'Пловдив')", (name,))
    con.commit()
    con.close()


# ---------------------------------------------------------------- PERF2-11 преизползване

def test_requests_reuse_one_connection(admin_client, db_module, opens):
    """Без склада всяка заявка отваряше нова връзка (наново схема и кеш)."""
    for url in ("/", "/docs", "/invoices", "/clients", "/docs"):
        assert admin_client.get(url).status_code == 200
    assert len(opens) <= 1, opens


def test_request_handle_is_closed_but_the_connection_waits_in_the_pool(flask_app, db_module):
    import appcore
    with flask_app.test_request_context("/"):
        con = appcore.get_db()
        con.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")  # обектът на заявката е негоден след нея
    assert len(db_module._pool) == 1
    raw = db_module._pool[0][1]
    assert db_module.close_cached_connections() == 1
    assert db_module._pool == []
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")  # наистина затворена


def test_released_connection_holds_no_lock_and_no_transaction(flask_app, db_module):
    """Недочетена заявка държи споделен катинар (DELETE журнал) — при
    затваряне го пускаше close(); сега го пуска освобождаването. Отворена
    транзакция се връща (rollback), както при затваряне."""
    import appcore
    _to_delete_journal(db_module)
    _add_client(db_module, "Първи")
    _add_client(db_module, "Втори")
    kept = []
    with flask_app.test_request_context("/"):
        con = appcore.get_db()
        cur = con.execute("SELECT name FROM clients ORDER BY id")
        cur.fetchone()  # недочетен курсор, жив и след заявката
        kept.append(cur)
    with flask_app.test_request_context("/"):
        con = appcore.get_db()
        con.execute("INSERT INTO clients (name) VALUES ('Некомитнат')")
        assert con.in_transaction
    assert len(db_module._pool) == 1
    other = sqlite3.connect(db_module.DB_PATH, timeout=0)
    try:
        other.execute("BEGIN EXCLUSIVE")  # никой не държи катинар
        other.rollback()
        assert other.execute("SELECT COUNT(*) FROM clients WHERE name = 'Некомитнат'"
                             ).fetchone()[0] == 0
    finally:
        other.close()


def test_per_request_settings_are_reset(flask_app, db_module):
    import appcore
    with flask_app.test_request_context("/"):
        con = appcore.get_db()
        con.row_factory = None
        con.execute("PRAGMA busy_timeout = 5")
        con.execute("PRAGMA foreign_keys = OFF")
    with flask_app.test_request_context("/"):
        con = appcore.get_db()
        assert con.row_factory is sqlite3.Row
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("SELECT ci_lower('ИВАН') AS x").fetchone()["x"] == "иван"


@pytest.mark.parametrize("journal", ["wal", "delete"])
def test_change_from_another_process_is_seen_on_the_next_request(admin_client, db_module,
                                                                 opens, journal):
    """Две програми върху една база: другата записва, тази го вижда още на
    следващата заявка — SQLite сам изхвърля кеша (WAL — wal-index, DELETE —
    броячът на промените в заглавката)."""
    if journal == "delete":
        _to_delete_journal(db_module)
    con = sqlite3.connect(db_module.DB_PATH)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == journal
    con.close()
    assert "Друг Компютър ООД" not in admin_client.get("/clients").get_data(as_text=True)
    for step in range(3):
        name = "Друг Компютър ООД %d" % step
        script = textwrap.dedent("""
            import sqlite3, sys
            con = sqlite3.connect(sys.argv[1], timeout=10)
            con.execute("INSERT INTO clients (name, city) VALUES (?, 'Русе')", (sys.argv[2],))
            con.commit()
            con.close()
        """)
        subprocess.run([sys.executable, "-c", script, db_module.DB_PATH, name], check=True,
                       timeout=60)
        assert name in admin_client.get("/clients").get_data(as_text=True)
    assert len(opens) <= 1, opens  # същата връзка, не нова


@pytest.mark.skipif(os.name == "nt", reason="под Windows отворен файл не може да бъде подменен — "
                    "затова restore/rename първо викат db.close_cached_connections()")
def test_replaced_database_file_gets_a_fresh_connection(admin_client, db_module, opens, tmp_path):
    """Възстановяване/преименуване от друг компютър (файлът е сменен под
    същото име): старата връзка сочи стария файл — не се преизползва."""
    _add_client(db_module, "Стара База ЕООД")
    assert "Стара База ЕООД" in admin_client.get("/clients").get_data(as_text=True)
    staged = str(tmp_path / "restored.db")
    src = sqlite3.connect(db_module.DB_PATH)
    dst = sqlite3.connect(staged)
    src.backup(dst)
    src.close()
    dst.execute("UPDATE clients SET name = 'Нова База ЕООД' WHERE name = 'Стара База ЕООД'")
    dst.commit()
    dst.close()
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_module.DB_PATH + suffix):
            os.remove(db_module.DB_PATH + suffix)
    os.replace(staged, db_module.DB_PATH)
    body = admin_client.get("/clients").get_data(as_text=True)
    assert "Нова База ЕООД" in body and "Стара База ЕООД" not in body


def test_switching_the_database_path_gets_a_fresh_connection(admin_client, db_module, tmp_path,
                                                             monkeypatch):
    _add_client(db_module, "Първа Папка ООД")
    assert "Първа Папка ООД" in admin_client.get("/clients").get_data(as_text=True)
    other = str(tmp_path / "second" / "pacho.db")
    os.makedirs(os.path.dirname(other))
    src = sqlite3.connect(db_module.DB_PATH)
    con = sqlite3.connect(other)
    src.backup(con)  # копие с WAL съдържанието
    src.close()
    con.execute("UPDATE clients SET name = 'Втора Папка ООД' WHERE name = 'Първа Папка ООД'")
    con.commit()
    con.close()
    monkeypatch.setattr(db_module, "DB_PATH", other)
    db_module._journal_settled.add(other)
    body = admin_client.get("/clients").get_data(as_text=True)
    assert "Втора Папка ООД" in body and "Първа Папка ООД" not in body


class _BrokenRaw:
    """Преизползвана връзка, чийто файл вече е негоден (прекъснат мрежов диск)."""
    closed = False

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    def close(self):
        _BrokenRaw.closed = True


def test_broken_reused_connection_is_replaced_transparently(admin_client, db_module):
    assert admin_client.get("/clients").status_code == 200
    assert len(db_module._pool) == 1
    entry = db_module._pool[0]
    entry[1].close()
    _BrokenRaw.closed = False
    db_module._pool[0] = (entry[0], _BrokenRaw(), entry[2], entry[3])
    resp = admin_client.get("/clients")
    assert resp.status_code == 200
    assert _BrokenRaw.closed
    assert [type(e[1]) for e in db_module._pool] == [sqlite3.Connection]


def test_connection_with_an_io_error_is_not_returned_to_the_pool(flask_app, db_module):
    import appcore
    with flask_app.test_request_context("/"):
        con = appcore.get_db()
        con.execute("SELECT 1")
        with pytest.raises(sqlite3.OperationalError):
            con.execute("SELECT * FROM no_such_table_x")
    assert db_module._pool == []


def test_wal_database_marked_shared_is_not_kept_open(admin_client, db_module):
    """Друг компютър отваря базата през мрежата и чака да я превключи в
    DELETE (иска да няма други връзки) — тази програма спира да пази връзки."""
    assert admin_client.get("/").status_code == 200
    assert len(db_module._pool) == 1
    con = sqlite3.connect(db_module.DB_PATH)
    con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, '1')",
                (db_module.SHARED_DB_KEY,))
    con.commit()
    con.close()
    assert admin_client.get("/").status_code == 200
    assert db_module._pool == []
    con = sqlite3.connect(db_module.DB_PATH, timeout=0)
    try:
        assert con.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    finally:
        con.close()


def test_idle_connections_are_closed_after_the_timeout(admin_client, db_module, monkeypatch):
    assert admin_client.get("/").status_code == 200
    assert len(db_module._pool) == 1
    raw = db_module._pool[0][1]
    monkeypatch.setattr(db_module, "POOL_IDLE_SECONDS", 0.0)
    db_module._reap_idle_connections()
    assert db_module._pool == []
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_restore_closes_the_cached_connections(admin_client, db_module, tmp_path):
    import backup
    folder = str(tmp_path / "bk")
    os.makedirs(folder)
    bpath = backup.local_backup(folder)
    assert admin_client.get("/").status_code == 200
    raw = db_module._pool[0][1]
    backup.request_restore(folder, os.path.basename(bpath))
    assert backup.apply_pending_restore()["ok"]
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_shared_rename_closes_the_cached_connections(admin_client, db_module, monkeypatch):
    import config as appconfig
    import legacy_migration
    folder = os.path.dirname(db_module.DB_PATH)
    old = os.path.join(folder, legacy_migration.LEGACY_DB_NAME)
    db_module.close_cached_connections()
    os.replace(db_module.DB_PATH, old)
    monkeypatch.setattr(db_module, "DB_PATH", old)
    appconfig.save_config({"db_path": old})
    monkeypatch.setattr(db_module, "SHARED_RENAME_MIN_VERSION", "3.0.0")
    assert db_module.record_instance(force=True)
    db_module.get_db().close()  # режимът на журнала — уреден за новия път
    assert admin_client.get("/").status_code == 200
    assert len(db_module._pool) == 1
    raw = db_module._pool[0][1]
    new = db_module.rename_shared_database()
    assert new != old and db_module.DB_PATH == new
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


# ---------------------------------------------------------------- PERF2-12 PDF на голяма фактура

def _big_invoice(rows=80):
    from routes_documents import _export_fields_and_items, _export_totals_row
    data = {"consignee_name": "Бразилия Ltda — получател", "consignee_address": "Rua São Paulo 1",
            "doc_date": "2026-10-15", "terms_payment": "Net in 90 Days",
            "items": [{"hs_code": "85389099", "po_no": "45000%05d" % i, "pos": str(10 * i),
                       "net_weight": "18.813", "material_code": "1VL1%05d" % i,
                       "description": "Шина мед, калайдисана — ред %d" % i, "qty": str(i % 30 + 1),
                       "unit_price": "371.09"} for i in range(1, rows + 1)]}
    fields, items, cols = _export_fields_and_items("invoice_br", data)
    return fields, items, cols, _export_totals_row("invoice_br", data, items, cols)


def _pdf_ops(pdf_bytes):
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    reader = PdfReader(io.BytesIO(pdf_bytes))

    def norm(x):
        if type(x).__name__ in ("FloatObject", "float"):
            return round(float(x), 2)
        if isinstance(x, (list, tuple)):
            return tuple(norm(i) for i in x)
        return x if isinstance(x, bytes) else str(x)
    return [[(norm(o), op) for o, op in ContentStream(p.get_contents(), reader).operations]
            for p in reader.pages]


def _template_source():
    from conftest import ROOT
    return open(os.path.join(ROOT, "templates", "pdf_export.html"), encoding="utf-8").read()


def test_pdf_template_has_no_per_cell_width_and_no_descendant_selectors():
    """xhtml2pdf проверява всяко CSS правило за всяка клетка (потомствените
    селектори — и с родителите ѝ) и разбира inline стила на всяка клетка:
    при 80 реда × 8 колони това беше ~15 % от времето."""
    src = _template_source()
    style = re.sub(r"\{#.*?#\}", "", src[src.index("<style>"):src.index("</style>")], flags=re.S)
    assert ".items-table td" not in style and ".fields-table td" not in style
    assert "tr.totals td" not in style
    body = src[src.index("{% for row in rows %}"):src.index("{% endfor %}</tr>")]
    assert "style=" not in body, body
    # Ширината остава на заглавния ред (и на реда TOTAL).
    assert src.count('style="width:{{ width }}%"') == 2


def test_big_invoice_layout_is_the_same_as_with_a_width_on_every_cell(flask_app):
    """Същият PDF (всяка линия, цвят и текст на място) като с изрична ширина
    на всяка клетка — колоните не се променят. Бразилия — без „Total weight“."""
    import pdf_export
    from pypdf import PdfReader
    src = _template_source()
    old = src.replace('<td class="{{ col_classes[loop.index0] }}">{{ lines(row[loop.index0]) }}</td>',
                      '<td class="{{ col_classes[loop.index0] }}" style="width:{{ width }}%">'
                      '{{ lines(row[loop.index0]) }}</td>')
    assert old != src
    fields, items, cols, totals = _big_invoice()
    with flask_app.test_request_context():
        new_pdf = pdf_export.generate_document_pdf("Фактура за Бразилия", "0000099001",
                                                   "INVBR-15102026-0001", fields, items, cols,
                                                   totals_row=totals)
        tmpl = flask_app.jinja_env.from_string(old)
        real = pdf_export.render_template
        pdf_export.render_template = lambda name, **ctx: tmpl.render(**ctx)
        try:
            old_pdf = pdf_export.generate_document_pdf("Фактура за Бразилия", "0000099001",
                                                       "INVBR-15102026-0001", fields, items, cols,
                                                       totals_row=totals)
        finally:
            pdf_export.render_template = real
    assert _pdf_ops(new_pdf) == _pdf_ops(old_pdf)
    reader = PdfReader(io.BytesIO(new_pdf))
    text = "\n".join(p.extract_text() for p in reader.pages)
    for i in (1, 40, 80):
        assert "1VL1%05d" % i in text and "45000%05d" % i in text
    assert "Total weight" not in text and "Общо тегло" not in text
    fonts = set()
    for page in reader.pages:
        for f in page["/Resources"]["/Font"].values():
            fonts.add(str(f.get_object().get("/BaseFont")))
    assert any("DejaVuSans" in f for f in fonts), fonts


def _footer_spans(pdf_bytes):
    import pdf_export
    from pypdf import PdfReader
    out = []
    for page in PdfReader(io.BytesIO(pdf_bytes)).pages:
        spans = []

        def visit(text, cm, tm, _f, _s):
            if text.strip():
                spans.append((tm[4] * cm[0] + tm[5] * cm[2] + cm[4],
                              tm[4] * cm[1] + tm[5] * cm[3] + cm[5], text))
        page.extract_text(visitor_text=visit)
        out.append([s for s in spans if s[1] < pdf_export._FOOTER_TOP_PT + 2])
    return out


def test_page_total_position_is_found_once_and_reused(flask_app, monkeypatch):
    """Разборът на целия първи лист за края на номера в колонтитула (~50 ms
    при голяма фактура) — веднъж на процес: текстът е подравнен вдясно."""
    import pdf_export
    fields, items, cols, totals = _big_invoice(60)
    monkeypatch.setattr(pdf_export, "_footer_anchor_cache", {})
    with flask_app.test_request_context():
        first = pdf_export.generate_document_pdf("Фактура за Бразилия", "0000099002", "",
                                                 fields, items, cols, totals_row=totals)
        assert len(pdf_export._footer_anchor_cache) == 1
        anchor = list(pdf_export._footer_anchor_cache.values())[0]

        def must_not_search(page):
            raise AssertionError("позицията вече е известна")
        monkeypatch.setattr(pdf_export, "_footer_label_end", must_not_search)
        second = pdf_export.generate_document_pdf("Опаковъчен лист", "7", "", fields[:3],
                                                  items[:5], cols)
    for pdf_bytes in (first, second):
        pages = _footer_spans(pdf_bytes)
        for spans in pages:
            total = [s for s in spans if s[2].strip() == "/ %d" % len(pages)]
            assert total, spans
            assert abs(total[0][0] - anchor[0]) < 0.05 and abs(total[0][1] - anchor[1]) < 0.05


def test_long_footer_is_still_searched_on_the_page(flask_app, monkeypatch):
    """Колонтитул на два реда — номерът е по-ниско: позицията не се взима от кеша."""
    import pdf_export
    monkeypatch.setattr(pdf_export, "_footer_anchor_cache", {(595.28, 841.89): (1.0, 1.0)})
    calls = []
    real = pdf_export._footer_label_end
    monkeypatch.setattr(pdf_export, "_footer_label_end",
                        lambda page: calls.append(1) or real(page))
    with flask_app.test_request_context():
        pdf_export.generate_document_pdf("Фактура " * 40, "1", "", [("Поле", "стойност")], [], [])
    assert calls
