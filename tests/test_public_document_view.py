# -*- coding: utf-8 -*-
"""Тестове за публичния преглед на документ БЕЗ вход, през QR код на
бланката — заявка: „всеки, който сканира с телефон баркода на някой от
документите, да му се зареди директно документа, без да има нужда от
домейна, който е в програмата“ + уточнение „само документа, нищо друго да
не вижда“.

Ключови разлики от обичайния /doc/<id>:
  - /p/<public_token> е БЕЗ @login_required — достъпен без вход.
  - token е случаен (128-битов), НЕ предвидимият barcode
    (ТИП-ДДММГГГГ-####) — целта е да НЕ може да се отгатне/изброи адресът
    на чужд документ (виж db._m002_public_token).
  - Само за вече баркодираните видове документи (не фактури, по изричен
    избор на потребителя) — invoice_br/invoice_no връщат 404 през този път.
  - Страницата не показва НИЩО друго освен самия документ — нито
    страничната лента/навигация на програмата, нито лентата с бутони
    (редакция/износ/списък), нито секцията за прикачени файлове."""
import json

from conftest import post_with_csrf

import qr_code


def _issue_cmr(admin_client, consignee_name="Публичен Тест Клиент ЕООД"):
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач ЕООД", "consignee_name": consignee_name,
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    return doc_id


def _issue_invoice_br(admin_client, consignee_name="Публичен Тест Фактура ЕООД"):
    resp = post_with_csrf(admin_client, "/invoice-br/new", {
        "consignee_name": consignee_name,
    }, csrf_source_url="/invoice-br/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    return doc_id


def _public_token(db_module, doc_id):
    con = db_module.get_db()
    row = con.execute("SELECT public_token FROM documents WHERE id = ?", (doc_id,)).fetchone()
    con.close()
    return row["public_token"]


# ---------------------------------------------------------------- достъп без вход

def test_normal_doc_view_still_requires_login(client, admin_client):
    """КРИТИЧЕН регресионен тест — открит по трудния начин при разработката
    на тази функционалност: добавянето на _public_doc_url/_public_doc_context
    точно преди view_document за малко не откачи @login_required decorator-а
    от view_document (Python decorator-ите просто декорират СЛЕДВАЩата
    дефиниция отдолу — вмъкване на нова функция между decorator и целта ѝ я
    прикача към новата функция вместо това). Резултатът би бил ЧЕТЕНЕ БЕЗ
    ВХОД на /doc/<id> за произволно ID — точно обратното на изричния избор
    да няма публичен достъп през предвидимо номериран адрес (само през
    непредвидимия public_token, виж останалите тестове тук). Останалата
    част от тестовия пакет не покриваше този инвариант никъде другаде."""
    doc_id = _issue_cmr(admin_client)
    resp = client.get("/doc/%d" % doc_id, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_issued_cmr_gets_a_public_token(admin_client, db_module):
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    assert token
    assert len(token) == 32


def test_public_url_opens_the_document_without_any_login(client, admin_client, db_module):
    """`client` тук е БЕЗ логнат потребител — точно сценарият на непознат,
    сканирал QR кода на бланката с телефона си."""
    doc_id = _issue_cmr(admin_client, "Сканиран Клиент ЕООД")
    token = _public_token(db_module, doc_id)
    resp = client.get("/p/%s" % token)
    assert resp.status_code == 200
    assert "Сканиран Клиент ЕООД".encode() in resp.data
    assert "ЧМР".encode() in resp.data


def test_unknown_public_token_returns_404(client):
    resp = client.get("/p/does-not-exist-at-all")
    assert resp.status_code == 404


def test_invoice_public_token_is_not_reachable_through_the_public_route(client, admin_client, db_module):
    """По изричен избор: само вече баркодираните видове документи получават
    публичен QR — фактурите остават достъпни само с вход, дори токенът им
    технически да съществува в базата."""
    doc_id = _issue_invoice_br(admin_client)
    token = _public_token(db_module, doc_id)
    assert token  # токенът съществува в базата...
    resp = client.get("/p/%s" % token)
    assert resp.status_code == 404  # ...но пътят е затворен


# ---------------------------------------------------------------- „само документа“

def test_public_view_has_no_sidebar_or_navigation(client, admin_client, db_module):
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    body = client.get("/p/%s" % token).data.decode()
    assert 'id="sidebar"' not in body
    assert 'id="global-scan-form"' not in body
    assert 'sidebar-scan' not in body


def test_public_view_has_no_toolbar_buttons(client, admin_client, db_module):
    """Нито редакция/износ/списък с документи, нито секцията за прикачени
    файлове — заявка: „само документа, нищо друго да не вижда“."""
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    body = client.get("/p/%s" % token).data.decode()
    assert 'class="doc-toolbar' not in body
    assert "Редактирай" not in body
    assert "Прикачени снимки" not in body


def test_logged_in_admin_also_sees_the_stripped_public_view(admin_client, db_module):
    """Дори ако сканиращият случайно ползва браузър с активна сесия в
    програмата, публичният адрес пак показва САМО документа (public_view
    отменя session.user_id в templates/base.html)."""
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    body = admin_client.get("/p/%s" % token).data.decode()
    assert 'id="sidebar"' not in body
    assert 'class="doc-toolbar' not in body


def test_normal_view_document_still_has_full_toolbar_and_sidebar(admin_client):
    """Контролен тест — обичайният /doc/<id> (с вход) НЕ е засегнат."""
    doc_id = _issue_cmr(admin_client)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert 'id="sidebar"' in body
    assert 'class="doc-toolbar' in body


# ---------------------------------------------------------------- QR кодът на бланката

def test_normal_document_view_includes_a_qr_code_for_the_public_url(admin_client, db_module,
                                                                    monkeypatch):
    """Проверява не само, че ИМА QR блок, а че той наистина кодира точния
    очакван публичен адрес — сравнявайки цялото PNG data URI (детерминирано
    за еднакъв вход), не само присъствие на текст (самият адрес не се
    показва като видим текст, само вграден в пикселите на QR картинката).

    „localhost“ на тестовия клиент се замества с LAN IP адреса на машината
    (поправка: „QR кодът не показва документа“ — 127.0.0.1/localhost на
    телефона сочи самия телефон); тук net.lan_ip е фиксиран, за да е
    детерминиран очакваният адрес."""
    import routes_documents
    monkeypatch.setattr(routes_documents.net, "lan_ip", lambda: "192.168.7.42")
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert 'class="doc-qr"' in body
    expected_url = "http://192.168.7.42/p/%s" % token
    expected_data_uri = qr_code.qr_png_data_uri(expected_url)
    assert expected_data_uri in body


# ------------------------------------------------- изборът на адрес за QR кода
# Поправка на реален проблем (заявка: „QR кодът не показва документа —
# поправи всеки да го вижда“): вграденият адрес беше буквално този от
# браузъра на оператора (на инсталираната програма — http://127.0.0.1:5000),
# а 127.0.0.1 на телефона сочи самия телефон. Редът на избор вече е:
# активен „Отдалечен достъп“ (публичен https, отвсякъде) → LAN IP вместо
# localhost (офисната Wi-Fi мрежа) → адресът както си е.

def _fixed_tunnel(monkeypatch, status="running", url="https://qr-test.trycloudflare.com"):
    import routes_documents
    monkeypatch.setattr(routes_documents.remote_tunnel, "status",
                        lambda: {"status": status, "url": url, "error": None})


def test_qr_prefers_the_remote_access_tunnel_url_when_running(admin_client, db_module,
                                                              monkeypatch):
    """Одит (19.08.2026, находка №21) — ПРОМЕНЕНО поведение спрямо
    първоначалното.

    Дотогава активният тунел влизаше направо в QR кода. Проблемът е, че
    страницата /doc/<id> Е печатната бланка: `*.trycloudflare.com`
    поддомейнът е случаен, тунелът се самоспира след 2 часа, а Cloudflare
    преизползва тези поддомейни за ЧУЖДИ тунели — отпечатана, товарителницата
    рано или късно води сканиращия (шофьор, митничар) към нечий чужд сървър.

    Сега: в QR кода влиза СТАБИЛНИЯТ локален/мрежов адрес, а временният
    публичен адрес се показва като връзка САМО на екрана (клас no-print),
    за да остане функцията „всеки да го вижда“ достъпна, без да я
    отпечатваме върху документ, който ще надживее тунела."""
    _fixed_tunnel(monkeypatch)
    import routes_documents
    monkeypatch.setattr(routes_documents.net, "lan_ip", lambda: "10.0.0.9")
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()

    tunnel_qr = qr_code.qr_png_data_uri("https://qr-test.trycloudflare.com/p/%s" % token)
    assert tunnel_qr not in body, (
        "ефимерният тунелен адрес НЕ бива да влиза в QR кода на бланката")
    # ...но операторът трябва да го вижда на екрана, за да може да го сподели.
    assert "https://qr-test.trycloudflare.com/p/%s" % token in body
    assert "no-print" in body


def test_qr_ignores_a_tunnel_that_is_not_actually_running(admin_client, db_module,
                                                          monkeypatch):
    """Тунел в състояние starting/error (без готов адрес) не се ползва —
    пада се към LAN IP заместването."""
    _fixed_tunnel(monkeypatch, status="starting", url=None)
    import routes_documents
    monkeypatch.setattr(routes_documents.net, "lan_ip", lambda: "10.0.0.9")
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert qr_code.qr_png_data_uri("http://10.0.0.9/p/%s" % token) in body


def test_qr_falls_back_to_the_browser_host_when_lan_ip_is_unknown(admin_client, db_module,
                                                                  monkeypatch):
    """Без мрежа изобщо (lan_ip → None) поведението е старото — адресът от
    браузъра, какъвто е (по-добре QR с localhost, отколкото никакъв)."""
    import routes_documents
    monkeypatch.setattr(routes_documents.net, "lan_ip", lambda: None)
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert qr_code.qr_png_data_uri("http://localhost/p/%s" % token) in body


def test_qr_keeps_a_real_public_domain_untouched(admin_client, monkeypatch):
    """Гледан през собствен публичен домейн (напр. вече през тунела),
    адресът не се пипа — той е публично достъпен. (Тества се самата
    функция в изкуствен request контекст с този домейн — test client-ът
    на Flask връзва бисквитката на сесията към localhost и смяната на
    Host би загубила входа.)"""
    import routes_documents
    monkeypatch.setattr(routes_documents.net, "lan_ip",
                        lambda: (_ for _ in ()).throw(AssertionError("не бива да се вика")))
    with admin_client.application.test_request_context(base_url="https://erp.example.com/"):
        url, local = routes_documents._public_doc_url("abc123")
    assert url == "https://erp.example.com/p/abc123"
    assert local is False


def test_local_qr_shows_an_on_screen_hint_but_public_domain_does_not(admin_client,
                                                                     db_module, monkeypatch):
    """При локален/частен адрес операторът вижда подсказка (клас no-print —
    не излиза на разпечатката) как да включи достъп отвсякъде; при активен
    тунел подсказката я няма — адресът вече е публичен."""
    import routes_documents
    monkeypatch.setattr(routes_documents.net, "lan_ip", lambda: "192.168.7.42")
    doc_id = _issue_cmr(admin_client)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert 'class="doc-qr-hint no-print"' in body

    # Одит (19.08.2026, находка №21): при активен тунел подсказката „включете
    # отдалечен достъп“ отпада, но на нейно място идва ДРУГА (пак no-print) —
    # временният публичен адрес. Проверяваме, че старата вече не се показва.
    _fixed_tunnel(monkeypatch)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert "включете „Отдалечен достъп“" not in body


def test_public_phone_view_never_shows_the_operator_hint(client, admin_client,
                                                         db_module, monkeypatch):
    """Човекът, отворил документа през телефона си, вече го вижда —
    подсказката за оператора няма какво да му каже."""
    import routes_documents
    monkeypatch.setattr(routes_documents.net, "lan_ip", lambda: "192.168.7.42")
    doc_id = _issue_cmr(admin_client)
    token = _public_token(db_module, doc_id)
    body = client.get("/p/%s" % token).data.decode()
    assert 'class="doc-qr"' in body
    assert "doc-qr-hint" not in body


def test_lan_ip_returns_a_usable_address_or_none():
    """net.lan_ip връща или истински (не loopback) IPv4 адрес, или None —
    никога 127.x, който би върнал проблема обратно."""
    import ipaddress
    import net
    ip = net.lan_ip()
    if ip is not None:
        parsed = ipaddress.ip_address(ip)
        assert not parsed.is_loopback


def test_draft_preview_has_no_qr_code(admin_client):
    """Черновата (предварителен преглед, без запис) все още няма
    public_token — не бива да показва счупен/празен QR."""
    resp = post_with_csrf(admin_client, "/cmr/preview", {
        "sender_name": "Преглед ЕООД", "consignee_name": "Преглед Клиент",
    }, csrf_source_url="/cmr/new", follow_redirects=True)
    assert 'class="doc-qr"' not in resp.data.decode()


def test_invoice_document_view_has_no_qr_code(admin_client):
    """Фактурите не показват QR на самата бланка (по изричен избор), макар
    да си имат public_token в базата (виж теста по-горе за /p/)."""
    doc_id = _issue_invoice_br(admin_client)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert 'class="doc-qr"' not in body


def test_pallet_label_format_has_no_qr_code_but_full_format_does(admin_client):
    """Малкият етикет (100×150мм) е твърде тесен за допълнителен QR до
    вече наличния баркод — само пълният A4 формат го показва."""
    items = json.dumps([{"order_no": "PO-1", "pos": "10", "reference": "REF-1",
                         "reference_desc": "Материал", "qty": "5"}])
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "pallet_no": "1", "client_name": "QR Тест Клиент", "items_format": "orders",
        "items_json": items,
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])

    full_body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert 'class="doc-qr"' in full_body

    label_body = admin_client.get("/doc/%d?format=label" % doc_id).data.decode()
    assert 'class="doc-qr"' not in label_body


_ALL_BARCODED_ISSUE_REQUESTS = [
    ("/cmr/new", {"sender_name": "Изпращач ЕООД", "consignee_name": "Клиент"}),
    ("/packing/new", {"sender_name": "Изпращач ЕООД", "consignee_name": "Клиент"}),
    ("/waybill/new", {"sender_name": "Изпращач ЕООД"}),
    ("/dualuse/new", {"sender_name": "Износител ЕООД"}),
    ("/export-it/new", {"declarant_name": "Декларатор"}),
]


def test_all_barcoded_document_types_get_a_working_qr_and_public_view(admin_client, client, db_module):
    """Всичките пет останали баркодирани типа документи (палетната карта
    вече е покрита отделно по-горе, заради своя специфичен label_format) —
    всеки получава QR в обичайния изглед И е реално достъпен през
    публичния адрес, без вход."""
    for url, data in _ALL_BARCODED_ISSUE_REQUESTS:
        resp = post_with_csrf(admin_client, url, data, csrf_source_url=url,
                              follow_redirects=False)
        assert resp.status_code == 302, "%s: %r" % (url, resp.data)
        doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
        token = _public_token(db_module, doc_id)
        assert token, url

        full_body = admin_client.get("/doc/%d" % doc_id).data.decode()
        assert 'class="doc-qr"' in full_body, url

        public_resp = client.get("/p/%s" % token)
        assert public_resp.status_code == 200, url
        public_body = public_resp.data.decode()
        assert 'id="sidebar"' not in public_body, url
        assert 'class="doc-toolbar' not in public_body, url


def test_public_view_of_pallet_card_also_shows_the_qr_code(client, admin_client, db_module):
    items = json.dumps([{"order_no": "PO-1", "pos": "10", "reference": "REF-1",
                         "reference_desc": "Материал", "qty": "5"}])
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "pallet_no": "2", "client_name": "QR Публичен Клиент", "items_format": "orders",
        "items_json": items,
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    token = _public_token(db_module, doc_id)

    body = client.get("/p/%s" % token).data.decode()
    assert 'class="doc-qr"' in body
    assert "QR Публичен Клиент" in body
