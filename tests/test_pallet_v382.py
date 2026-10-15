# -*- coding: utf-8 -*-
"""v3.82.0, група PALLET (одит 15.10.2026):

* PALLET-7 — „Нето, кг“ на самата палетна карта (форма, групово издаване,
  печат, износи, издърпване в опаковъчен лист);
* PALLET-10 — „Опаковъчен лист от тази поръчка (PO)“ на изгледа на картата;
* PALLET-8 — връщане към списъка с ?hl=<id> (лентата на документа)."""
import io
import json
import re
from datetime import date

from openpyxl import load_workbook

from conftest import post_with_csrf, read_source
from test_workflow_shortcuts import _add_material, _issue_pallets, _pull, _rows

import routes_pallet_extra


def _issue_single(client, gross="520,5", net=None, client_name="Клиент ЕООД",
                  order_no="4500012345", ref="REF-1", follow=False):
    data = {"client_name": client_name, "gross": gross, "pallet_type": "120×80",
            "items_format": "orders",
            "items_json": json.dumps([{"order_no": order_no, "pos": "10", "reference": ref,
                                       "reference_desc": "Материал", "qty": "2"}])}
    if net is not None:
        data["net"] = net
    resp = post_with_csrf(client, "/pallet/new", data, csrf_source_url="/pallet/new",
                          follow_redirects=follow)
    if follow:
        return resp
    assert resp.status_code == 302, resp.data
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def _doc(db_module, doc_id):
    con = db_module.get_db()
    try:
        row = con.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return row, json.loads(row["data"])
    finally:
        con.close()


def _set(db_module, doc_id, created_at=None, **fields):
    con = db_module.get_db()
    row = con.execute("SELECT data FROM documents WHERE id = ?", (doc_id,)).fetchone()
    data = json.loads(row["data"])
    data.update(fields)
    con.execute("UPDATE documents SET data = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), doc_id))
    if created_at:
        con.execute("UPDATE documents SET created_at = ? WHERE id = ?", (created_at, doc_id))
    con.commit()
    con.close()


# ---------------------------------------------------------------- PALLET-7

def test_form_has_net_field_next_to_gross_with_tare_for_auto(admin_client):
    html = admin_client.get("/pallet/new").get_data(as_text=True)
    # Единичната карта и шаблонът за следващите карти — нето веднага след брутото.
    assert html.count('data-field="gross"') == 2 and html.count('data-field="net"') == 2
    assert re.search(r'name="gross" data-field="gross"></div>\s*(\{#.*?#\}\s*)?'
                     r'<div><label for="f-net">', html, re.S)
    assert 'data-pallet-tare="20"' in html


def test_empty_tare_setting_gives_no_auto_net(admin_client, db_module):
    con = db_module.get_db()
    db_module.save_settings(con, {"packing_pallet_tare": ""})
    con.commit()
    con.close()
    assert 'data-pallet-tare=""' in admin_client.get("/pallet/new").get_data(as_text=True)


def test_single_card_stores_net_and_prints_it_next_to_gross(admin_client, db_module):
    doc_id = _issue_single(admin_client, gross="520,5", net="500,5")
    _row, data = _doc(db_module, doc_id)
    assert data["net"] == "500,5" and data["gross"] == "520,5"
    html = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert 'plt-stats has-net' in html
    assert re.search(r"БРУТО, КГ / GROSS</div><div class=\"val val-key\">520.5</div></div>\s*"
                     r".*?НЕТО, КГ / NET</div><div class=\"val val-key\">500.5<", html, re.S)
    label = admin_client.get("/doc/%d?format=label" % doc_id).get_data(as_text=True)
    assert "НЕТО, КГ / NET" in label


def test_old_card_without_net_keeps_working(admin_client, db_module):
    doc_id = _issue_single(admin_client, gross="300")
    _row, data = _doc(db_module, doc_id)
    assert "net" not in data or data["net"] == ""
    html = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert "НЕТО, КГ / NET" not in html and "has-net" not in html
    # и издърпването в опаковъчен лист остава бруто − тара
    res = _pull(admin_client, "1")
    assert res["row"]["net"] == "280" and res["net_source"] == "tare"


def test_negative_unparsable_and_over_gross_net_are_flagged_on_save(admin_client):
    for net, expected in (("-5", "отрицателна"), ("12 кг", "не може да бъде разчетена"),
                          ("600", "по-голямо от „Бруто, кг“")):
        html = _issue_single(admin_client, gross="520", net=net, follow=True).get_data(as_text=True)
        assert expected in html, net
        assert "Нето, кг" in html, net


def test_bulk_issue_stores_net_per_card_and_flags_bad_values(admin_client, db_module):
    form = {"client_name": "Клиент", "doc_date": date.today().isoformat(), "groups": "1,2,3",
            "doc_lang": "en"}
    for i, (gross, net) in enumerate((("450,5", "430,5"), ("100", "-3"), ("100", "150")), start=1):
        form.update({"items_json_%d" % i: json.dumps(_rows(("A%d" % i, "d", "1"))),
                     "items_format_%d" % i: "orders", "gross_%d" % i: gross, "net_%d" % i: net})
    resp = post_with_csrf(admin_client, "/pallet/bulk-issue", form, csrf_source_url="/pallet/new",
                          follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert "Внимание (карта №2)" in html and "„Нето, кг“" in html
    assert "Внимание (карта №3): „Нето, кг“ (150) е по-голямо от „Бруто, кг“ (100)" in html
    con = db_module.get_db()
    nets = [json.loads(r["data"]).get("net") for r in
            con.execute("SELECT data FROM documents WHERE doc_type='pallet' ORDER BY id")]
    con.close()
    assert nets == ["430,5", "-3", "150"]
    # Списъкът „Издадени палетни карти“ показва и нетото.
    assert ">430.5<" in html.replace(" ", "")


def test_bulk_review_has_net_field_fill_all_and_tare(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2"))])
    drafts = [{"client_name": "К", "items": _rows(("A1", "d", "1")), "items_format": "orders",
               "gross": "500", "net": "470", "pallet_no": "1 of 1"}]
    with admin_client.application.test_request_context():
        token = routes_pallet_extra._store_preview("bulk_pallet", drafts)
    html = admin_client.get("/pallet/bulk-review/restore/%s" % token).get_data(as_text=True)
    assert 'name="net_1" value="470"' in html
    assert 'data-fill="net"' in html and 'data-fill="gross"' in html
    assert 'data-pallet-tare="20"' in html


def test_pull_prefers_card_net_then_tare_then_materials(admin_client, db_module, legacy_packing_pull):
    # legacy_packing_pull: празна тара/описание — проверяваме и пътя „справочник“.
    _add_material(db_module, "A1", "10")
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2"))] * 2)
    _set(db_module, 1, net="90,5")
    res = _pull(admin_client, "1")
    assert res["row"]["net"] == "90,5" and res["net_source"] == "card"
    assert "нето" not in res["note"].lower()
    res = _pull(admin_client, "2")
    assert res["row"]["net"] == "20" and res["net_source"] == "materials"


def test_untrusted_card_net_falls_back_to_gross_minus_tare(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "2"))] * 2)
    for bad in ("-5", "0", "abc", "1.234,5", "150"):  # 150 > бруто 100
        _set(db_module, 1, net=bad)
        res = _pull(admin_client, "1")
        assert res["row"]["net"] == "80" and res["net_source"] == "tare", bad
    _set(db_module, 1, net="77.5")
    res = _pull(admin_client, "1-2")  # и при диапазон
    assert [r["row"]["net"] for r in res["results"]] == ["77.5", "80"]
    assert [r["net_source"] for r in res["results"]] == ["card", "tare"]


def test_pallet_xlsx_pdf_fields_and_list_export_include_net(admin_client, db_module):
    import routes_documents
    doc_id = _issue_single(admin_client, gross="520,5", net="500,5")
    fields, _cols, _items = routes_documents._export_fields_and_items("pallet", _doc(db_module, doc_id)[1])
    labels = [label for label, _v in fields]
    assert labels.index("Нето, кг") == labels.index("Бруто, кг") + 1
    assert dict(fields)["Нето, кг"] == "500,5"
    resp = admin_client.get("/doc/%d/export.xlsx" % doc_id)
    assert resp.status_code == 200
    ws = load_workbook(io.BytesIO(resp.data)).active
    cells = {ws.cell(r, 1).value: ws.cell(r, 2).value for r in range(1, ws.max_row + 1)}
    assert cells.get("Нето, кг") == 500.5
    ws = load_workbook(io.BytesIO(admin_client.get("/docs/export.xlsx?type=pallet").data)).active
    rows = [[c.value for c in row] for row in ws.iter_rows()]
    assert rows[0][7:9] == ["Бруто, кг", "Нето, кг"]
    assert rows[1][7] == 520.5 and rows[1][8] == 500.5


# ---------------------------------------------------------------- PALLET-10

def test_same_po_button_only_when_card_has_po(admin_client, db_module):
    with_po = _issue_single(admin_client)
    without = _issue_single(admin_client, order_no="")
    assert "Опаковъчен лист от тази поръчка (PO)" in \
        admin_client.get("/doc/%d" % with_po).get_data(as_text=True)
    assert "/pallet/%d/packing-same-po" % with_po in \
        admin_client.get("/doc/%d" % with_po).get_data(as_text=True)
    assert "Опаковъчен лист от тази поръчка (PO)" not in \
        admin_client.get("/doc/%d" % without).get_data(as_text=True)
    resp = admin_client.get("/pallet/%d/packing-same-po" % without)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/doc/%d" % without)


def test_same_po_pulls_same_client_cards_within_window(admin_client, db_module):
    a = _issue_single(admin_client, gross="520")
    b = _issue_single(admin_client, gross="300", order_no=" 0004500012345 ")  # водещи нули
    _issue_single(admin_client, client_name="Друг клиент")                    # друг клиент
    _issue_single(admin_client, order_no="4500099999")                         # друго PO
    old = _issue_single(admin_client)                                          # преди 200 дни
    _set(db_module, old, created_at="2020-01-01 10:00:00")
    numbers = {d: _doc(db_module, d)[0]["number"] for d in (a, b)}
    resp = admin_client.get("/pallet/%d/packing-same-po" % a)
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert loc.startswith("/packing/new?")
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(loc).query)
    assert q["pull"] == [",".join([numbers[a], numbers[b]])]
    assert q["receiver_name"] == ["Клиент ЕООД"] and q["order_no"] == ["4500012345"]
    # и обратно — от втората карта се стига до същите две
    q2 = parse_qs(urlsplit(admin_client.get("/pallet/%d/packing-same-po" % b)
                           .headers["Location"]).query)
    assert q2["pull"] == q["pull"]


def test_same_po_is_capped(admin_client, db_module, monkeypatch):
    ids = [_issue_single(admin_client) for _ in range(4)]
    monkeypatch.setattr(routes_pallet_extra, "SAME_PO_MAX_CARDS", 2)
    resp = admin_client.get("/pallet/%d/packing-same-po" % ids[-1], follow_redirects=False)
    from urllib.parse import parse_qs, urlsplit
    pulled = parse_qs(urlsplit(resp.headers["Location"]).query)["pull"][0].split(",")
    assert len(pulled) == 2 and _doc(db_module, ids[-1])[0]["number"] in pulled
    assert "pallet_packing_same_po" in read_source("routes_pallet_extra.py")


def test_same_po_404_for_non_pallet(admin_client):
    assert admin_client.get("/pallet/999/packing-same-po").status_code == 404


# ---------------------------------------------------------------- PALLET-8

def test_view_list_link_carries_hl_and_invoices_go_to_their_list(admin_client):
    doc_id = _issue_single(admin_client)
    html = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert 'data-doc-list-link data-doc-id="%d" href="/docs?hl=%d"' % (doc_id, doc_id) in html
    resp = post_with_csrf(admin_client, "/invoice-br/new", {"consignee_name": "ABB", "invoice_number": "BR-9", "items_json": json.dumps(
                              [{"material_code": "M", "qty": "1", "unit_price": "1"}])},
                          csrf_source_url="/invoice-br/new")
    inv_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    html = admin_client.get("/doc/%d" % inv_id).get_data(as_text=True)
    assert 'href="/invoices?hl=%d"' % inv_id in html and "Към фактурите" in html
    # и самият ред е в списъка с това id (initRowHighlight)
    assert 'data-row-id="%d"' % inv_id in admin_client.get("/invoices").get_data(as_text=True)


def test_app_js_remembers_last_saved_doc_only_by_id_for_its_list():
    src = read_source("static", "app.js")
    assert "function initSavedDocMemory()" in src and "function takeSavedDocId(hl)" in src
    assert "SAVED_DOC_TTL_MS = 10 * 60 * 1000" in src
    body = src.split("function takeSavedDocId(hl)", 1)[1].split("\n}\n", 1)[0]
    assert "saved.list === window.location.pathname" in body
