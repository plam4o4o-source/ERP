# -*- coding: utf-8 -*-
"""Одит (04.10.2026) — печатни бланки (група PRINT): P3–P27, F9.

Бързи проверки без браузър: печатните изгледи се рендерират през
admin_client и се проверява HTML-ът (и правилата в style.css, на които
стъпва поправката). Измерванията в истински печатен движок са в
tests/test_e2e_print_layout.py (`-m e2e`).

Документите се записват направо в базата (не през формите): тук се
проверява БЛАНКАТА, а проверките на формите (задължителни полета и т.н.) не
са предмет на тези тестове.
"""
import json
import re
import secrets

import pytest

from conftest import read_source

FILL = "……………………"


def _doc(db_module, doc_type, data):
    """Записва документ направо в базата и връща id-то му."""
    import appcore

    con = db_module.get_db()
    try:
        number, year, seq, barcode = db_module.next_number(con, doc_type)
        data = dict(data, number=number, barcode=barcode)
        cur = con.execute(
            "INSERT INTO documents (doc_type, number, year, seq, barcode, public_token,"
            " public_token_expires_at, data, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (doc_type, number, year, seq, barcode, secrets.token_hex(16),
             appcore.public_token_expiry(), json.dumps(data, ensure_ascii=False)))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _view(admin_client, db_module, doc_type, data, query=""):
    doc_id = _doc(db_module, doc_type, data)
    resp = admin_client.get("/doc/%d%s" % (doc_id, query))
    assert resp.status_code == 200
    return resp.data.decode("utf-8")


def _css():
    return read_source("static", "style.css")


def _thead(body):
    m = re.search(r'<table class="goods">\s*(?:\{#.*?#\}\s*)?<thead>(.*?)</thead>', body, re.S)
    assert m, "заглавният ред на таблицата не е намерен"
    return m.group(1)


def _totals_row(body):
    m = re.search(r'<tr class="totals">(.*?)</tr>', body, re.S)
    assert m, "редът ОБЩО/TOTAL не е намерен"
    return m.group(1)


LONG_CODE = "MATERIALCODE-ÄÖÜß-ĞŞİÇ-" + "X" * 40


def _inv_items(n=3):
    return [dict(hs_code="7326909890", po_no="4500%06d" % i, pos=str(i * 10), net_weight="1.5",
                 material_code=LONG_CODE if i == 2 else "MAT-%05d" % i,
                 description="Стоманен детайл %d" % i, pallet_no="1", qty=str(i * 3),
                 unit_price="12.5") for i in range(1, n + 1)]


INVOICES = [("invoice_br", "Material code"), ("invoice_no", "Material Description"),
            ("invoice_dubai", "Material code")]


# ---------------------------------------------------------------- P3: фактури
def test_invoice_tables_use_fixed_layout_and_wrap_long_codes():
    """P3: дълъг код без интервали разширяваше таблицата — „Total Price“
    излизаше извън листа. Фиксирана подредба + пренасяне в клетката."""
    css = _css()
    assert ".inv table.goods { table-layout: fixed; }" in css
    assert re.search(r"\.inv table\.goods th, \.inv table\.goods td \{[^}]*overflow-wrap: anywhere",
                     css)


@pytest.mark.parametrize("doc_type,flex_col", INVOICES)
def test_invoice_has_exactly_one_flexible_column(admin_client, db_module, doc_type, flex_col):
    """P3: при table-layout: fixed ширините идват от заглавния ред; само ЕДНА
    колона е без ширина и поема остатъка (описанието при Норвегия, кодът на
    материала при Бразилия/Дубай) — иначе описанието се свиваше до една дума
    на ред. Сборът на фиксираните ширини оставя на гъвкавата колона поне
    ~170px от 733px полезна ширина на A4."""
    body = _view(admin_client, db_module, doc_type,
                 {"invoice_number": "X-1", "doc_date": "2026-10-04", "items": _inv_items()})
    ths = re.findall(r"<th([^>]*)>(.*?)</th>", _thead(body), re.S)
    flexible = [text for attrs, text in ths if "width:" not in attrs]
    assert flexible == [flex_col], flexible
    fixed = sum(int(w) for w in re.findall(r"width:(\d+)px", _thead(body)))
    assert 733 - fixed >= 170, "за гъвкавата колона остават само %dpx" % (733 - fixed)


def test_brazil_invoice_still_has_no_total_weight(admin_client, db_module):
    """Изрична заявка: фактурата за Бразилия е БЕЗ „Total weight“."""
    body = _view(admin_client, db_module, "invoice_br",
                 {"invoice_number": "BR-1", "items": _inv_items()})
    assert "Total weight" not in body


# ---------------------------------------------------------------- P20/P19/P22/P24: фактури
@pytest.mark.parametrize("doc_type,flex_col", INVOICES)
def test_invoice_total_quantity_is_right_aligned_and_numeric_headers_too(
        admin_client, db_module, doc_type, flex_col):
    """P20: общото количество беше центрирано (class="c"), а количествата в
    редовете — вдясно. P19: заглавията на числовите колони — вдясно."""
    body = _view(admin_client, db_module, doc_type,
                 {"invoice_number": "X-2", "items": _inv_items()})
    totals = _totals_row(body)
    assert '<td class="n">18</td>' in totals, totals
    assert 'class="c"' not in totals
    head = _thead(body)
    for label in ("Quantity", "Unit Price", "Total Price"):
        assert re.search(r'<th class="n"[^>]*>%s' % label, head), label


def test_numeric_header_rule_beats_the_per_document_th_rules():
    """P19: `.print-page table th.n` (0,2,2) губеше от по-късните
    `.inv table.goods th` / `.pkl table.goods th` (също 0,2,2) — заглавията
    оставаха вляво. Правилото с `tr >` е (0,2,3) и е след тях без значение."""
    css = _css()
    assert ".print-page table tr > th.n { text-align: right; }" in css


def test_norway_invoice_header_typo_fixed(admin_client, db_module):
    """P22: „Material Decription“ на печатната бланка."""
    body = _view(admin_client, db_module, "invoice_no", {"invoice_number": "NO-1", "items": _inv_items()})
    assert "Material Description" in body
    assert "Decription" not in body


@pytest.mark.parametrize("doc_type", ["invoice_br", "invoice_no", "invoice_dubai"])
def test_invoice_without_date_prints_a_dash(admin_client, db_module, doc_type):
    """P24: празно „Date:“ оставаше голо, а останалите полета показват „—“."""
    body = _view(admin_client, db_module, doc_type, {"invoice_number": "X-3", "items": []})
    assert "Date: <b>—</b>" in body


# ---------------------------------------------------------------- P10/F9/P11: опаковъчен лист
PK_ITEMS = [dict(packing="Pallet", description="Steel %d" % i, qty=str(i * 10), length="1200",
                 width="800", height=str(900 + i), volume="0.86%d" % i, net="%d,5" % (300 + i),
                 gross="%d.25" % (320 + i)) for i in range(1, 4)]


def test_packing_total_row_prints_the_total_quantity(admin_client, db_module):
    """P10: „ОБЩО / TOTAL“ беше colspan=4 върху колоната „Брой“ — общото
    количество изобщо не се печаташе."""
    body = _view(admin_client, db_module, "packing", {"items": PK_ITEMS})
    totals = _totals_row(body)
    assert 'colspan="4"' not in totals
    cells = re.findall(r"<td[^>]*>(.*?)</td>", totals, re.S)
    assert cells[0].startswith("ОБЩО / TOTAL")
    assert cells[1].strip() == "60", cells  # 10 + 20 + 30


def test_packing_empty_totals_print_the_sum_of_the_rows(admin_client, db_module):
    """F9: непопълнени общи суми (операторът не е натиснал подсказките) —
    на бланката излиза сборът на редовете вместо празна клетка; запетаята
    като десетичен знак се чете („301,5“)."""
    body = _view(admin_client, db_module, "packing", {"items": PK_ITEMS})
    cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", _totals_row(body), re.S)]
    assert cells[-3:] == ["2.586", "907.5", "966.75"], cells


def test_packing_entered_totals_win_over_the_sum(admin_client, db_module):
    """F9: въведената обща сума (напр. с тара на палета) остава — сборът е
    само заместител на ПРАЗНО поле."""
    body = _view(admin_client, db_module, "packing",
                 {"items": PK_ITEMS, "total_volume": "3", "total_net": "1000", "total_gross": "1100,5"})
    cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", _totals_row(body), re.S)]
    assert cells[-3:] == ["3", "1000", "1100.5"], cells


def test_packing_dimensions_are_one_column(admin_client, db_module):
    """P11: Д/Ш/В бяха три тесни колони и оставяха на описанието ~100px
    (6–8 реда на артикул). Сега е една колона „Размери“, описанието е
    единствената гъвкава колона, таблицата е с фиксирана подредба."""
    body = _view(admin_client, db_module, "packing", {"items": PK_ITEMS})
    head = _thead(body)
    assert "Дължина" not in head and "Широчина" not in head and "Височина" not in head
    assert "Размери" in head
    ths = re.findall(r"<th([^>]*)>(.*?)</th>", head, re.S)
    assert [t for a, t in ths if "width:" not in a] == ["Описание на материала / Material Description"]
    assert "1200×<wbr>800×<wbr>901" in body
    assert 'colspan="8"' in body  # идентификационният ред покрива всички колони
    assert ".pkl table.goods { table-layout: fixed; }" in _css()


def test_packing_row_without_dimensions_leaves_the_cell_empty(admin_client, db_module):
    body = _view(admin_client, db_module, "packing", {"items": [dict(description="x", qty="1")]})
    assert "×" not in body.split("<tbody>")[1].split("</tbody>")[0]


# ---------------------------------------------------------------- P12/P24: палетна карта
PL = dict(doc_date="2026-10-04", client_name="Müller Logistik GmbH", client_address="Hauptstraße 5",
          client_city="München", client_country="Germany", pallet_no="3/12", packaging_type="EUR",
          pallet_type="120×80", height="145", gross="456.7")


def test_pallet_label_has_a_tall_full_width_barcode_and_big_key_values(admin_client, db_module):
    """P12: на етикета 100×150 баркодът беше 79×5.9мм (височина 34), а
    клиентът — 15px. Сега височина 118 (черти ≥20мм при ширина ~87мм),
    клиентът е с размер по дължината на текста, палет № и бруто са едри."""
    body = _view(admin_client, db_module, "pallet", PL, "?format=label")
    head = body.split('class="plt-head-barcode"')[1][:400]
    assert 'height="137"' in head  # 118 черти + 19 текст
    assert "plt-client-box plt-client-s" in body
    assert body.count('class="val val-key"') == 2
    assert body.count('class="pbox"') == 4  # 4 кутии в един ред (заявка 12.08.2026)
    css = _css()
    assert ".label-format .plt { display: flex; flex-direction: column; min-height: 140mm; }" in css
    assert ".label-format .plt-stats .pbox .val.val-key { font-size: 24px;" in css


def test_pallet_label_long_client_gets_a_smaller_font(admin_client, db_module):
    long_name = "Акционерно дружество „Гьокхан Шимшек Индустри ве Тиджарет“ " * 4
    body = _view(admin_client, db_module, "pallet", dict(PL, client_name=long_name), "?format=label")
    assert "plt-client-box plt-client-l" in body


def test_pallet_a4_barcode_is_unchanged(admin_client, db_module):
    body = _view(admin_client, db_module, "pallet", PL)
    assert 'height="69"' in body.split('class="plt-head-barcode"')[1][:400]


def test_pallet_without_date_prints_a_dash(admin_client, db_module):
    """P24: „Дата / Date:“ оставаше голо."""
    body = _view(admin_client, db_module, "pallet", dict(PL, doc_date=""))
    assert "Дата / Date: <b>—</b>" in body


# ---------------------------------------------------------------- P21: групов печат на палетни карти
def _bulk_ids(db_module, n=2, items_format=""):
    items = ([dict(order_no="45", pos="10", reference="R", reference_desc="D", qty="5")]
             if items_format == "orders" else [dict(code="A", description="D", qty="5", weight="1")])
    return [_doc(db_module, "pallet", dict(PL, items=items, items_format=items_format))
            for _ in range(n)]


@pytest.mark.parametrize("items_format", ["", "orders"])
def test_pallet_bulk_numeric_headers_are_marked(admin_client, db_module, items_format):
    """P21: колоните с количество/тегло в груповия печат бяха без class="n"
    (заглавието вляво, числата вдясно)."""
    ids = _bulk_ids(db_module, 1, items_format)
    body = admin_client.get("/pallet/bulk-print?ids=%s" % ",".join(map(str, ids))).data.decode()
    head = _thead(body)
    labels = ["Кол. / Open Qty"] if items_format == "orders" else ["Количество / Qty", "Тегло, кг / Weight"]
    for label in labels:
        assert re.search(r'<th class="n"[^>]*>%s</th>' % re.escape(label), head), label


def test_pallet_bulk_print_renders_a_qr_per_card_when_the_route_provides_it(flask_app, db_module):
    """P21: единичната карта има QR код, груповият печат — не. Шаблонът
    показва QR за всяка карта, за която маршрутът подаде `qr_by_id`
    ({doc.id: data URI}); без него картата остава без QR (както досега)."""
    from flask import render_template

    ids = _bulk_ids(db_module, 2)
    con = db_module.get_db()
    rows = [con.execute("SELECT * FROM documents WHERE id = ?", (i,)).fetchone() for i in ids]
    con.close()
    docs = [(r, json.loads(r["data"])) for r in rows]
    qr = "data:image/png;base64,QUJD"
    with flask_app.test_request_context("/pallet/bulk-print"):
        with_qr = render_template("pallet_bulk_print.html", docs=docs, ids_str="1",
                                  qr_by_id={ids[0]: qr})
        without = render_template("pallet_bulk_print.html", docs=docs, ids_str="1")
    assert with_qr.count('class="doc-qr"') == 1
    assert 'src="%s"' % qr in with_qr
    assert 'class="doc-qr"' not in without


# ---------------------------------------------------------------- P14/P15/P16/P27: декларации
def test_dualuse_cites_regulation_2021_821(admin_client, db_module):
    """P14: бланката цитираше „Регламент (ЕС) 821/2021“, а бележката под нея
    — „(ЕС) 2021/821“ (правилното)."""
    body = _view(admin_client, db_module, "dualuse", {"sender_name": "А", "invoice_numbers": "1",
                                                      "destination_country": "Турция"})
    assert "821/2021" not in body
    assert body.count("(ЕС) 2021/821") == 3


def test_dualuse_has_a_signature_and_stamp_line(admin_client, db_module):
    """P15: декларацията се подписва — нямаше ред за подпис/печат."""
    body = _view(admin_client, db_module, "dualuse", {"sender_name": "А", "declarant_name": "Иван"})
    foot = body.split('class="dud-foot"')[1].split('class="dud-barcode"')[0]
    assert '<div class="sign-line">подпис и печат</div>' in foot
    assert ".dud-foot .signer .sign-line {" in _css()


def test_dualuse_empty_fields_print_a_fill_in_line(admin_client, db_module):
    """P16: празни полета печатаха „по фактура , износ за ,“."""
    body = _view(admin_client, db_module, "dualuse", {"sender_name": "А"})
    assert "по фактура ," not in body and "износ за ," not in body
    assert "по фактура %s, износ за %s, не" % (FILL, FILL) in body
    assert "ЕИК/ЕГН %s" % FILL in body


def test_dualuse_filled_fields_are_printed_as_entered(admin_client, db_module):
    body = _view(admin_client, db_module, "dualuse", {
        "sender_name": "А", "sender_eik": "123456789", "invoice_numbers": "0000001234",
        "destination_country": "Турция", "place": "Габрово", "doc_date": "2026-10-04",
        "declarant_name": "Иван Петров"})
    assert "по фактура 0000001234, износ за Турция, не" in body
    assert FILL not in body


def test_export_it_empty_fields_print_a_fill_in_line(admin_client, db_module):
    """P16: „по фактура № <b></b> за фирма <b></b>“ при празни полета."""
    body = _view(admin_client, db_module, "export_it", {"declarant_name": "А"})
    assert "<b></b>" not in body
    assert "фактура № <b>%s</b>" % FILL in body
    assert "за фирма <b>%s</b>" % FILL in body


def test_declarations_have_distinct_subtitles_and_the_same_number_sign(admin_client, db_module):
    """P27: двете бланки бяха само „ДЕКЛАРАЦИЯ“, номерът — „№“ срещу „Nr:“."""
    du = _view(admin_client, db_module, "dualuse", {"sender_name": "А"})
    ex = _view(admin_client, db_module, "export_it", {"declarant_name": "А"})
    assert '<div class="dud-sub">за стоки с възможна двойна употреба</div>' in du
    assert '<div class="exi-sub">за общностен статут на стоките</div>' in ex
    assert "Nr:" not in ex
    assert re.search(r'<div class="no">№ \d{4}/\d{4}</div>', ex)
    assert re.search(r'<div class="no">№ \d{4}/\d{4}</div>', du)


# ---------------------------------------------------------------- ЧМР: P5/P6/P7/P13/P26
CMR = dict(sender_name="Изпращач", consignee_name="Получател", marks="PL-001\nPL-002",
           goods="Стоманени детайли\nЛагери", packages="2", weight="1200")


def test_cmr_goods_cells_keep_operator_newlines():
    """P5: кутии 6–9 губеха новите редове на оператора (без pre-wrap)."""
    assert ".cmr-grid .goods td { white-space: pre-wrap; }" in _css()


def test_cmr_goods_header_row_does_not_stretch():
    """P7: `height: 100%` на таблицата разтягаше и заглавния ред —
    стойностите плуваха насред кутията."""
    assert ".cmr-grid .goods tr:first-child { height: 1px; }" in _css()


def test_cmr_overflow_fallback_uses_natural_row_heights():
    """P6: когато и последната степен не стига — редове по съдържание."""
    css = _css()
    assert ".cmr-overflow .cmr-grid { grid-auto-rows: auto; }" in css
    js = read_source("static", "app.js")
    fit = js[js.index("function fitCmrPages"):js.index("function relocateQrHints")]
    assert 'page.classList.toggle("cmr-overflow", pass === 1)' in fit


def test_cmr_qr_is_not_shrunk_by_the_fit_levels_and_barcode_is_taller(admin_client, db_module):
    """P13: QR падаше до 38px (≈10мм) на последната степен, баркодът беше
    67×5.2мм. Степените вече не пипат --cmr-qr; баркодът е с височина 64."""
    css = _css()
    for level in range(1, 6):
        block = re.search(r"\.cmr-fit-%d \.cmr \{([^}]*)\}" % level, css).group(1)
        assert "--cmr-qr" not in block, level
    body = _view(admin_client, db_module, "cmr", CMR)
    head_no = body.split('<div class="no">')[1][:600]
    assert 'height="83"' in head_no  # 64 черти + 19 текст


def test_cmr_copy_label_is_bold_and_bigger(admin_client, db_module):
    """P26: надписът на екземпляра беше 10px курсив."""
    css = _css()
    assert ".cmr .copy-label { font-size: 12px; font-style: normal; font-weight: 700;" in css
    body = _view(admin_client, db_module, "cmr", CMR, "?copies=3")
    assert "2. Екземпляр за получателя / Copy for consignee" in body


def test_print_qr_label_wraps_on_the_form():
    """P4/P26: надписът под QR кода беше неразделим (~33мм)."""
    assert ".print-page .doc-qr-label { white-space: normal; max-width: 26mm; }" in _css()


# ---------------------------------------------------------------- товарителница: P4/P8/P9
def test_waybill_header_number_box_can_shrink():
    """P4: `.twb-head-no { width: 70mm; flex: none }` + лого → заглавната
    лента преливаше с 53px и режеше QR кода."""
    css = _css()
    assert ".twb-head-no { flex: 0 1 70mm; min-width: 0; text-align: center; }" in css
    assert ".twb-head .doc-logo { max-width: 120px; }" in css
    assert "flex: none; display: flex; align-items: center; gap: 6px; }" in css  # 2-up без промяна


def test_waybill_two_up_is_decided_by_measured_height():
    """P8: „2 на лист“ се избираше само по брой редове (≤ 8)."""
    js = read_source("static", "app.js")
    assert "function fitWaybillPages()" in js
    assert "initWaybillPrintFit();" in js
    fit = js[js.index("function fitWaybillPages"):js.index("function initWaybillPrintFit")]
    assert "getBoundingClientRect().height" in fit
    assert "WAYBILL_PRINTABLE_MM" in fit
    assert ".twb-measuring { width: 210mm !important;" in _css()


def test_waybill_footer_never_starts_a_sheet_alone():
    """P9: при 60 реда 2 от 8-те листа носеха само колонтитула."""
    assert ".twb-notes, .twb-footer { break-before: avoid; page-break-before: avoid; }" in _css()
