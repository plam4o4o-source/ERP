# -*- coding: utf-8 -*-
"""Действия върху документи — одит 01.10.2026: „Копирай като нов“ (P1),
„Издай“ от предварителния преглед (P5), публичният адрес с „Копирай“ (P6),
проверките при издаване на фактура (U5), печатът на фактурите (U8),
съобщението при изтриване (U12), предложеният номер на фактура (P4) и
името на клиента в списъка без json.loads (F1d)."""
import html
import json
import re
from datetime import date

import pytest

from conftest import get_csrf_token, post_with_csrf

YEAR = date.today().year


def _issue(client, url, fields, expect_ok=True):
    resp = post_with_csrf(client, url, fields, csrf_source_url=url, follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    if expect_ok:
        assert "/doc/" in location, location
        return int(location.rstrip("/").split("/")[-1])
    return location


def _doc_count(db_module):
    con = db_module.get_db()
    try:
        return con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        con.close()


def _doc_row(db_module, doc_id):
    con = db_module.get_db()
    try:
        row = con.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return row, json.loads(row["data"])
    finally:
        con.close()


def _edit_data(body):
    m = re.search(r"data-edit='([^']*)'", body)
    assert m, "формата не е предварително попълнена (няма data-edit)"
    return json.loads(html.unescape(m.group(1)))


def _toasts(body):
    return re.findall(r'class="toast toast-(\w+)', body)


ITEM_BR = {"material_code": "MAT-1", "qty": "2", "unit_price": "1.5", "net_weight": "0.5"}


# ---------------------------------------------------------------- U5 фактури

def test_invoice_without_any_item_row_is_rejected_and_the_form_keeps_the_data(admin_client, db_module):
    location = _issue(admin_client, "/invoice-no/new", {
        "consignee_name": "Норвежки получател", "invoice_number": "NO-EMPTY",
        # Ред само с подразбиращия се HS код не е ред със стока.
        "items_json": json.dumps([{"hs_code": "85389099", "qty": ""}]),
    }, expect_ok=False)
    assert "/invoice-no/new?restore=" in location
    assert _doc_count(db_module) == 0
    body = admin_client.get(location).get_data(as_text=True)
    assert "няма нито един ред със стока" in body
    assert _edit_data(body)["consignee_name"] == "Норвежки получател"


def test_invoice_number_used_by_another_invoice_type_blocks_until_confirmed(admin_client, db_module):
    """Одит (04.10.2026, F2): номер на фактура от ДРУГ тип вече СПИРА
    издаването (досега само предупреждаваше — след като документът вече е
    издаден); второто „Издай“ със същия номер е изричното потвърждение."""
    _issue(admin_client, "/invoice-br/new", {
        "consignee_name": "ABB", "invoice_number": "CROSS-1", "items_json": json.dumps([ITEM_BR])})
    fields = {"consignee_name": "ABB", "invoice_number": "CROSS-1",
              "items_json": json.dumps([{"description": "x", "qty": "1", "unit_price": "1"}])}
    resp = post_with_csrf(admin_client, "/invoice-no/new", fields,
                          csrf_source_url="/invoice-no/new", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert _doc_count(db_module) == 1
    assert "CROSS-1 вече е използван през %d г. за Фактура за Бразилия" % YEAR in body
    assert _toasts(body) == ["error"], _toasts(body)
    assert _edit_data(body)["invoice_number"] == "CROSS-1", "въведеното трябва да е запазено"
    resp = post_with_csrf(admin_client, "/invoice-no/new", fields,
                          csrf_source_url="/invoice-no/new", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert _doc_count(db_module) == 2
    assert "toast-warning" in body and "след Вашето потвърждение" in body


def test_same_type_duplicate_number_gives_exactly_one_message(admin_client, db_module):
    fields = {"consignee_name": "ABB", "invoice_number": "DUP-ONE",
              "items_json": json.dumps([ITEM_BR])}
    _issue(admin_client, "/invoice-br/new", fields)
    resp = post_with_csrf(admin_client, "/invoice-br/new", fields,
                          csrf_source_url="/invoice-br/new", follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert _doc_count(db_module) == 1
    assert _toasts(body) == ["error"], _toasts(body)
    assert "вече има издаден документ с номер DUP-ONE" in body
    assert _edit_data(body)["invoice_number"] == "DUP-ONE", "въведеното трябва да е запазено"


def test_same_type_duplicate_on_edit_gives_exactly_one_message(admin_client, db_module):
    _issue(admin_client, "/invoice-br/new", {
        "consignee_name": "ABB", "invoice_number": "E-1", "items_json": json.dumps([ITEM_BR])})
    second = _issue(admin_client, "/invoice-br/new", {
        "consignee_name": "ABB", "invoice_number": "E-2", "items_json": json.dumps([ITEM_BR])})
    resp = post_with_csrf(admin_client, "/doc/%d/edit" % second, {
        "consignee_name": "ABB", "invoice_number": "E-1", "items_json": json.dumps([ITEM_BR])},
        follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert _toasts(body) == ["error"], _toasts(body)
    assert _doc_row(db_module, second)[0]["number"] == "E-2"


# ---------------------------------------------------------------- P4 предложен номер

def test_new_invoice_form_suggests_the_next_number_across_invoice_types(admin_client):
    _issue(admin_client, "/invoice-br/new", {
        "consignee_name": "ABB", "invoice_number": "%d-0042" % YEAR,
        "items_json": json.dumps([ITEM_BR])})
    _issue(admin_client, "/invoice-br/new", {
        "consignee_name": "ABB", "invoice_number": "%d-0007" % YEAR,
        "items_json": json.dumps([ITEM_BR])})
    body = admin_client.get("/invoice-br/new").get_data(as_text=True)
    assert 'value="%d-0043"' % YEAR in body
    # Одит (04.10.2026, F2): номерацията на фактурите е ОБЩА — другите типове
    # предлагат същия следващ номер, а не номер, който вече може да е зает.
    other = admin_client.get("/invoice-no/new").get_data(as_text=True)
    assert 'value="%d-0043"' % YEAR in other


def test_suggestion_ignores_automatic_internal_numbers(admin_client):
    # Празен ръчен номер → автоматичен вътрешен „0001/<година>“.
    _issue(admin_client, "/invoice-dubai/new", {
        "consignee_name": "ABB", "invoice_number": "", "items_json": json.dumps([ITEM_BR])})
    body = admin_client.get("/invoice-dubai/new").get_data(as_text=True)
    assert re.search(r'name="invoice_number" required\s+value=""', body), (
        "автоматичният номер „0001/%d“ не е основа за предложение" % YEAR)


# ---------------------------------------------------------------- P1 копирай като нов

COPY_CASES = [
    ("cmr", "/cmr/new", {"consignee_name": "Копие ЕООД", "goods": "Табла",
                         "established_date": "2026-01-05"}),
    ("packing", "/packing/new", {"receiver_name": "Копие ЕООД", "doc_date": "2026-01-05",
                                 "items_json": json.dumps([{"description": "Кашон А", "qty": "3"}])}),
    ("pallet", "/pallet/new", {"client_name": "Копие ЕООД", "pallet_no": "2", "items_format": "orders",
                               "doc_date": "2026-01-05",
                               "items_json": json.dumps([{"order_no": "4700", "pos": "10",
                                                         "reference": "R-1", "qty": "5"}])}),
    ("waybill", "/waybill/new", {"consignee_name": "Копие ЕООД", "loading_date": "2026-01-05",
                                 "items_json": json.dumps([{"description": "Палет", "qty": "1"}])}),
    ("dualuse", "/dualuse/new", {"sender_name": "Копие ЕООД", "doc_date": "2026-01-05",
                                 "invoice_numbers": "0000001234", "destination_country": "Турция",
                                 "declarant_name": "Иван Петров",
                                 "items_json": json.dumps([{"description": "Уред", "qty": "1"}])}),
    ("export_it", "/export-it/new", {"receiver_name": "Копие ЕООД", "doc_date": "2026-01-05",
                                     "invoice_no": "0000001234",
                                     "items_json": json.dumps([{"description": "Уред", "qty": "1"}])}),
    ("invoice_br", "/invoice-br/new", {"consignee_name": "Копие ЕООД", "invoice_number": "CP-BR",
                                       "doc_date": "2026-01-05", "items_json": json.dumps([ITEM_BR])}),
    ("invoice_no", "/invoice-no/new", {"consignee_name": "Копие ЕООД", "invoice_number": "CP-NO",
                                       "items_json": json.dumps([{"description": "x", "qty": "1",
                                                                  "unit_price": "2"}])}),
    ("invoice_dubai", "/invoice-dubai/new", {"consignee_name": "Копие ЕООД", "invoice_number": "CP-DU",
                                             "items_json": json.dumps([ITEM_BR])}),
]


@pytest.mark.parametrize("doc_type,url,fields", COPY_CASES, ids=[c[0] for c in COPY_CASES])
def test_copy_as_new_prefills_the_form_and_issues_a_new_number(admin_client, db_module,
                                                                doc_type, url, fields):
    original_id = _issue(admin_client, url, fields)
    original, original_data = _doc_row(db_module, original_id)
    view = admin_client.get("/doc/%d" % original_id).get_data(as_text=True)
    assert "/doc/%d/copy" % original_id in view and "Копирай като нов" in view

    resp = admin_client.get("/doc/%d/copy" % original_id)
    assert resp.status_code == 302 and (url + "?restore=") in resp.headers["Location"]
    body = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    prefill = _edit_data(body)
    assert "Копие ЕООД" in json.dumps(prefill, ensure_ascii=False)
    for key in ("number", "barcode", "doc_date", "established_date", "loading_date",
                "invoice_number"):
        assert key not in prefill, key
    if original_data.get("items"):
        assert prefill["items"] == original_data["items"]

    # Издаване от попълнената форма → НОВ документ с нов номер.
    resubmit = {k: v for k, v in prefill.items() if k != "items"}
    if "items" in prefill:
        resubmit["items_json"] = json.dumps(prefill["items"])
    if doc_type.startswith("invoice_"):
        resubmit["invoice_number"] = "NEW-" + doc_type
    new_id = _issue(admin_client, url, resubmit)
    new_row, new_data = _doc_row(db_module, new_id)
    assert new_id != original_id
    assert new_row["number"] != original["number"]
    assert new_row["barcode"] != original["barcode"]
    assert new_row["public_token"] != original["public_token"]
    assert "Копие ЕООД" in json.dumps(new_data, ensure_ascii=False)


def test_copy_of_an_unknown_document_is_404(admin_client):
    assert admin_client.get("/doc/999/copy").status_code == 404


# ---------------------------------------------------------------- P5 „Издай“ от прегледа

def _preview(client, url, fields):
    resp = post_with_csrf(client, url, fields, csrf_source_url="/", follow_redirects=False)
    assert resp.status_code == 302 and "/preview/" in resp.headers["Location"]
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def test_issue_directly_from_the_preview_of_a_new_document(admin_client, db_module):
    token = _preview(admin_client, "/packing/preview", {
        "receiver_name": "От прегледа", "items_json": json.dumps([{"description": "А", "qty": "1"}])})
    page = admin_client.get("/preview/%s" % token).get_data(as_text=True)
    assert 'action="/preview/%s/issue"' % token in page and "Издай" in page
    assert _doc_count(db_module) == 0

    csrf = get_csrf_token(admin_client, "/preview/%s" % token)
    resp = admin_client.post("/preview/%s/issue" % token, data={"csrf_token": csrf})
    assert resp.status_code == 302 and "/doc/" in resp.headers["Location"]
    doc_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    row, data = _doc_row(db_module, doc_id)
    assert data["receiver_name"] == "От прегледа" and data["items"][0]["description"] == "А"

    # Второ натискане (двоен клик/F5) не издава втори документ.
    again = admin_client.post("/preview/%s/issue" % token, data={"csrf_token": csrf})
    assert again.headers["Location"].endswith("/doc/%d" % doc_id)
    assert _doc_count(db_module) == 1


def test_issue_from_preview_requires_csrf(admin_client, db_module):
    token = _preview(admin_client, "/cmr/preview", {"consignee_name": "X"})
    assert admin_client.post("/preview/%s/issue" % token, data={}).status_code == 400
    assert _doc_count(db_module) == 0


def test_issue_from_preview_runs_the_same_invoice_validations(admin_client, db_module):
    token = _preview(admin_client, "/invoice-br/preview", {
        "consignee_name": "Празна", "invoice_number": "PV-0", "items_json": "[]"})
    csrf = get_csrf_token(admin_client, "/preview/%s" % token)
    resp = admin_client.post("/preview/%s/issue" % token, data={"csrf_token": csrf})
    assert "/invoice-br/new?restore=" in resp.headers["Location"]
    assert _doc_count(db_module) == 0


def test_save_edit_directly_from_the_preview_with_version_check(admin_client, db_module):
    doc_id = _issue(admin_client, "/cmr/new", {"consignee_name": "Старо име"})
    version = _doc_row(db_module, doc_id)[0]["version"]
    token = _preview(admin_client, "/cmr/preview", {
        "consignee_name": "Ново име", "edit_doc_id": str(doc_id), "edit_doc_version": str(version)})
    page = admin_client.get("/preview/%s" % token).get_data(as_text=True)
    assert "Запази промените" in page
    csrf = get_csrf_token(admin_client, "/preview/%s" % token)
    resp = admin_client.post("/preview/%s/issue" % token, data={"csrf_token": csrf})
    assert resp.headers["Location"].endswith("/doc/%d" % doc_id)
    row, data = _doc_row(db_module, doc_id)
    assert data["consignee_name"] == "Ново име" and row["version"] == version + 1
    assert _doc_count(db_module) == 1


def test_save_edit_from_a_stale_preview_is_a_conflict_not_an_overwrite(admin_client, db_module):
    doc_id = _issue(admin_client, "/cmr/new", {"consignee_name": "Първо"})
    version = _doc_row(db_module, doc_id)[0]["version"]
    token = _preview(admin_client, "/cmr/preview", {
        "consignee_name": "От стария преглед", "edit_doc_id": str(doc_id),
        "edit_doc_version": str(version)})
    post_with_csrf(admin_client, "/doc/%d/edit" % doc_id, {"consignee_name": "Чужда редакция"})
    csrf = get_csrf_token(admin_client, "/preview/%s" % token)
    resp = admin_client.post("/preview/%s/issue" % token, data={"csrf_token": csrf})
    assert "/doc/%d/edit?restore=" % doc_id in resp.headers["Location"]
    assert _doc_row(db_module, doc_id)[1]["consignee_name"] == "Чужда редакция"


# ---------------------------------------------------------------- P6 публичен адрес

def test_public_link_panel_shows_the_full_url_with_a_copy_button(admin_client, db_module):
    doc_id = _issue(admin_client, "/cmr/new", {"consignee_name": "X"})
    token = _doc_row(db_module, doc_id)[0]["public_token"]
    body = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    m = re.search(r'<input type="text" id="(public-link-url-%d)" value="([^"]+)" readonly' % doc_id, body)
    assert m and m.group(2).endswith("/p/%s" % token)
    assert 'data-copy-target="#%s"' % m.group(1) in body


# ---------------------------------------------------------------- U8 печат на фактурите

def test_invoice_print_formats_unit_prices_and_bank_line(admin_client, db_module):
    items = [{"material_code": "A", "qty": "10", "unit_price": "12.5", "net_weight": "0.2"},
             {"material_code": "B", "qty": "20", "unit_price": "0,0125", "net_weight": "4.51"},
             {"material_code": "C", "qty": "1", "unit_price": "3", "net_weight": ""}]
    doc_id = _issue(admin_client, "/invoice-br/new", {
        "consignee_name": "ABB", "invoice_number": "PR-1",
        "bank_details": "IBAN : BG1   / Postbank /", "items_json": json.dumps(items)})
    body = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert '<td class="r">12.50</td>' in body
    assert '<td class="r">0.0125</td>' in body
    assert '<td class="r">3.00</td>' in body
    assert "IBAN : BG1   / Postbank<" in body and "Postbank /" not in body
    # Бразилската бланка следва точно образеца, без общо тегло (заявка:
    # „във фактурите премахни колона Total weight“).
    assert "Total Net Weight" not in body and "Total weight" not in body


@pytest.mark.parametrize("url", ["/invoice-no/new", "/invoice-dubai/new"])
def test_other_invoices_print_unit_prices_with_two_decimals(admin_client, url):
    doc_id = _issue(admin_client, url, {
        "consignee_name": "ABB", "invoice_number": "PR-" + url[9:11],
        "items_json": json.dumps([{"description": "x", "material_code": "A",
                                   "qty": "4", "unit_price": "7.1"}])})
    body = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert '<td class="r">7.10</td>' in body


# ---------------------------------------------------------------- U12 изтриване

def test_delete_message_names_the_document_type_and_number(admin_client):
    doc_id = _issue(admin_client, "/cmr/new", {"consignee_name": "X"})
    resp = post_with_csrf(admin_client, "/doc/%d/delete" % doc_id, {},
                          csrf_source_url="/doc/%d" % doc_id, follow_redirects=True)
    assert "ЧМР товарителница № 0001/%d е изтрит(а)." % YEAR in resp.get_data(as_text=True)


# ---------------------------------------------------------------- F1d списък

def test_documents_list_takes_the_client_name_from_the_column(admin_client, monkeypatch):
    import routes_documents
    _issue(admin_client, "/packing/new", {"receiver_name": "Колонен Клиент"})

    def boom(_raw):
        raise AssertionError("списъкът не бива да разбира JSON-а на всеки ред")

    monkeypatch.setattr(routes_documents, "safe_json_data", boom, raising=False)
    body = admin_client.get("/docs").get_data(as_text=True)
    assert "Колонен Клиент" in body
