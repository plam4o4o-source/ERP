# -*- coding: utf-8 -*-
"""Одит (04.10.2026, група EXPORT): издаване на документи.

R5 — задължителните полета се проверяват и на сървъра (празен POST вече не
     издава номериран документ без получател);
UX-Б4 — декларацията за двойна употреба изисква държава и декларатор;
     деклараторът се допълва от „Лице за контакт“ в Настройки;
F2 — предложеният номер на фактура е по ВСИЧКИ типове фактури, а номер,
     зает от фактура от друг тип, спира издаването до изрично потвърждение;
F6 — „Издай“ от предварителния преглед е атомарно (5 едновременни → 1);
R7 — при пълен диск съобщението казва истината.
"""
import json
import re
import sqlite3
import threading
from datetime import date

import pytest

from conftest import post_with_csrf

YEAR = date.today().year
ITEM = {"material_code": "MAT-1", "qty": "2", "unit_price": "1.5"}


def _count(db_module):
    con = db_module.get_db()
    try:
        return con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        con.close()


def _post(client, url, fields, follow=False):
    return post_with_csrf(client, url, fields, csrf_source_url=url, follow_redirects=follow)


def _edit_data(body):
    import html
    m = re.search(r"data-edit='([^']*)'", body)
    assert m, "формата не е предварително попълнена"
    return json.loads(html.unescape(m.group(1)))


# ---------------------------------------------------------------- R5

@pytest.mark.parametrize("url", ["/cmr/new", "/packing/new", "/pallet/new", "/waybill/new",
                                 "/dualuse/new", "/export-it/new"])
def test_r5_empty_post_does_not_issue_a_document(admin_client, db_module, url):
    resp = _post(admin_client, url, {"notes": "запазено", "sender_name": "Изпращач"})
    assert resp.status_code == 302
    assert "%s?restore=" % url in resp.headers["Location"]
    assert _count(db_module) == 0, "издаден е номериран документ без задължителните полета"
    body = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "задължителните полета" in body
    assert _edit_data(body)["sender_name"] == "Изпращач", "въведеното не е запазено"


def test_r5_whitespace_only_recipient_is_rejected(admin_client, db_module):
    _post(admin_client, "/cmr/new", {"consignee_name": "   "})
    assert _count(db_module) == 0


def test_r5_issue_from_preview_is_validated_too(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/cmr/preview", {"sender_name": "Изпращач"},
                          csrf_source_url="/cmr/new", follow_redirects=True)
    token = re.search(r"/preview/([\w-]+)/issue", resp.get_data(as_text=True)).group(1)
    resp = post_with_csrf(admin_client, "/preview/%s/issue" % token, {},
                          csrf_source_url="/cmr/new")
    assert "/cmr/new?restore=" in resp.headers["Location"]
    assert _count(db_module) == 0


def test_r5_complete_form_still_issues(admin_client, db_module):
    resp = _post(admin_client, "/cmr/new", {"consignee_name": "Клиент"})
    assert re.search(r"/doc/\d+$", resp.headers["Location"])
    assert _count(db_module) == 1


# ---------------------------------------------------------------- UX-Б4

def test_dualuse_requires_country_and_declarant(admin_client, db_module):
    resp = _post(admin_client, "/dualuse/new", {"invoice_numbers": "0000001234"}, follow=True)
    body = resp.get_data(as_text=True)
    assert _count(db_module) == 0
    assert "Държава на износ" in body and "Декларатор" in body


def test_dualuse_declarant_defaults_to_settings_contact_person(admin_client, db_module):
    con = db_module.get_db()
    db_module.save_settings(con, {"sender_person": "Иван Петров"})
    con.commit()
    con.close()
    resp = _post(admin_client, "/dualuse/new", {"invoice_numbers": "0000001234",
                                                "destination_country": "Турция"})
    assert re.search(r"/doc/\d+$", resp.headers["Location"]), resp.headers["Location"]
    con = db_module.get_db()
    data = json.loads(con.execute("SELECT data FROM documents").fetchone()["data"])
    con.close()
    assert data["declarant_name"] == "Иван Петров"


# ---------------------------------------------------------------- F2

def _invoice(client, url, number, follow=False, **extra):
    fields = {"consignee_name": "ABB", "invoice_number": number,
              "items_json": json.dumps([ITEM])}
    fields.update(extra)
    return _post(client, url, fields, follow=follow)


def test_f2_suggestion_is_the_next_number_across_all_invoice_types(admin_client):
    _invoice(admin_client, "/invoice-br/new", "0000012950")
    _invoice(admin_client, "/invoice-dubai/new", "0000012957")
    for url in ("/invoice-br/new", "/invoice-no/new", "/invoice-dubai/new"):
        body = admin_client.get(url).get_data(as_text=True)
        assert 'value="0000012958"' in body, url


def test_f2_number_of_another_invoice_type_blocks_until_confirmed(admin_client, db_module):
    _invoice(admin_client, "/invoice-dubai/new", "0000012957")
    resp = _invoice(admin_client, "/invoice-br/new", "0000012957")
    assert "/invoice-br/new?restore=" in resp.headers["Location"]
    assert _count(db_module) == 1, "издадено без потвърждение"
    body = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "0000012957 вече е използван" in body and "Фактура за Дубай" in body
    assert _edit_data(body)["invoice_number"] == "0000012957"
    # Второто „Издай“ със същия номер е изричното потвърждение.
    resp = _invoice(admin_client, "/invoice-br/new", "0000012957")
    assert re.search(r"/doc/\d+$", resp.headers["Location"])
    assert _count(db_module) == 2
    # Потвърждението важи веднъж — за следващата фактура пак се пита.
    resp = _invoice(admin_client, "/invoice-no/new", "0000012957")
    assert "restore=" in resp.headers["Location"]
    assert _count(db_module) == 2


def test_f2_explicit_confirmation_field_and_it_is_not_stored(admin_client, db_module):
    _invoice(admin_client, "/invoice-dubai/new", "X-1")
    resp = _invoice(admin_client, "/invoice-no/new", "X-1", confirm_number_reuse="X-1")
    assert re.search(r"/doc/\d+$", resp.headers["Location"])
    con = db_module.get_db()
    data = json.loads(con.execute(
        "SELECT data FROM documents WHERE doc_type = 'invoice_no'").fetchone()["data"])
    con.close()
    assert "confirm_number_reuse" not in data


def test_f2_editing_to_another_types_number_is_blocked_too(admin_client, db_module):
    _invoice(admin_client, "/invoice-dubai/new", "E-7")
    resp = _invoice(admin_client, "/invoice-br/new", "E-8")
    doc_id = int(resp.headers["Location"].rsplit("/", 1)[-1])
    resp = post_with_csrf(admin_client, "/doc/%d/edit" % doc_id, {
        "consignee_name": "ABB", "invoice_number": "E-7", "items_json": json.dumps([ITEM])})
    assert "restore=" in resp.headers["Location"]
    con = db_module.get_db()
    assert con.execute("SELECT number FROM documents WHERE id = ?", (doc_id,)).fetchone()[0] == "E-8"
    con.close()


# ---------------------------------------------------------------- F6

@pytest.fixture
def threaded_server(flask_app, db_module):
    """Многонишков сървър (като waitress в програмата) — live_server от
    conftest е еднонишков и подрежда заявките една след друга, тоест там
    състезанието изобщо не може да се случи."""
    from werkzeug.security import generate_password_hash
    from werkzeug.serving import make_server

    con = db_module.get_db()
    con.execute("INSERT INTO users (username, password_hash, full_name, role, active,"
                " must_change_password) VALUES ('f6', ?, 'F6', 'admin', 1, 0)",
                (generate_password_hash("f6-password-123"),))
    con.commit()
    con.close()
    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_f6_concurrent_issue_from_one_preview_creates_one_document(threaded_server, db_module,
                                                                   monkeypatch):
    requests = pytest.importorskip("requests")
    import time

    import routes_documents
    base = threaded_server
    s = requests.Session()
    page = s.get(base + "/login").text
    token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page).group(1)
    s.post(base + "/login", data={"username": "f6", "password": "f6-password-123",
                                  "csrf_token": token})
    form = s.get(base + "/cmr/new").text
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', form).group(1)
    preview = s.post(base + "/cmr/preview", data={"csrf_token": csrf,
                                                  "consignee_name": "Едновременно"}).text
    issue_url = base + re.search(r"(/preview/[\w-]+/issue)", preview).group(1)
    # Записът отнема време (мрежов диск, антивирус) — точно прозорецът, в
    # който пет едновременни заявки минаваха проверката „вече издаден ли е“.
    real_save = routes_documents.save_document

    def slow_save(*args, **kwargs):
        time.sleep(0.3)
        return real_save(*args, **kwargs)

    monkeypatch.setattr(routes_documents, "save_document", slow_save)
    cookies = s.cookies.get_dict()
    barrier = threading.Barrier(5)
    results = []

    def worker():
        sess = requests.Session()
        sess.cookies.update(cookies)
        barrier.wait()
        r = sess.post(issue_url, data={"csrf_token": csrf}, allow_redirects=False)
        results.append(r.headers.get("Location", ""))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert _count(db_module) == 1, "от един преглед са издадени %d документа" % _count(db_module)
    targets = {m.group(1) for m in (re.search(r"/doc/(\d+)", loc) for loc in results) if m}
    assert len(results) == 5 and len(targets) == 1, results


# ---------------------------------------------------------------- R7

def test_r7_disk_full_message_does_not_say_wait_a_few_seconds(admin_client, db_module, monkeypatch):
    import routes_documents

    def full(*_a, **_kw):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(routes_documents, "save_document", full)
    resp = _post(admin_client, "/cmr/new", {"consignee_name": "Клиент"}, follow=True)
    body = resp.get_data(as_text=True)
    assert "дискът с базата данни е пълен" in body
    assert "след няколко секунди" not in body
    assert _edit_data(body)["consignee_name"] == "Клиент"


def test_r7_other_errors_keep_the_retry_message(admin_client, monkeypatch):
    import routes_documents

    def busy(*_a, **_kw):
        raise RuntimeError("базата е заета")

    monkeypatch.setattr(routes_documents, "save_document", busy)
    body = _post(admin_client, "/cmr/new", {"consignee_name": "Клиент"},
                 follow=True).get_data(as_text=True)
    assert "след няколко секунди" in body
