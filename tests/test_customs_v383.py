# -*- coding: utf-8 -*-
"""v3.83.0, група CUSTOMS — LDM/палетни места/платимо тегло, CN/TARIC код,
Норвегия (Digitoll) проверка преди товарене, MRN/EORI, двойна употреба
(версия на Приложение I), Intrastat износ, eFTI износ на ЧМР (customs.py)."""
import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from conftest import post_with_csrf, with_required
from test_print_layout import _doc


# ---------------------------------------------------------------- чисти функции

def test_cn_code_normalisation_and_validation():
    import customs
    assert customs.normalize_cn(" 8538 90 99 ") == "85389099"
    assert customs.normalize_cn("8538.90.99.00") == "8538909900"
    assert customs.cn_valid("8538 90 99") and customs.cn_valid("8538909900")
    for bad in ("853890", "853890991", "85389099AB", "8538-90-99", "", "８５３８９０９９"):
        assert not customs.cn_valid(bad), bad
    assert customs.cn_state("") == "" and customs.cn_state("85389099") == "ok"
    assert customs.cn_state("8538") == "invalid"
    url = customs.taric_url("8538.90.99")
    assert url.startswith("https://ec.europa.eu/taxation_customs/dds2/taric/measures.jsp?")
    assert "Taric=85389099" in url
    assert customs.taric_url("12") == ""


def test_eori_and_mrn_formats():
    import customs
    assert customs.eori_valid("bg 123456789") and customs.eori_valid("DE1234567890123")
    assert not customs.eori_valid("123456789") and not customs.eori_valid("BG")
    assert not customs.eori_valid("BG1234567890123456")
    assert customs.mrn_valid("26BG002004001234E5") and customs.mrn_valid("26 bg 0020 0400 1234 E5")
    assert not customs.mrn_valid("26BG002004001234E") and not customs.mrn_valid("BG26002004001234E5")


@pytest.mark.parametrize("text,code", [
    ("Норвегия", "NO"), ("Norway", "NO"), ("NO", "NO"), ("Oslo, Norge", "NO"),
    ("Германия", "DE"), ("Deutschland", "DE"), ("de", "DE"), ("EL", "GR"),
    ("ABB AS\nOstre Aker vei 24\n0581 Oslo\nNorway", "NO"),
    ("Müller GmbH\nMünchen, Germany", "DE"), ("São Paulo, Brasil", "BR"),
    ("Österreich", "AT"), ("Холандия", "NL"), ("", ""), ("Плевен", ""),
])
def test_country_code_from_free_text(text, code):
    import customs
    assert customs.country_code(text) == code


def test_destination_country_by_document_type():
    import customs
    assert customs.destination_country("invoice_no", {}) == "NO"
    assert customs.destination_country("cmr", {"consignee_country": "Norway"}) == "NO"
    assert customs.destination_country("cmr", {"place_delivery": "Drammen, Норвегия"}) == "NO"
    assert customs.destination_country("packing", {"receiver_country": "Германия"}) == "DE"
    assert not customs.is_norway("cmr", {"consignee_country": "Турция"})


def test_ldm_formulas():
    import customs
    full = customs.compute_ldm([{"type": "eur", "qty": "33"}], gross="5000", factor=1850)
    assert str(full["ldm"]) == "13.20" and full["pallet_places"] == 33
    assert full["chargeable_weight"] == 24420  # 13,20 × 1850 > 5000 бруто
    assert full["volumetric_weight"] == 24420 and not full["over_capacity"]
    one = customs.compute_ldm([{"type": "eur", "qty": "1"}], gross="800")
    assert str(one["ldm"]) == "0.40" and one["pallet_places"] == 1
    assert one["chargeable_weight"] == 800  # 0,40 × 1850 = 740 < 800
    # стифиране 2 етажа: 10 палета → 5 основи
    stacked = customs.compute_ldm([{"type": "eur", "qty": "10", "stack": "2"}])
    assert str(stacked["ldm"]) == "2.00"
    odd = customs.compute_ldm([{"type": "eur", "qty": "3", "stack": "2"}])
    assert str(odd["ldm"]) == "0.80"  # ⌈3/2⌉ = 2 основи
    ind = customs.compute_ldm([{"type": "ind", "qty": "2"}])
    assert str(ind["ldm"]) == "1.00" and ind["pallet_places"] == 3
    custom = customs.compute_ldm([{"type": "custom", "qty": "1", "length": "2400", "width": "1200"}])
    assert str(custom["ldm"]) == "1.20"
    # размерите на EUR/IND са фиксирани, редове без брой/размер се пропускат
    mixed = customs.compute_ldm([{"type": "eur", "qty": "1", "length": "9999", "width": "9999"},
                                 {"type": "custom", "qty": "2"}, {"type": "eur", "qty": ""}])
    assert str(mixed["ldm"]) == "0.40" and mixed["lines"] == 1
    over = customs.compute_ldm([{"type": "eur", "qty": "35"}])
    assert over["over_capacity"]
    assert customs.compute_ldm([{"type": "eur", "qty": "33"}], factor=2000)["chargeable_weight"] == 26400
    assert customs.compute_ldm([{"type": "eur", "qty": "33"}], factor="abc")["factor"] == 1850


def test_packing_rows_become_ldm_lines():
    import customs
    items = [{"length": "1200", "width": "800", "qty": "40"},
             {"length": "1200", "width": "800", "qty": "15"}, {"description": "без размери"}]
    one_each = customs.compute_ldm(customs.packing_ldm_lines(items))
    assert str(one_each["ldm"]) == "0.80"
    per_qty = customs.compute_ldm(customs.packing_ldm_lines(items[:1], per_qty=True, stack=2))
    assert str(per_qty["ldm"]) == "8.00"


def test_parse_ldm_lines_is_tolerant():
    import customs
    assert customs.parse_ldm_lines("развалено") == []
    assert customs.parse_ldm_lines('{"a": 1}') == []
    assert customs.parse_ldm_lines('[{"type": "eur"}, 5]') == [{"type": "eur"}]


# ---------------------------------------------------------------- миграция / материали

def test_migration_adds_cn_code_to_materials(db_module):
    con = db_module.get_db()
    cols = [r["name"] for r in con.execute("PRAGMA table_info(materials)")]
    con.close()
    assert "cn_code" in cols


def test_materials_cn_code_saved_normalised_and_returned_by_lookup(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/materials/save", {
        "original_code": "", "code": "MAT-1", "description": "Шкаф", "net_weight": "12",
        "cn_code": "8538 90 99"}, csrf_source_url="/materials", follow_redirects=True)
    assert resp.status_code == 200
    con = db_module.get_db()
    assert con.execute("SELECT cn_code FROM materials WHERE code='MAT-1'").fetchone()[0] == "85389099"
    con.close()
    data = admin_client.get("/materials/lookup?code=MAT-1").get_json()
    assert data["cn_code"] == "85389099"
    assert admin_client.get("/materials/lookup?q=MAT").get_json()["materials"][0]["cn_code"] == "85389099"
    page = admin_client.get("/materials").data.decode("utf-8")
    assert "Taric=85389099" in page
    # редакция без поле cn_code (стара форма) не трие кода
    post_with_csrf(admin_client, "/materials/save", {
        "original_code": "MAT-1", "code": "MAT-1", "description": "Шкаф 2", "net_weight": "12"},
        csrf_source_url="/materials")
    con = db_module.get_db()
    assert con.execute("SELECT cn_code FROM materials WHERE code='MAT-1'").fetchone()[0] == "85389099"
    con.close()


def test_materials_invalid_cn_code_is_rejected(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/materials/save", {
        "original_code": "", "code": "MAT-2", "description": "", "net_weight": "",
        "cn_code": "8538-9"}, csrf_source_url="/materials")
    body = resp.data.decode("utf-8")
    assert "8 или 10 цифри" in body
    assert 'value="8538-9"' in body  # въведеното остава
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM materials WHERE code='MAT-2'").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- формите

def test_cmr_form_has_ldm_calculator_and_customs_fields(admin_client):
    body = admin_client.get("/cmr/new").data.decode("utf-8")
    assert "data-ldm-calc" in body and 'name="ldm_lines"' in body
    for name in ("ldm", "pallet_places", "chargeable_weight", "ldm_factor", "mrn",
                 "no_declaration_ref", "sender_eori", "consignee_eori"):
        assert 'name="%s"' % name in body, name
    for preset in ("1750", "1850", "2000"):
        assert '<option value="%s"' % preset in body
    assert re.search(r'<option value="1850" selected>', body)
    assert "static/customs.js" in body


def test_packing_and_invoice_forms_have_customs_fields(admin_client):
    packing = admin_client.get("/packing/new").data.decode("utf-8")
    assert 'data-mode="rows"' in packing and 'data-rows-table="packing-items"' in packing
    assert 'name="ldm_stack"' in packing and 'name="mrn"' in packing
    for url in ("/invoice-no/new", "/invoice-br/new", "/invoice-dubai/new"):
        body = admin_client.get(url).data.decode("utf-8")
        assert 'name="mrn"' in body and 'name="sender_eori"' in body, url


def test_cmr_with_customs_data_prints_mrn_in_box_13_and_keeps_ldm(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/cmr/new", with_required("/cmr/new", {
        "consignee_country": "Norway", "weight": "5000", "ldm": "13,20", "pallet_places": "33",
        "chargeable_weight": "24420", "ldm_lines": '[{"type":"eur","qty":"33","stack":"1"}]',
        "mrn": "26BG002004001234E5", "no_declaration_ref": "NO-REF-77",
        "sender_eori": "BG123456789", "sender_instructions": "Пазете от влага"}),
        csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302
    doc_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    con = db_module.get_db()
    data = json.loads(con.execute("SELECT data FROM documents WHERE id=?", (doc_id,)).fetchone()[0])
    con.close()
    assert data["ldm"] == "13,20" and data["chargeable_weight"] == "24420"
    assert json.loads(data["ldm_lines"])[0]["qty"] == "33"
    body = admin_client.get("/doc/%d" % doc_id).data.decode("utf-8")
    box13 = body.split("Указания на изпращача<br>Sender's instructions")[1].split("</div>\n      </div>")[0]
    assert "Пазете от влага" in box13 and "MRN: 26BG002004001234E5" in box13
    assert "NO ref.: NO-REF-77" in box13 and "EORI: BG123456789" in box13
    # LDM не се печата на бланката (оформлението на ЧМР е непроменено)
    assert "13,20" not in body.split('id="customs-panel"')[0]


def test_cmr_without_customs_data_has_unchanged_box_13(admin_client, db_module):
    doc_id = _doc(db_module, "cmr", {"sender_name": "А", "consignee_name": "Б",
                                     "sender_instructions": "Само текст"})
    body = admin_client.get("/doc/%d" % doc_id).data.decode("utf-8")
    assert '<div class="val">Само текст</div>' in body
    sheet = body.split('class="print-page')[1].split('id="customs-panel"')[0]
    assert "MRN" not in sheet and "EORI" not in sheet


def test_copy_as_new_drops_mrn_and_norwegian_reference(admin_client, db_module):
    doc_id = _doc(db_module, "cmr", {"consignee_name": "Б", "mrn": "26BG002004001234E5",
                                     "no_declaration_ref": "X1", "sender_eori": "BG1"})
    resp = admin_client.get("/doc/%d/copy" % doc_id)
    assert resp.status_code == 302
    form = admin_client.get(resp.headers["Location"]).data.decode("utf-8")
    m = re.search(r"data-edit='([^']*)'", form)
    edit = json.loads(m.group(1).replace("&#34;", '"').replace("&#39;", "'").replace("&amp;", "&"))
    assert "mrn" not in edit and "no_declaration_ref" not in edit
    assert edit["sender_eori"] == "BG1"


def test_invoice_print_shows_eori_and_mrn_only_when_set(admin_client, db_module):
    plain = _doc(db_module, "invoice_br", {"consignee_name": "WEG", "items": []})
    body = admin_client.get("/doc/%d" % plain).data.decode("utf-8")
    sheet = body.split('class="print-page"')[1].split('id="customs-panel"')[0]
    assert "EORI" not in sheet and "MRN" not in sheet
    assert "Total weight" not in body
    full = _doc(db_module, "invoice_no", {"consignee_name": "ABB AS", "mrn": "26BG002004001234E5",
                                          "sender_eori": "BG123", "consignee_eori": "NO999",
                                          "items": []})
    body = admin_client.get("/doc/%d" % full).data.decode("utf-8")
    assert "MRN: <b>26BG002004001234E5</b>" in body
    assert "EORI: <b>BG123</b>" in body and "EORI: <b>NO999</b>" in body


# ---------------------------------------------------------------- настройки

def test_customs_settings_admin_only(employee_client):
    resp = employee_client.get("/customs/settings")
    assert resp.status_code in (302, 403)
    body = employee_client.get("/").data.decode("utf-8")
    assert "/customs/settings" not in body and "/customs/ldm" in body
    ldm = employee_client.get("/customs/ldm").data.decode("utf-8")
    assert 'href="/customs/intrastat"' in ldm and "/customs/settings" not in ldm


def test_customs_settings_save_validate_and_audit(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/customs/settings", {
        "customs_ldm_factor": "abc", "customs_sender_eori": "bg 123456789",
        "customs_dualuse_annex_versions": "X", "customs_dualuse_control_codes": "",
        "customs_intrastat_nature": "11", "customs_intrastat_transport": "3",
        "customs_intrastat_origin": "BG"}, csrf_source_url="/customs/settings")
    assert "Коефициентът кг/LDM" in resp.data.decode("utf-8")
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'customs_%'").fetchone()[0] == 0
    con.close()
    resp = post_with_csrf(admin_client, "/customs/settings", {
        "customs_ldm_factor": "2000", "customs_sender_eori": "bg 123456789",
        "customs_dualuse_annex_versions": "Делегиран регламент (ЕС) 2025/2003\nДелегиран регламент (ЕС) 2026/XXX",
        "customs_dualuse_control_codes": "3A001 — Електронни компоненти\n5A002: Криптография",
        "customs_intrastat_nature": "11", "customs_intrastat_transport": "3",
        "customs_intrastat_origin": "Германия"}, csrf_source_url="/customs/settings",
        follow_redirects=True)
    assert "Митническите настройки са запазени." in resp.data.decode("utf-8")
    assert 'aria-current="page" href="/customs/settings"' in resp.data.decode("utf-8")
    con = db_module.get_db()
    s = db_module.get_settings(con)
    audit = con.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 5").fetchall()
    con.close()
    assert s["customs_ldm_factor"] == "2000" and s["customs_sender_eori"] == "BG123456789"
    assert s["customs_intrastat_origin"] == "DE"
    assert any("митнически настройки" in r[0] for r in audit)
    cmr = admin_client.get("/cmr/new").data.decode("utf-8")
    assert re.search(r'<option value="2000" selected>', cmr)
    assert 'value="BG123456789"' in cmr  # EORI на износителя по подразбиране
    bad = post_with_csrf(admin_client, "/customs/settings", {
        "customs_dualuse_control_codes": "не е код"}, csrf_source_url="/customs/settings")
    assert "Невалидни контролни позиции" in bad.data.decode("utf-8")


# ---------------------------------------------------------------- двойна употреба

def test_dualuse_annex_version_and_control_codes(admin_client, db_module):
    con = db_module.get_db()
    db_module.save_settings(con, {
        "customs_dualuse_annex_versions": "Делегиран регламент (ЕС) 2025/2003\nДелегиран регламент (ЕС) 2026/1",
        "customs_dualuse_control_codes": "3A001 — Електронни компоненти\n5A002 — Криптография"})
    con.commit()
    con.close()
    form = admin_client.get("/dualuse/new").data.decode("utf-8")
    assert 'value="Делегиран регламент (ЕС) 2025/2003"' in form
    assert '<option value="Делегиран регламент (ЕС) 2026/1">' in form
    assert 'data-cc-code="3A001"' in form and 'data-cc-code="5A002"' in form
    resp = post_with_csrf(admin_client, "/dualuse/new", with_required("/dualuse/new", {
        "annex_version": "Делегиран регламент (ЕС) 2025/2003", "control_codes": "3A001, 5A002"}),
        csrf_source_url="/dualuse/new", follow_redirects=True)
    body = resp.data.decode("utf-8")
    assert "Регламент (ЕС) 2021/821 (Приложение I в редакцията на Делегиран регламент (ЕС) 2025/2003)" in body
    assert "Сравнени позиции от Приложение I: 3A001, 5A002." in body


def test_dualuse_old_document_prints_as_before(admin_client, db_module):
    doc_id = _doc(db_module, "dualuse", {"invoice_numbers": "1", "destination_country": "Турция"})
    body = admin_client.get("/doc/%d" % doc_id).data.decode("utf-8")
    assert "двойна употреба на Регламент (ЕС) 2021/821.</div>" in body
    assert "Приложение I в редакцията" not in body and "Сравнени позиции" not in body


# ---------------------------------------------------------------- Норвегия

def _norway_shipment(db_module, with_mrn=True):
    inv = _doc(db_module, "invoice_no", {
        "consignee_name": "ABB AS", "consignee_address": "Oslo\nNorway", "doc_date": "2026-11-06",
        "items": [{"hs_code": "85389099", "qty": "2", "unit_price": "10"},
                  {"hs_code": "8538", "qty": "1", "unit_price": "5"}]})
    con = db_module.get_db()
    inv_no = con.execute("SELECT number FROM documents WHERE id=?", (inv,)).fetchone()[0]
    con.close()
    pk = _doc(db_module, "packing", {"receiver_name": "ABB AS", "receiver_country": "Norway",
                                     "invoice_no": inv_no, "total_net": "900", "total_gross": "1000",
                                     "total_packages": "3", "hs_code": "85389099"})
    con = db_module.get_db()
    pk_no = con.execute("SELECT number FROM documents WHERE id=?", (pk,)).fetchone()[0]
    con.close()
    cmr = _doc(db_module, "cmr", {
        "consignee_name": "ABB AS", "consignee_country": "Norway",
        "attached_docs": "Invoice %s, Packing list %s" % (inv_no, pk_no), "weight": "1000",
        "packages": "3", "sender_eori": "BG123456789",
        "mrn": "26BG002004001234E5" if with_mrn else "", "no_declaration_ref": "NO-1"})
    return inv, pk, cmr


def test_norway_checklist_marks_missing_items_red(admin_client, db_module):
    _inv, _pk, cmr = _norway_shipment(db_module, with_mrn=False)
    body = admin_client.get("/doc/%d" % cmr).data.decode("utf-8")
    panel = body.split('id="customs-panel"')[1]
    assert "Норвегия (Digitoll): липсват 2 от задължителните данни" in panel
    assert re.search(r'<li class="bad">.*?MRN на износната декларация', panel, re.S)
    assert re.search(r'<li class="bad">.*?CN кодове', panel, re.S)  # ред 2 с „8538“
    assert re.search(r'<li class="ok">.*?Фактура издадена', panel, re.S)
    assert re.search(r'<li class="ok">.*?Опаковъчен лист издаден', panel, re.S)
    assert re.search(r'<li class="ok">.*?Бруто тегло', panel, re.S)
    page = admin_client.get("/doc/%d/customs-check" % cmr).data.decode("utf-8")
    assert "Pre-loading check — Norway" in page
    assert re.search(r'<tr class="bad">\s*<td class="mark">✗</td>\s*<td>MRN', page)
    assert "data-print" in page


def test_norway_checklist_all_green(admin_client, db_module):
    inv, _pk, cmr = _norway_shipment(db_module)
    con = db_module.get_db()
    data = json.loads(con.execute("SELECT data FROM documents WHERE id=?", (inv,)).fetchone()[0])
    data["items"][1]["hs_code"] = "8538 90 99 00"
    con.execute("UPDATE documents SET data=? WHERE id=?", (json.dumps(data), inv))
    con.commit()
    con.close()
    body = admin_client.get("/doc/%d" % cmr).data.decode("utf-8")
    assert "Норвегия (Digitoll): всичко е готово за товарене." in body
    # и от фактурата (без MRN в нея) — данните идват от свързаното ЧМР
    inv_body = admin_client.get("/doc/%d" % inv).data.decode("utf-8")
    assert "всичко е готово за товарене" in inv_body


def test_non_norway_document_has_no_digitoll_block(admin_client, db_module):
    doc_id = _doc(db_module, "cmr", {"consignee_name": "Б", "consignee_country": "Турция",
                                     "stat_no": "8538.90.99"})
    body = admin_client.get("/doc/%d" % doc_id).data.decode("utf-8")
    assert 'id="customs-panel"' in body and "Digitoll" not in body
    assert "Taric=85389099" in body  # линк към TARIC за кл. 10
    page = admin_client.get("/doc/%d/customs-check" % doc_id).data.decode("utf-8")
    assert "Pre-loading check — export" in page and "Digitoll" not in page


def test_panel_not_on_preview_or_other_types(admin_client, db_module):
    pal = _doc(db_module, "pallet", {"client_name": "Б"})
    assert 'id="customs-panel"' not in admin_client.get("/doc/%d" % pal).data.decode("utf-8")
    assert admin_client.get("/doc/%d/customs-check" % pal).status_code == 404


# ---------------------------------------------------------------- eFTI

def _validate(obj, schema, path="$"):
    """Минимален валидатор за подмножеството на JSON Schema в EFTI_JSON_SCHEMA."""
    types = schema.get("type")
    if types:
        names = types if isinstance(types, list) else [types]
        py = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
              "integer": int, "number": (int, float)}
        ok = any(isinstance(obj, py[n]) and not (n in ("integer", "number") and isinstance(obj, bool))
                 for n in names)
        assert ok, "%s: %r не е %s" % (path, obj, names)
    for key in schema.get("required", ()):
        assert key in obj, "%s: липсва %s" % (path, key)
    for key, sub in schema.get("properties", {}).items():
        if isinstance(obj, dict) and key in obj:
            _validate(obj[key], sub, path + "." + key)
    if isinstance(obj, list):
        assert len(obj) >= schema.get("minItems", 0), path
        for i, item in enumerate(obj):
            if "items" in schema:
                _validate(item, schema["items"], "%s[%d]" % (path, i))


def _cmr_full(db_module):
    return _doc(db_module, "cmr", {
        "sender_name": "PH Logistics", "sender_address": "ул. Х 1", "sender_city": "5300 Габрово",
        "sender_country": "България", "sender_eik": "BG111", "consignee_name": "ABB AS",
        "consignee_address": "Ostre Aker vei 24", "consignee_city": "Oslo",
        "consignee_country": "Norway", "consignee_vat": "NO987", "place_delivery": "Oslo, Norway",
        "place_loading": "Габрово, България", "date_loading": "2026-11-06",
        "attached_docs": "Invoice 12955; Packing list 0007/2026", "marks": "PL-1",
        "packages": "3", "packing": "Палети", "goods": "HV Switchgear", "stat_no": "8538 90 99",
        "weight": "1250,5", "volume": "4.2", "sender_instructions": "Keep dry",
        "payment_instructions": "Франко / Carriage paid", "cod": "", "special_agreements": "—",
        "carrier": "Trans OOD, Sofia, BG", "truck_reg": "EB 1234 AB", "trailer_reg": "EB 5678 EE",
        "driver": "Иван", "reservations": "", "established_place": "Габрово",
        "established_date": "2026-11-06", "mrn": "26bg002004001234e5", "sender_eori": "BG123456789",
        "ldm": "1,20", "pallet_places": "3", "chargeable_weight": "2220"})


def test_efti_json_export_matches_schema_and_mapping(admin_client, db_module):
    import customs
    doc_id = _cmr_full(db_module)
    resp = admin_client.get("/doc/%d/efti.json" % doc_id)
    assert resp.status_code == 200 and resp.mimetype == "application/json"
    assert "attachment" in resp.headers["Content-Disposition"]
    data = json.loads(resp.data.decode("utf-8"))
    _validate(data, customs.EFTI_JSON_SCHEMA)
    assert data["formatVersion"] == customs.EFTI_EXPORT_VERSION
    assert data["transportDocument"]["type"] == "CMR"
    assert data["consignor"]["name"] == "PH Logistics"
    assert data["consignor"]["postalAddress"]["countryCode"] == "BG"
    assert data["consignor"]["eoriNumber"] == "BG123456789"
    assert data["consignee"]["postalAddress"]["countryCode"] == "NO"
    assert data["takingOver"] == {"place": "Габрово, България", "date": "2026-11-06"}
    assert data["delivery"]["place"] == "Oslo, Norway" and data["delivery"]["countryCode"] == "NO"
    goods = data["goodsItems"][0]
    assert goods["grossMassKg"] == 1250.5 and goods["packageCount"] == 3
    assert goods["commodityCode"] == "85389099" and goods["commodityCodeValid"] is True
    assert data["dangerousGoods"] == {"present": False}
    assert data["documentsAttached"] == ["Invoice 12955", "Packing list 0007/2026"]
    assert data["transportMeans"] == [{"type": "road_tractor", "registration": "EB 1234 AB"},
                                      {"type": "trailer", "registration": "EB 5678 EE"}]
    assert data["customs"]["mrn"] == "26BG002004001234E5"
    assert data["loading"]["loadingMetres"] == 1.2 and data["loading"]["chargeableWeightKg"] == 2220
    assert "qr" not in resp.data.decode("utf-8").lower()
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'износ на ЧМР (eFTI%'"
                       ).fetchone()[0] == 1
    con.close()


def test_efti_export_of_empty_cmr_still_validates(admin_client, db_module):
    import customs
    doc_id = _doc(db_module, "cmr", {"consignee_name": "Б"})
    data = admin_client.get("/doc/%d/efti.json" % doc_id).get_json()
    _validate(data, customs.EFTI_JSON_SCHEMA)
    assert data["goodsItems"][0]["grossMassKg"] is None and data["transportMeans"] == []


def test_efti_xml_export_is_well_formed(admin_client, db_module):
    doc_id = _cmr_full(db_module)
    resp = admin_client.get("/doc/%d/efti.xml" % doc_id)
    assert resp.status_code == 200 and resp.mimetype == "application/xml"
    root = ET.fromstring(resp.data)
    ns = {"e": "urn:ph-logistics:efti-cmr-export:1"}
    assert root.tag.endswith("eFTIConsignmentExport") and root.get("version") == "1.0"
    assert root.find("e:Consignor/e:Name", ns).text == "PH Logistics"
    assert root.find("e:GoodsItems/e:GoodsItem/e:GrossMassKg", ns).text == "1250.5"
    assert root.find("e:DangerousGoods/e:Present", ns).text == "false"
    regs = [el.text for el in root.findall("e:TransportMeans/e:TransportMeans/e:Registration", ns)]
    assert regs == ["EB 1234 AB", "EB 5678 EE"]


def test_efti_only_for_cmr_and_link_on_cmr_view(admin_client, db_module):
    pk = _doc(db_module, "packing", {"receiver_name": "Б"})
    assert admin_client.get("/doc/%d/efti.json" % pk).status_code == 404
    cmr = _doc(db_module, "cmr", {"consignee_name": "Б"})
    assert admin_client.get("/doc/%d/efti.txt" % cmr).status_code == 404
    body = admin_client.get("/doc/%d" % cmr).data.decode("utf-8")
    assert "/doc/%d/efti.json" % cmr in body and "/doc/%d/efti.xml" % cmr in body
    assert "eFTI JSON" in body and "efti.json" not in admin_client.get("/doc/%d" % pk).data.decode()


# ---------------------------------------------------------------- Intrastat

def _month():
    return date.today().strftime("%Y-%m")


def _intrastat_data(db_module):
    today = date.today().isoformat()
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, vat, country) VALUES ('Müller GmbH', 'DE123456789', 'DE')")
    con.commit()
    con.close()
    _doc(db_module, "packing", {"receiver_name": "Müller GmbH", "receiver_country": "Германия",
                                "doc_date": today, "hs_code": "8538 90 99", "total_net": "100,4",
                                "terms_delivery": "FCA",
                                "items": [{"description": "HV Switchgear", "net": "100.4"}]})
    _doc(db_module, "packing", {"receiver_name": "Müller GmbH", "receiver_country": "Germany",
                                "doc_date": today, "hs_code": "85389099", "terms_delivery": "FCA",
                                "items": [{"description": "Шкаф", "net": "50"}, {"net": "49,6"}]})
    _doc(db_module, "packing", {"receiver_name": "ABB AS", "receiver_country": "Norway",
                                "doc_date": today, "hs_code": "85389099", "total_net": "999"})
    _doc(db_module, "packing", {"receiver_name": "Клиент", "receiver_country": "България",
                                "doc_date": today, "hs_code": "85389099", "total_net": "5"})
    _doc(db_module, "packing", {"receiver_name": "Müller GmbH", "receiver_country": "Germany",
                                "doc_date": "2020-01-15", "hs_code": "85389099", "total_net": "7"})


def test_intrastat_lines_eu_only_aggregated(db_module, flask_app):
    import customs
    _intrastat_data(db_module)
    con = db_module.get_db()
    m = customs.parse_month(_month())
    with flask_app.test_request_context():
        lines = customs.intrastat_lines(con, m, ("packing",), aggregate=True)
        detail = customs.intrastat_lines(con, m, ("packing",), aggregate=False)
    con.close()
    assert len(lines) == 1 and len(detail) == 2
    line = lines[0]
    assert line["cn8"] == "85389099" and line["country"] == "DE" and line["vat"] == "DE123456789"
    assert line["nature"] == "11" and line["transport"] == "3" and line["origin"] == "BG"
    assert customs._mass_kg(line["net_kg"]) == 200  # 100,4 + (50 + 49,6)
    assert line["value_eur"] is None and "липсва фактурна стойност" in line["issues"]
    assert len(line["docs"]) == 2


def test_intrastat_value_from_linked_invoice(db_module, flask_app):
    import customs
    today = date.today().isoformat()
    inv = _doc(db_module, "invoice_br", {"consignee_name": "X", "consignee_address": "Brasil",
                                         "doc_date": today, "country_origin": "Turkey",
                                         "items": [{"hs_code": "85389099", "qty": "2",
                                                    "unit_price": "100.50"}]})
    con = db_module.get_db()
    number = con.execute("SELECT number FROM documents WHERE id=?", (inv,)).fetchone()[0]
    con.close()
    _doc(db_module, "packing", {"receiver_name": "Y", "receiver_country": "Italy",
                                "doc_date": today, "hs_code": "85389099", "total_net": "10",
                                "invoice_no": number})
    con = db_module.get_db()
    with flask_app.test_request_context():
        lines = customs.intrastat_lines(con, customs.parse_month(_month()))
    con.close()
    assert len(lines) == 1  # фактурата е за Бразилия — само опаковъчният лист е в ЕС
    assert str(customs._money(lines[0]["value_eur"])) == "201.00"
    assert lines[0]["origin"] == "TR" and lines[0]["country"] == "IT"
    assert "липсва ДДС № на партньора" in lines[0]["issues"]


def test_intrastat_page_and_downloads(admin_client, employee_client, db_module):
    _intrastat_data(db_module)
    page = employee_client.get("/customs/intrastat?month=%s&src=packing" % _month())
    body = page.data.decode("utf-8")
    assert page.status_code == 200 and "85389099" in body and "DE123456789" in body
    assert "Форматът за подаване в НСИ не е проверен" in body
    csv_resp = employee_client.get("/customs/intrastat/export.csv?month=%s&src=packing" % _month())
    assert csv_resp.status_code == 200
    text = csv_resp.data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    assert rows[0][2] == "CN8" and rows[0][10] == "Нето маса, кг"
    assert len(rows) == 2 and rows[1][2] == "85389099" and rows[1][4] == "DE" and rows[1][10] == "200"
    xresp = employee_client.get("/customs/intrastat/export.xlsx?month=%s&src=packing" % _month())
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(xresp.data))
    ws = wb.worksheets[0]
    assert ws.cell(row=1, column=3).value == "CN8" and ws.cell(row=2, column=11).value == 200
    assert "НСИ" in wb["Бележка"]["A1"].value
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'износ Intrastat'").fetchone()[0] == 2
    con.close()
    assert employee_client.get("/customs/intrastat/export.pdf").status_code == 404


def test_intrastat_csv_escapes_formulas():
    import customs
    line = customs._new_line("2026-11", "OPL № 1", "85389099", "=HYPERLINK(\"x\")", "DE", "DE1",
                             "11", "3", "FCA", None, None, "BG")
    line["no"] = 1
    text = customs.intrastat_csv([line]).decode("utf-8-sig")
    assert "'=HYPERLINK" in text


# ---------------------------------------------------------------- статика

def test_customs_js_is_served_and_mirrors_python_constants(client):
    resp = client.get("/static/customs.js")
    assert resp.status_code == 200
    js = resp.data.decode("utf-8")
    assert "TRAILER_LENGTH = 13.6" in js and "TRAILER_WIDTH = 2.4" in js and "TRAILER_EUR = 33" in js
    assert "eur: [1200, 800], ind: [1200, 1000]" in js
    assert "/^\\d{8}(\\d{2})?$/" in js


def test_customs_css_linked_and_served(client):
    login = client.get("/login").data.decode("utf-8")
    assert "static/customs.css" in login and "static/customs.js" in login
    css = client.get("/static/customs.css").data.decode("utf-8")
    assert "input.cn-invalid" in css and ".customs-check-table" in css
