# -*- coding: utf-8 -*-
"""Одит (04.10.2026, група WORKFLOW): бързи пътища за оператора — постоянен
профил на настолния прозорец (Б1), „Опаковъчен лист / Фактура от тези карти“
(Б2), диапазон/списък в „Добави от палета“ (Б3), езикът на документа (Б6),
нето от справочника (Д3), еднократен токен на груповото издаване (F6),
сканиране с Caps Lock/фонетична подредба (F8) и дребните UX-2…UX-6.
E2E частта (браузър) е в tests/test_e2e_workflow.py."""
import io
import json
import re
import sys
import types
from datetime import date

from openpyxl import Workbook

from conftest import post_with_csrf, read_source

YEAR = date.today().year


# ---------------------------------------------------------------- помощни

def _issue_pallets(client, cards, doc_lang="en", issue_token=None, client_name="Клиент"):
    """Издава партида палетни карти през /pallet/bulk-issue (както ги праща
    прегледът след импорт). cards: списък от списъци с редове (orders)."""
    form = {"client_name": client_name, "doc_date": date.today().isoformat(),
            "groups": ",".join(str(i) for i in range(1, len(cards) + 1)),
            "doc_lang": doc_lang}
    if issue_token is not None:
        form["issue_token"] = issue_token
    for i, items in enumerate(cards, start=1):
        form["items_json_%d" % i] = json.dumps(items, ensure_ascii=False)
        form["items_format_%d" % i] = "orders"
        form["gross_%d" % i] = "100"
        form["pallet_type_%d" % i] = "120×80"
        form["height_%d" % i] = "150"
    return post_with_csrf(client, "/pallet/bulk-issue", form, csrf_source_url="/pallet/new",
                          follow_redirects=False)


def _rows(*specs):
    return [{"order_no": "4700", "pos": str(10 * n), "reference": ref, "reference_desc": desc,
             "qty": qty} for n, (ref, desc, qty) in enumerate(specs, start=1)]


def _pallet_docs(db_module):
    con = db_module.get_db()
    try:
        return con.execute("SELECT id, number, barcode, data FROM documents"
                           " WHERE doc_type = 'pallet' ORDER BY id").fetchall()
    finally:
        con.close()


def _add_material(db_module, code, net):
    con = db_module.get_db()
    con.execute("INSERT INTO materials (code, description, net_weight) VALUES (?, ?, ?)",
                (code, "Material " + code, net))
    con.commit()
    con.close()


def _pull(client, code, url="/packing/pull-pallet", **extra):
    data = {"code": code}
    data.update(extra)
    resp = post_with_csrf(client, url, data, csrf_source_url="/packing/new")
    return resp.get_json()


# ---------------------------------------------------------------- Б1/Д5

def test_webview_start_kwargs_disable_private_mode_and_persist_storage(tmp_path, monkeypatch):
    """pywebview 6.x е с private_mode=True по подразбиране — бисквитките и
    автодовършването се губеха при всяко затваряне на прозореца."""
    import desktop
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def start(func=None, args=None, gui=None, debug=False, private_mode=True, storage_path=None):
        pass

    kwargs = desktop.webview_start_kwargs(start)
    assert kwargs["private_mode"] is False
    # Одит (06.10.2026): новото име на потребителската папка.
    assert kwargs["storage_path"] == str(tmp_path / "PHLogistics" / "webview")
    assert (tmp_path / "PHLogistics" / "webview").is_dir()


def test_webview_start_kwargs_old_pywebview_without_the_parameters():
    """По-стар pywebview без тези параметри не бива да гърми с TypeError."""
    import desktop

    def start(func=None, args=None, gui=None, debug=False):
        pass

    assert desktop.webview_start_kwargs(start) == {}


def test_run_native_window_passes_persistent_profile_to_webview(tmp_path, monkeypatch):
    import desktop
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(desktop, "wait_for_server", lambda url, timeout=15: True)
    calls = {}
    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **k: calls.setdefault("window", (a, k))

    def start(func=None, private_mode=True, storage_path=None, debug=False):
        calls["start"] = {"private_mode": private_mode, "storage_path": storage_path}

    fake.start = start
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert desktop.run_native_window("http://127.0.0.1:1/") is True
    assert calls["start"]["private_mode"] is False
    assert calls["start"]["storage_path"].endswith("webview")


# ---------------------------------------------------------------- F8

def test_code_variants_caps_lock_and_phonetic_layouts():
    import bg_keyboard
    assert "CMR-04102026-0001" in bg_keyboard.code_variants("cmr-04102026-0001")
    assert "CMR-04102026-0001" in bg_keyboard.code_variants("ЦМР-04102026-0001")
    assert "CMR-04102026-0001" in bg_keyboard.code_variants("цмр-04102026-0001")
    # Фонетична по БАН: V е „в“ (традиционната я дава като W).
    assert "TOV-04102026-0001" in bg_keyboard.code_variants("ТОВ-04102026-0001")
    # Буквалният вход винаги е пръв.
    assert bg_keyboard.code_variants("PAL-1")[0] == "PAL-1"
    assert bg_keyboard.code_variants("  ") == []


def test_scan_finds_document_typed_with_caps_lock_or_phonetic_layout(admin_client, db_module):
    from conftest import issue_cmr
    doc_id = issue_cmr(admin_client)
    con = db_module.get_db()
    barcode = con.execute("SELECT barcode FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    con.close()
    phonetic = barcode.replace("CMR", "ЦМР")
    for typed in (barcode.lower(), phonetic, phonetic.lower()):
        resp = post_with_csrf(admin_client, "/scan", {"code": typed}, follow_redirects=False)
        assert resp.headers["Location"].endswith("/doc/%d" % doc_id), typed


def test_pull_pallet_by_lowercase_or_phonetic_barcode(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "5"))])
    barcode = _pallet_docs(db_module)[0]["barcode"]
    for typed in (barcode.lower(), barcode.replace("PAL", "ПАЛ")):
        data = _pull(admin_client, typed)
        assert data["ok"], (typed, data)


# ---------------------------------------------------------------- Б3 / UX-6 / Б6 / Д3

def test_expand_pallet_codes_ranges_lists_and_barcodes():
    from routes_pallet_extra import expand_pallet_codes
    assert expand_pallet_codes("2747-2750") == (["2747", "2748", "2749", "2750"], False)
    assert expand_pallet_codes("1,3, 5") == (["1", "3", "5"], False)
    assert expand_pallet_codes("1 - 3; 2") == (["1", "2", "3"], False)
    assert expand_pallet_codes("PAL-04102026-0003") == (["PAL-04102026-0003"], False)
    assert expand_pallet_codes("1/2025-2/2025") == (["0001/2025", "0002/2025"], False)
    codes, too_many = expand_pallet_codes("1-150, 200-300")
    assert len(codes) == 200 and too_many


def test_pull_pallet_range_returns_one_row_per_card(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "5")), _rows(("A2", "d2", "6")),
                                  _rows(("A3", "d3", "7"))])
    data = _pull(admin_client, "1-3, 2, 99")
    assert data["ok"] and data["multi"]
    assert [r["number"] for r in data["results"]] == ["%04d/%d" % (n, YEAR) for n in (1, 2, 3)]
    assert [r["row"]["qty"] for r in data["results"]] == ["5", "6", "7"]
    assert len(data["errors"]) == 1 and "99" in data["errors"][0]
    # Единичен код — досегашният формат на отговора.
    single = _pull(admin_client, "1")
    assert single["ok"] and "multi" not in single and single["row"]["qty"] == "5"


def test_pull_pallet_row_text_follows_document_language(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "5"), ("A2", "d2", "1"), ("A3", "d3", "1"),
                                        ("A4", "d4", "1"))] * 2, doc_lang="bg")
    assert json.loads(_pallet_docs(db_module)[0]["data"])["pallet_no"] == "1 от 2"
    en = _pull(admin_client, "1", lang="en")["row"]
    assert en["description"] == "Pallet 1 of 2 — d1, d2, d3 and 1 more"
    assert en["packing"] == "Pallet"
    bg = _pull(admin_client, "1", lang="bg")["row"]
    assert bg["description"] == "Палет 1 от 2 — d1, d2, d3 и още 1"
    assert bg["packing"] == "Палет"
    # Без lang — подразбирането на опаковъчния лист (английски).
    assert _pull(admin_client, "1")["row"]["packing"] == "Pallet"


def test_bulk_pallet_number_follows_document_language_not_ui(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "5"))] * 2, doc_lang="en")
    _issue_pallets(admin_client, [_rows(("A1", "d1", "5"))] * 2, doc_lang="bg")
    numbers = [json.loads(r["data"])["pallet_no"] for r in _pallet_docs(db_module)]
    assert numbers == ["1 of 2", "2 of 2", "1 от 2", "2 от 2"]


def test_pull_pallet_unparsable_qty_is_left_empty_with_warning(admin_client, db_module):
    """UX-6: досега неразчитаемото количество ставаше „брой редове“."""
    _issue_pallets(admin_client, [_rows(("A1", "d1", "пет"), ("A2", "d2", "x"))])
    data = _pull(admin_client, "1")
    assert data["row"]["qty"] == ""
    assert "Брой" in data["note"]


def test_pull_pallet_net_weight_suggested_from_materials(admin_client, db_module):
    _add_material(db_module, "A1", "1.5")
    _add_material(db_module, "A2", "0,25")
    _issue_pallets(admin_client, [_rows(("A1", "d1", "4"), ("a2", "d2", "2")),
                                  _rows(("A1", "d1", "4"), ("ZZZ", "d9", "2"))])
    full = _pull(admin_client, "1")
    assert full["row"]["net"] == "6.5"
    assert "справочника материали" in full["note"]
    # Материал без нето тегло → без частичен сбор (би бил грешно число).
    partial = _pull(admin_client, "2")
    assert "net" not in partial["row"]
    assert "попълнете го ръчно" in partial["note"]


# ---------------------------------------------------------------- F6

def test_bulk_issue_token_prevents_second_issue(admin_client, db_module):
    cards = [_rows(("A1", "d1", "5")), _rows(("A2", "d2", "6"))]
    first = _issue_pallets(admin_client, cards, issue_token="tok-AAAAAAAAAAAA")
    assert first.status_code == 302 and "/pallet/bulk-result" in first.headers["Location"]
    again = _issue_pallets(admin_client, cards, issue_token="tok-AAAAAAAAAAAA")
    assert again.headers["Location"] == first.headers["Location"]
    assert len(_pallet_docs(db_module)) == 2
    # Друг токен — нова, легитимна партида.
    _issue_pallets(admin_client, cards, issue_token="tok-BBBBBBBBBBBB")
    assert len(_pallet_docs(db_module)) == 4


def test_bulk_issue_token_released_after_failed_issue(admin_client, db_module, monkeypatch):
    import routes_pallet_extra

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    cards = [_rows(("A1", "d1", "5"))]
    original = routes_pallet_extra.save_document
    monkeypatch.setattr(routes_pallet_extra, "save_document", boom)
    failed = _issue_pallets(admin_client, cards, issue_token="tok-CCCCCCCCCCCC")
    assert "/pallet/bulk-review/restore/" in failed.headers["Location"]
    monkeypatch.setattr(routes_pallet_extra, "save_document", original)
    ok = _issue_pallets(admin_client, cards, issue_token="tok-CCCCCCCCCCCC")
    assert "/pallet/bulk-result" in ok.headers["Location"]
    assert len(_pallet_docs(db_module)) == 1


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_ORDER_ROWS = [["Order No", "Pos", "Reference", "Reference Desc", "Open Qty", "Pallet"],
               ["4700", "10", "a1", "d1", 5, 1],
               ["4700", "20", "A2", "d2", 6, 2]]


def test_bulk_import_review_has_token_and_get_url_that_leads_to_result(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/pallet/bulk-import",
                          {"excel_file": (io.BytesIO(_xlsx(_ORDER_ROWS)), "o.xlsx"),
                           "doc_lang": "en"},
                          csrf_source_url="/pallet/new", content_type="multipart/form-data")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    token = re.search(r'name="issue_token" value="([^"]+)"', body).group(1)
    review_url = re.search(r'history\.replaceState\(history\.state, "", "([^"]+)"\)', body).group(1)
    assert 'name="doc_lang" value="en"' in body
    # GET вариантът показва СЪЩИЯ преглед със същия токен.
    again = admin_client.get(review_url).get_data(as_text=True)
    assert 'name="issue_token" value="%s"' % token in again
    issued = _issue_pallets(admin_client, [_rows(("A1", "d1", "5"))] * 2, issue_token=token)
    # След издаване „Назад“ към прегледа води към резултата, не към нов опит.
    back = admin_client.get(review_url)
    assert back.status_code == 302
    assert back.headers["Location"] == issued.headers["Location"]


# ---------------------------------------------------------------- Б2

def test_bulk_result_offers_packing_list_and_invoices_from_these_cards(admin_client, db_module):
    resp = _issue_pallets(admin_client, [_rows(("A1", "d1", "5")), _rows(("A2", "d2", "6"))],
                          client_name="WEG")
    body = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    numbers = "%04d/%d,%04d/%d" % (1, YEAR, 2, YEAR)
    assert "/packing/new?pull=" + numbers in body
    assert "receiver_name=WEG" in body and "order_no=4700" in body
    for endpoint in ("/invoice-br/new", "/invoice-no/new", "/invoice-dubai/new"):
        assert endpoint + "?pull=" in body


def test_invoice_pull_several_cards_with_pallet_number_in_document_language(admin_client, db_module):
    _issue_pallets(admin_client, [_rows(("A1", "d1", "5")), _rows(("a2", "d2", "6"))],
                   doc_lang="bg")
    data = _pull(admin_client, "1-2", url="/invoice/pull-pallet", lang="en")
    assert data["ok"] and data["count"] == 2
    assert [r["pallet_no"] for r in data["rows"]] == ["1 of 2", "2 of 2"]
    # UX-4: ненамерен код — с главни букви.
    assert data["rows"][1]["material_code"] == "A2"


# ---------------------------------------------------------------- UX-3 / UX-4

def test_invoice_excel_import_fills_pallet_number_and_uppercases_codes(admin_client):
    resp = post_with_csrf(admin_client, "/invoice/import-items",
                          {"excel_file": (io.BytesIO(_xlsx(_ORDER_ROWS)), "o.xlsx")},
                          csrf_source_url="/invoice-no/new", content_type="multipart/form-data")
    data = resp.get_json()
    assert data["ok"]
    assert [r["pallet_no"] for r in data["rows"]] == ["1", "2"]
    assert [r["material_code"] for r in data["rows"]] == ["A1", "A2"]


# ---------------------------------------------------------------- форми

def test_invoice_address_book_options_show_full_company_name(admin_client, db_module):
    con = db_module.get_db()
    con.execute("INSERT INTO invoice_clients (name, delivery_name, billing_name) VALUES (?, ?, ?)",
                ("WEG BR", "WEG Equipamentos Eletricos S.A.", ""))
    con.commit()
    con.close()
    body = admin_client.get("/invoice-br/new").get_data(as_text=True)
    assert "WEG BR — WEG Equipamentos Eletricos S.A.</option>" in body
    assert 'id="copy-billto-to-consignee-btn"' in body


def test_form_templates_quick_wins(admin_client):
    packing = admin_client.get("/packing/new").get_data(as_text=True)
    assert "PAL-2026-0003" not in packing and "PAL-ДДММГГГГ-0003" in packing
    assert 'data-doc-lang="en"' in packing
    assert 'data-doc-lang="bg"' in admin_client.get("/packing/new?sender_lang=bg").get_data(as_text=True)
    dualuse = admin_client.get("/dualuse/new").get_data(as_text=True)
    assert re.search(r'name="destination_country"[^>]*required', dualuse)
    assert re.search(r'name="declarant_name"[^>]*required', dualuse)
    cmr = admin_client.get("/cmr/new").get_data(as_text=True)
    assert re.search(r'name="packages"[^>]*data-nonneg', cmr)
    waybill = admin_client.get("/waybill/new").get_data(as_text=True)
    assert re.search(r'name="extra_costs"[^>]*data-nonneg', waybill)
    review = read_source("templates", "pallet_bulk_review.html")
    assert 'id="bulk-fill-all"' in review


def test_bulk_preview_title_and_bar_are_translatable():
    src = read_source("templates", "pallet_bulk_preview.html")
    assert "{{ _('Предварителен преглед — палетни карти') }}" in src
    assert "_('Предварителен преглед на %(count)s палетни карти — нищо не е запазено'" in src
