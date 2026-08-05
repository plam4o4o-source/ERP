# -*- coding: utf-8 -*-
"""Характеризиращ тестов пакет за Фаза 3 (структурен рефакторинг): пуска
ЦЯЛОТО Flask приложение (appcore.create_app() + всички routes_* модули,
точно както app.py ги регистрира — виж fixture flask_app в conftest.py) и
опитва реални HTTP заявки през Flask test client.

Целта е РЕГРЕСИОНЕН предпазен колан за разделянето на стария монолитен
app.py по routes_*.py модули: ако вход, издаване на документ, права на
достъп (admin_required/login_required) или CSRF защитата се счупят при
пренасянето на кода, тестовете тук трябва да го хванат — преди да е стигнало
до реален служител на терен."""
import json

from conftest import get_csrf_token, post_with_csrf


# ---------------------------------------------------------------- вход/изход и CSRF

def test_login_page_renders_with_csrf_field(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


def test_login_wrong_password_shows_error(client):
    token = get_csrf_token(client, "/login")
    resp = client.post("/login", data={"username": "nobody", "password": "wrong",
                                       "csrf_token": token})
    assert resp.status_code == 200
    assert "Грешно потребителско име".encode() in resp.data


def test_login_success_redirects_to_dashboard(admin_client):
    resp = admin_client.get("/", follow_redirects=False)
    assert resp.status_code == 200  # вече логнат — таблото се зарежда директно


def test_dashboard_requires_login_redirects_to_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_logout_clears_session(admin_client):
    resp = admin_client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    resp2 = admin_client.get("/", follow_redirects=False)
    assert resp2.status_code == 302
    assert "/login" in resp2.headers["Location"]


def test_post_without_csrf_token_is_rejected(admin_client):
    resp = admin_client.post("/clients/new", data={"name": "Тест ЕООД"})
    assert resp.status_code == 400


def test_post_with_wrong_csrf_token_is_rejected(admin_client):
    resp = admin_client.post("/clients/new",
                             data={"name": "Тест ЕООД", "csrf_token": "invalid-token"})
    assert resp.status_code == 400


def test_must_change_password_redirects_to_change_password(flask_app, db_module):
    from werkzeug.security import generate_password_hash
    con = db_module.get_db()
    con.execute(
        "INSERT INTO users (username, password_hash, full_name, role, active,"
        " must_change_password) VALUES (?, ?, ?, 'employee', 1, 1)",
        ("must_change", generate_password_hash("test-password-123"), "Трябва Смяна"),
    )
    con.commit()
    con.close()
    c = flask_app.test_client()
    token = get_csrf_token(c, "/login")
    c.post("/login", data={"username": "must_change", "password": "test-password-123",
                           "csrf_token": token})
    resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/password" in resp.headers["Location"]
    # change_password самата страница трябва да е достъпна (изключена от enforce)
    resp2 = c.get("/password")
    assert resp2.status_code == 200


# ---------------------------------------------------------------- документни потоци (5 типа)

def test_cmr_new_get_renders_form(admin_client):
    resp = admin_client.get("/cmr/new")
    assert resp.status_code == 200


def test_cmr_new_post_creates_document(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач ЕООД", "consignee_name": "Получател ООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302
    assert "/doc/" in resp.headers["Location"]


def test_packing_new_post_with_items_creates_document(admin_client):
    items = json.dumps([{"description": "Стока А", "qty": "2"}])
    resp = post_with_csrf(admin_client, "/packing/new", {
        "sender_name": "Изпращач", "items_json": items,
    }, csrf_source_url="/packing/new", follow_redirects=False)
    assert resp.status_code == 302


def test_pallet_new_post_with_items_creates_document(admin_client):
    items = json.dumps([{"code": "ART-1", "description": "Кашон", "qty": "3"}])
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "pallet_no": "1", "items_json": items,
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    assert resp.status_code == 302


def test_dualuse_new_post_creates_document(admin_client):
    resp = post_with_csrf(admin_client, "/dualuse/new", {
        "sender_name": "Износител ЕООД",
    }, csrf_source_url="/dualuse/new", follow_redirects=False)
    assert resp.status_code == 302


def test_export_it_new_post_creates_document(admin_client):
    resp = post_with_csrf(admin_client, "/export-it/new", {
        "declarant_name": "Декларатор",
    }, csrf_source_url="/export-it/new", follow_redirects=False)
    assert resp.status_code == 302


def test_cmr_preview_flow_redirects_to_preview_token(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/preview", {
        "sender_name": "Преглед ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302
    assert "/preview/" in resp.headers["Location"]
    resp2 = admin_client.get(resp.headers["Location"])
    assert resp2.status_code == 200
    assert "DRAFT".encode() in resp2.data or "ПРЕДВАРИТЕЛЕН".encode() in resp2.data


def test_view_document_after_creation(admin_client):
    create_resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач за преглед",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_url = create_resp.headers["Location"]
    resp = admin_client.get(doc_url)
    assert resp.status_code == 200


def test_edit_document_updates_data(admin_client):
    create_resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Оригинален изпращач",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_url = create_resp.headers["Location"]
    doc_id = doc_url.rstrip("/").split("/")[-1]
    edit_url = "/doc/%s/edit" % doc_id
    resp = post_with_csrf(admin_client, edit_url, {
        "sender_name": "Редактиран изпращач",
    }, csrf_source_url=edit_url, follow_redirects=False)
    assert resp.status_code == 302
    view = admin_client.get(doc_url)
    assert "Редактиран изпращач".encode() in view.data


def test_export_document_xlsx_returns_spreadsheet(admin_client):
    create_resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач за износ",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = create_resp.headers["Location"].rstrip("/").split("/")[-1]
    resp = admin_client.get("/doc/%s/export.xlsx" % doc_id)
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["Content-Type"]


def test_delete_document_requires_admin(employee_client, admin_client):
    create_resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "За изтриване",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = create_resp.headers["Location"].rstrip("/").split("/")[-1]

    del_url = "/doc/%s/delete" % doc_id
    resp = post_with_csrf(employee_client, del_url, {}, csrf_source_url="/",
                          follow_redirects=False)
    assert resp.status_code == 403  # служител без admin права — забранено

    resp2 = post_with_csrf(admin_client, del_url, {}, csrf_source_url="/",
                           follow_redirects=False)
    assert resp2.status_code == 302  # админ може


# ---------------------------------------------------------------- палетна карта: bulk/pull

def test_packing_pull_pallet_not_found_returns_error_json(admin_client):
    resp = post_with_csrf(admin_client, "/packing/pull-pallet",
                          {"code": "NE-SASHTESTVUVASHT"}, csrf_source_url="/")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is False


def test_packing_pull_pallet_finds_created_pallet(admin_client):
    items = json.dumps([{"code": "ART-1", "description": "Кашон А", "qty": "5"}])
    create_resp = post_with_csrf(admin_client, "/pallet/new", {
        "pallet_no": "77", "boxes": "5", "items_json": items,
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    doc_id = create_resp.headers["Location"].rstrip("/").split("/")[-1]
    # взимаме баркода директно от базата, за да не парсваме HTML
    import db as db_mod
    con = db_mod.get_db()
    row = con.execute("SELECT barcode, number FROM documents WHERE id = ?", (doc_id,)).fetchone()
    con.close()

    resp = post_with_csrf(admin_client, "/packing/pull-pallet",
                          {"code": row["barcode"]}, csrf_source_url="/")
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["number"] == row["number"]


# ---------------------------------------------------------------- клиенти (адресна книга)

def test_clients_crud_flow(admin_client):
    resp = post_with_csrf(admin_client, "/clients/new", {
        "name": "Тестов Клиент ЕООД", "city": "София",
    }, csrf_source_url="/clients/new", follow_redirects=False)
    assert resp.status_code == 302

    list_resp = admin_client.get("/clients")
    assert "Тестов Клиент ЕООД".encode() in list_resp.data

    import db as db_mod
    con = db_mod.get_db()
    client_row = con.execute(
        "SELECT id FROM clients WHERE name = ?", ("Тестов Клиент ЕООД",)
    ).fetchone()
    con.close()
    client_id = client_row["id"]

    edit_url = "/clients/%d/edit" % client_id
    edit_resp = admin_client.get(edit_url)
    assert edit_resp.status_code == 200

    upd_resp = post_with_csrf(admin_client, edit_url, {
        "name": "Тестов Клиент ЕООД", "city": "Пловдив",
    }, csrf_source_url=edit_url, follow_redirects=False)
    assert upd_resp.status_code == 302

    del_url = "/clients/%d/delete" % client_id
    del_resp = post_with_csrf(admin_client, del_url, {}, csrf_source_url="/clients",
                              follow_redirects=False)
    assert del_resp.status_code == 302
    list_resp2 = admin_client.get("/clients")
    assert "Тестов Клиент ЕООД".encode() not in list_resp2.data


# ---------------------------------------------------------------- настройки

def test_settings_page_get_and_post(admin_client):
    resp = admin_client.get("/settings")
    assert resp.status_code == 200
    upd = post_with_csrf(admin_client, "/settings", {
        "sender_name": "Моята Фирма ЕООД",
    }, csrf_source_url="/settings", follow_redirects=False)
    assert upd.status_code == 302
    resp2 = admin_client.get("/settings")
    assert "Моята Фирма ЕООД".encode() in resp2.data


def test_my_settings_theme_change(admin_client):
    resp = admin_client.get("/my-settings")
    assert resp.status_code == 200
    upd = post_with_csrf(admin_client, "/my-settings", {"theme": "dark"},
                         csrf_source_url="/my-settings", follow_redirects=False)
    assert upd.status_code == 302


def test_my_settings_shows_admin_system_section_only_for_admin(admin_client, employee_client):
    admin_resp = admin_client.get("/my-settings")
    assert "gh_owner".encode() in admin_resp.data or "GitHub".encode() in admin_resp.data
    emp_resp = employee_client.get("/my-settings")
    assert emp_resp.status_code == 200


# ---------------------------------------------------------------- админ панел (потребители)

def test_admin_routes_forbidden_for_employee(employee_client):
    assert employee_client.get("/admin/users").status_code == 403
    assert employee_client.get("/admin/system").status_code == 403


def test_admin_user_new_toggle_password_delete_flow(admin_client):
    resp = post_with_csrf(admin_client, "/admin/users/new", {
        "username": "novak", "full_name": "Нов Служител",
        "password": "parola12345", "role": "employee",
    }, csrf_source_url="/admin/users", follow_redirects=False)
    assert resp.status_code == 302

    import db as db_mod
    con = db_mod.get_db()
    row = con.execute("SELECT id, active, must_change_password FROM users WHERE username = ?",
                      ("novak",)).fetchone()
    con.close()
    assert row["active"] == 1
    assert row["must_change_password"] == 1
    user_id = row["id"]

    toggle_url = "/admin/users/%d/toggle" % user_id
    t_resp = post_with_csrf(admin_client, toggle_url, {}, csrf_source_url="/admin/users",
                            follow_redirects=False)
    assert t_resp.status_code == 302

    pwd_url = "/admin/users/%d/password" % user_id
    p_resp = post_with_csrf(admin_client, pwd_url, {"password": "novaparola123"},
                            csrf_source_url="/admin/users", follow_redirects=False)
    assert p_resp.status_code == 302

    del_url = "/admin/users/%d/delete" % user_id
    d_resp = post_with_csrf(admin_client, del_url, {}, csrf_source_url="/admin/users",
                            follow_redirects=False)
    assert d_resp.status_code == 302


def test_admin_cannot_delete_own_account(admin_client):
    with admin_client.session_transaction() as sess:
        own_id = sess["user_id"]
    del_url = "/admin/users/%d/delete" % own_id
    resp = post_with_csrf(admin_client, del_url, {}, csrf_source_url="/admin/users",
                          follow_redirects=True)
    assert "Не можете да изтриете".encode() in resp.data
