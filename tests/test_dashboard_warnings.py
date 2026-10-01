# -*- coding: utf-8 -*-
"""Предупреждения на таблото: неуспешен архив и изоставащ часовник (O4/O7
от анализа на 01.10.2026); година без „y.“ на английски."""


def test_dashboard_warns_admin_when_the_last_backup_failed(admin_client, db_module):
    import backup
    con = db_module.get_db()
    try:
        db_module.save_settings(con, {"backup_folder": "/tmp/x", backup.LAST_RESULT_KEY: "error",
                                      backup.LAST_ERROR_KEY: "диск недостъпен",
                                      backup.LAST_ERROR_AT_KEY: "2026-10-01 10:00:00"})
        con.commit()
    finally:
        con.close()
    body = admin_client.get("/").get_data(as_text=True)
    assert "Последното архивиране е неуспешно" in body and "диск недостъпен" in body


def test_dashboard_warns_when_no_backup_folder_is_set(admin_client):
    assert "Не е зададена папка за архив" in admin_client.get("/").get_data(as_text=True)


def test_employee_does_not_see_backup_warnings(employee_client):
    assert "Не е зададена папка за архив" not in employee_client.get("/").get_data(as_text=True)


def test_dashboard_warns_when_this_pc_clock_is_behind(admin_client, db_module):
    con = db_module.get_db()
    try:
        con.execute("INSERT INTO documents (doc_type, number, year, seq, barcode, public_token, data,"
                    " created_by, created_at) VALUES ('cmr', '0001/2099', 2099, 1, 'B1', 't1', '{}', 1,"
                    " '2099-01-01 10:00:00')")
        con.commit()
    finally:
        con.close()
    assert "Часовникът на този компютър изостава" in admin_client.get("/").get_data(as_text=True)


def test_english_dashboard_has_no_y_dot_after_the_year(admin_client):
    admin_client.get("/?lang=en")
    with admin_client.session_transaction() as sess:
        sess["lang"] = "en"
    body = admin_client.get("/").get_data(as_text=True)
    assert " y.<" not in body and " y.</" not in body


def test_delete_confirmation_names_the_document_type(admin_client, db_module):
    """U12: „Да изтрия ли документ № 0001/2026?“ — при еднакви номери във всеки
    тип операторът не знае кой изтрива."""
    from conftest import post_with_csrf
    import json
    post_with_csrf(admin_client, "/cmr/new", {"consignee_name": "R"})
    body = admin_client.get("/docs").get_data(as_text=True)
    assert "Да изтрия ли ЧМР товарителница № 0001/" in body
    post_with_csrf(admin_client, "/invoice-br/new", {
        "consignee_name": "R", "invoice_number": "77",
        "items_json": json.dumps([{"description": "x", "qty": "1", "unit_price": "1"}])},
        csrf_source_url="/invoice-br/new")
    body = admin_client.get("/invoices").get_data(as_text=True)
    assert "№ 77? Действието е необратимо." in body and "Да изтрия ли фактура №" not in body
