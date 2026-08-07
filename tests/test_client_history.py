# -*- coding: utf-8 -*-
"""Регресионни тестове за „История на документите от картата на клиента“
(routes_clients.client_edit, _client_recent_documents) — заявка: „направи
всичко което предлагаш“ (списък с предложения за подобрения)."""
from conftest import post_with_csrf


def _add_client(client, name, **extra):
    data = {"name": name}
    data.update(extra)
    resp = post_with_csrf(client, "/clients/new", data, csrf_source_url="/clients/new",
                          follow_redirects=False)
    assert resp.status_code == 302, resp.data
    # Намираме новосъздадения клиент по име (client_edit няма отделен ID в Location-а).
    con_page = client.get("/clients")
    return con_page


def _client_id_by_name(admin_client, name):
    from appcore import get_db
    import app  # осигурява app контекста в теста
    with app.app.app_context():
        con = get_db()
        row = con.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()
        return row["id"] if row else None


def test_client_card_shows_no_documents_message_when_none(admin_client):
    _add_client(admin_client, "Клиент Без Документи ЕООД")
    client_id = _client_id_by_name(admin_client, "Клиент Без Документи ЕООД")
    resp = admin_client.get("/clients/%s/edit" % client_id)
    assert resp.status_code == 200
    assert "Все още няма издадени документи" in resp.data.decode()


def test_client_card_lists_matching_documents(admin_client):
    _add_client(admin_client, "Клиент С Документи ЕООД")
    client_id = _client_id_by_name(admin_client, "Клиент С Документи ЕООД")

    post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Клиент С Документи ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)

    resp = admin_client.get("/clients/%s/edit" % client_id)
    body = resp.data.decode()
    assert resp.status_code == 200
    assert "Все още няма издадени документи" not in body
    assert "Виж всички документи на този клиент" in body


def test_client_card_does_not_show_other_clients_documents(admin_client):
    _add_client(admin_client, "Клиент А ЕООД")
    _add_client(admin_client, "Клиент Б ЕООД")
    client_a_id = _client_id_by_name(admin_client, "Клиент А ЕООД")

    post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Клиент Б ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)

    resp = admin_client.get("/clients/%s/edit" % client_a_id)
    assert "Все още няма издадени документи" in resp.data.decode()


def test_client_card_new_client_form_has_no_history_section(admin_client):
    resp = admin_client.get("/clients/new")
    assert "Последни документи на този клиент" not in resp.data.decode()


def test_client_card_exact_name_match_not_substring(admin_client):
    """Клиент „Алфа“ не трябва да вижда документите на „Алфа Дистрибуция“ —
    resolve_client_name прави ТОЧНО сравнение на цялото име, не substring."""
    _add_client(admin_client, "Алфа")
    alfa_id = _client_id_by_name(admin_client, "Алфа")

    post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Алфа Дистрибуция ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)

    resp = admin_client.get("/clients/%s/edit" % alfa_id)
    assert "Все още няма издадени документи" in resp.data.decode()


def test_client_card_renders_document_type_title_and_number(admin_client):
    """Тестовете на самия патч проверяват само че разделът НЕ е празен и че
    линкът „Виж всички“ е там — но не и че таблицата реално показва
    човешкото заглавие на типа (през db.DOC_TYPES) и номера на документа.
    Точно там би минала незабелязано грешка в шаблона (напр. суровият код
    „cmr“ вместо „ЧМР товарителница“, или липсващ номер)."""
    _add_client(admin_client, "Клиент За Заглавия ЕООД")
    client_id = _client_id_by_name(admin_client, "Клиент За Заглавия ЕООД")

    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Клиент За Заглавия ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    body = admin_client.get("/clients/%s/edit" % client_id).data.decode()
    assert "ЧМР товарителница" in body       # заглавието на типа, не суровото "cmr"
    assert "/doc/%s" % doc_id in body        # линкът „Отвори“ сочи към документа
    import re
    assert re.search(r"\d{4}/\d{4}", body)   # номерът на документа (напр. 0001/2026)
