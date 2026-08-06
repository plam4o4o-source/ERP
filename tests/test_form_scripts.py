# -*- coding: utf-8 -*-
"""Регресионни тестове за преместването на вградените <script> блокове на
шестте формуляра за документи (ЧМР/опаковъчен лист/палетна карта/
товарителница/декларациите) в static/app.js, задвижвано от data-*
атрибути (виж initDocumentForm/initCmrPlaces/initPullFromPallet в
app.js). Товарителницата не беше в обхвата на оригиналния патч
(документният тип не съществуваше по времето на генерирането му) —
приведена ръчно по същия модел.

Тук се проверява РЕНДЕРЪТ (Flask test client, без реален браузър — както
навсякъде другаде в проекта, виж tests/README.md): че шаблоните вече не
вграждат собствен <script> блок, и че съдържат точно тези data-*
атрибути, от които app.js се нуждае, за да свърши същата работа. Реалното
функционално поведение в браузър (автопопълване от адресната книга,
динамични таблици с артикули, каскадните ЧМР пунктове за товарене/
разтоварване, "Добави от палета", предварителното попълване при
редакция) бе проверено ръчно с headless Chromium (Playwright) срещу
работещ dev сървър, точно както при анализа на разположението на логото
(виж CHANGELOG.md) — всички проверени сценарии минаха без регресия и без
конзолни грешки в браузъра, включително известната, НЕПРОМЕНЕНА и отпреди
миграцията особеност, че ЧМР селектът за получател няма name атрибут и
затова не възстановява избрания пункт за разтоварване при отваряне за
редакция (само текстовото поле остава попълнено)."""
import json

from conftest import post_with_csrf

FORM_URLS = {
    "cmr": "/cmr/new",
    "packing": "/packing/new",
    "pallet": "/pallet/new",
    "waybill": "/waybill/new",
    "dualuse": "/dualuse/new",
    "export_it": "/export-it/new",
}


def _no_inline_script(body):
    """Извън споделения <script src=".../app.js">, страницата не бива да
    съдържа никакъв друг <script> блок — целият специфичен за формата JS
    вече е в app.js, задвижван от data-* атрибути."""
    import re
    scripts = re.findall(r"<script\b[^>]*>", body)
    for tag in scripts:
        assert "app.js" in tag or "src=" in tag, (
            "Намерен вграден <script> блок извън app.js: %r" % tag)


def test_app_js_loaded_on_every_document_form(admin_client):
    for doc_type, url in FORM_URLS.items():
        resp = admin_client.get(url)
        assert resp.status_code == 200, doc_type
        assert b'src="/static/app.js"' in resp.data, doc_type


def test_no_form_embeds_its_own_script_block(admin_client):
    for doc_type, url in FORM_URLS.items():
        resp = admin_client.get(url)
        _no_inline_script(resp.data.decode())


def test_main_doc_form_has_data_clients_attribute(admin_client):
    for doc_type, url in FORM_URLS.items():
        resp = admin_client.get(url)
        body = resp.data.decode()
        assert 'id="main-doc-form"' in body, doc_type
        assert "data-clients='" in body, doc_type


def test_cmr_form_has_no_data_edit_when_issuing_new(admin_client):
    resp = admin_client.get("/cmr/new")
    body = resp.data.decode()
    assert "data-edit=" not in body


def test_edit_mode_renders_data_edit_with_document_json(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач ЕООД", "consignee_name": "Получател ООД",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_url = resp.headers["Location"]
    doc_id = doc_url.rstrip("/").split("/")[-1]

    edit_resp = admin_client.get("/doc/%s/edit" % doc_id)
    body = edit_resp.data.decode()
    assert "data-edit='" in body
    assert "Получател ООД" in body


def test_packing_items_table_has_columns_and_items_data_attributes(admin_client):
    resp = admin_client.get("/packing/new")
    body = resp.data.decode()
    assert 'id="packing-items"' in body
    assert ('data-columns="description,qty,packing,length,width,height,'
            'volume,net,gross"') in body
    assert "data-items='[]'" in body


def test_packing_pull_pallet_button_has_data_url(admin_client):
    resp = admin_client.get("/packing/new")
    body = resp.data.decode()
    assert 'id="pull-pallet-btn"' in body
    assert 'data-url="/packing/pull-pallet"' in body


def test_waybill_items_table_has_columns_and_items_data_attributes(admin_client):
    """Товарителницата не беше в обхвата на оригиналния патч — приведена
    ръчно по същия модел, виж bележката в модулния докстринг по-горе."""
    resp = admin_client.get("/waybill/new")
    body = resp.data.decode()
    assert 'id="waybill-items"' in body
    assert 'data-columns="description,packing,marks,weight,qty"' in body
    assert "data-items='[]'" in body


def test_pallet_items_table_columns_depend_on_items_format(admin_client, con, db_module):
    # Нова карта (без edit_data) — колоните по подразбиране: код/описание/кол/тегло.
    resp = admin_client.get("/pallet/new")
    assert 'data-columns="code,description,qty,weight"' in resp.data.decode()

    # Импорт от справка за поръчки записва items_format=orders — редакцията
    # на такава карта трябва да зареди ДРУГИЯ набор колони в data-columns.
    cur = con.execute(
        "INSERT INTO clients (name, city, country) VALUES (?, ?, ?)",
        ("Клиент ООД", "София", "България"),
    )
    con.commit()
    data = {
        "client_name": "Клиент ООД", "items_format": "orders",
        "items": [{"order_no": "OR-1", "pos": "10", "reference": "REF",
                  "reference_desc": "Описание", "qty": "3"}],
    }
    con.execute(
        "INSERT INTO documents (doc_type, number, year, seq, barcode, data, created_by)"
        " VALUES ('pallet', '0099/2026', 2026, 99, 'PAL-TEST', ?, NULL)",
        (json.dumps(data, ensure_ascii=False),),
    )
    con.commit()
    doc_id = con.execute(
        "SELECT id FROM documents WHERE barcode = 'PAL-TEST'").fetchone()["id"]

    edit_resp = admin_client.get("/doc/%d/edit" % doc_id)
    body = edit_resp.data.decode()
    # Заявка: „съдържанието на палета да е същото като на импортирания
    # файл“ — пазят се ВСИЧКИ разпознати колони от справката за поръчки
    # (не само 5-те основни), виж routes_pallet_extra._parse_order_export.
    assert ('data-columns="due_date,order_no,pos,project,reference,'
            'reference_desc,qty,unit,stock"') in body
    assert "OR-1" in body  # data-items съдържа предварително заредения ред


def test_dualuse_client_select_has_autofill_country_attribute(admin_client):
    resp = admin_client.get("/dualuse/new")
    body = resp.data.decode()
    assert 'data-target="dest"' in body
    assert 'data-autofill-country="destination_country"' in body


def test_export_it_form_renders_without_inline_script(admin_client):
    resp = admin_client.get("/export-it/new")
    assert resp.status_code == 200
    _no_inline_script(resp.data.decode())


def test_all_six_forms_still_issue_documents_successfully(admin_client):
    """Характеризиращ регресионен тест: миграцията на JS не бива да
    променя server-side поведението при реално издаване на документ."""
    items = json.dumps([{"description": "Стока", "qty": "1"}])
    cases = [
        ("/cmr/new", {"sender_name": "Изпращач", "consignee_name": "Получател"}),
        ("/packing/new", {"receiver_name": "Получател", "items_json": items}),
        ("/pallet/new", {"client_name": "Клиент", "items_json": items}),
        ("/waybill/new", {"sender_name": "Изпращач", "consignee_name": "Получател",
                          "items_json": items}),
        ("/dualuse/new", {"sender_name": "Изпращач"}),
        ("/export-it/new", {"declarant_name": "Иван Иванов"}),
    ]
    for url, data in cases:
        resp = post_with_csrf(admin_client, url, data, csrf_source_url=url,
                              follow_redirects=False)
        assert resp.status_code == 302, (url, resp.data[:300])
        assert "/doc/" in resp.headers["Location"], url
