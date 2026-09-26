# -*- coding: utf-8 -*-
"""Регресионни тестове за теста за грешки от 25.09.2026 (ERP_ТЕСТ_2026_09_25.md) —
документи, фактури (само в евро), PDF, търсене, табло и дребните сривове
при невалиден вход. Номерата в имената на тестовете следват отчета."""
import html
import json
import re

from conftest import get_edit_doc_version, post_with_csrf


def _doc_data(db_module, doc_id):
    con = db_module.get_db()
    try:
        return json.loads(con.execute("SELECT data FROM documents WHERE id = ?",
                                      (doc_id,)).fetchone()[0])
    finally:
        con.close()


# ---------------------------------------------------------------- №3 конфликт при редакция

def test_03_edit_conflict_names_the_other_users_values(admin_client, db_module):
    """Второто „Запази“ след конфликт заменя чуждата редакция — затова
    съобщението трябва да казва истината и да показва записаните стойности,
    а не да твърди, че формата е „презаредена с актуалните данни“."""
    post_with_csrf(admin_client, "/cmr/new", {"consignee_name": "Orig", "goods": "orig goods"})
    stale_version = get_edit_doc_version(admin_client, "/doc/1/edit")
    post_with_csrf(admin_client, "/doc/1/edit", {"consignee_name": "Orig", "goods": "B goods"})

    resp = post_with_csrf(admin_client, "/doc/1/edit",
                          {"consignee_name": "A name", "goods": "orig goods",
                           "edit_doc_version": stale_version})
    assert resp.status_code == 302 and "restore=" in resp.headers["Location"]
    assert _doc_data(db_module, 1)["goods"] == "B goods"  # първият опит не презаписва

    body = admin_client.get(resp.headers["Location"]).data.decode()
    text = html.unescape(body)
    assert "актуалните данни" not in text
    assert "Вашите промени НЕ са записани" in text
    assert "Вид на стоката: записано „B goods“, ваше „orig goods“" in text
    # Въведеното от оператора е запазено във формата.
    edit = json.loads(html.unescape(re.search(r"data-edit='([^']*)'", body).group(1)))
    assert edit["consignee_name"] == "A name"


def test_03_edit_conflict_with_non_ascii_digit_version_is_a_conflict_not_a_crash(admin_client, db_module):
    """„²“.isdigit() е True, но int(„²“) гърми — по-рано срив (302 от общия
    обработчик) вместо нормалния конфликтен изход."""
    post_with_csrf(admin_client, "/cmr/new", {"consignee_name": "Orig"})
    resp = post_with_csrf(admin_client, "/doc/1/edit",
                          {"consignee_name": "X", "edit_doc_version": "²"})
    assert resp.status_code == 302 and "restore=" in resp.headers["Location"]


# ---------------------------------------------------------------- №10 PDF с дълга клетка

import io  # noqa: E402

import pytest  # noqa: E402
from pypdf import PdfReader  # noqa: E402


def _issue(admin_client, url, fields):
    resp = post_with_csrf(admin_client, url, fields)
    assert resp.status_code == 302, resp.status_code
    return int(re.search(r"/doc/(\d+)", resp.headers["Location"]).group(1))


def _pdf_words(admin_client, doc_id):
    resp = admin_client.get("/doc/%d/export.pdf" % doc_id)
    assert resp.status_code == 200, resp.headers.get("Location")
    assert resp.mimetype == "application/pdf"
    reader = PdfReader(io.BytesIO(resp.data))
    # Тясната текстова колона пренася и в средата на дума (-pdf-word-wrap:
    # CJK), затова сравнението е без интервали и нови редове.
    return "".join("".join(page.extract_text().split()) for page in reader.pages)


@pytest.mark.parametrize("url,base,item_key", [
    ("/packing/new", {"receiver_name": "R"}, "description"),
    ("/waybill/new", {"consignee_name": "R"}, "marks"),
])
def test_10_pdf_export_survives_a_cell_taller_than_a_page(admin_client, db_module, url, base, item_key):
    """Клетка с ~1500 знака (над страница в тясна колона) проваляше целия
    PDF износ с LayoutError. Сега текстът се разделя на редове-продължения
    и НИЩО не се губи."""
    words = ["дума%03d" % i for i in range(180)]
    fields = dict(base)
    fields["items_json"] = json.dumps([{item_key: " ".join(words), "qty": "1"}])
    doc_id = _issue(admin_client, url, fields)
    text = _pdf_words(admin_client, doc_id)
    missing = [w for w in words if w not in text]
    assert not missing, missing[:5]



def test_10_long_unbroken_value_is_split_without_losing_characters():
    import pdf_export
    value = "X" * 600 + " края"
    parts = pdf_export._split_long_text(value)
    assert all(len(p) <= pdf_export._PDF_CELL_CHUNK for p in parts)
    assert "".join(parts).replace(" ", "") == value.replace(" ", "")
    assert pdf_export._split_long_text("кратко") == ["кратко"]


# ---------------------------------------------------------------- №11 търсене

def _list_numbers(admin_client, query):
    body = admin_client.get("/docs", query_string={"q": query}).get_data(as_text=True)
    return set(re.findall(r'href="/doc/(\d+)"', body))


def test_11_search_finds_values_with_quotes_and_backslashes(admin_client, db_module):
    """Търсенето вървеше по суровия json.dumps, където `"` и `\\` са
    ескейпнати — име като `Фирма "Ромашка" ЕООД` не се намираше."""
    _issue(admin_client, "/cmr/new", {"consignee_name": 'Фирма "Ромашка" ЕООД', "goods": "C:\\path"})
    _issue(admin_client, "/cmr/new", {"consignee_name": "Друга"})
    assert _list_numbers(admin_client, '"Ромашка"') == {"1"}
    assert _list_numbers(admin_client, 'Фирма "ромашка"') == {"1"}
    assert _list_numbers(admin_client, "C:\\path") == {"1"}


def test_11_search_does_not_match_json_key_names(admin_client, db_module):
    _issue(admin_client, "/cmr/new", {"consignee_name": "Първа"})
    _issue(admin_client, "/cmr/new", {"consignee_name": "Втора"})
    assert _list_numbers(admin_client, "consignee") == set()
    assert _list_numbers(admin_client, "втора") == {"2"}


# ---------------------------------------------------------------- №14 двойна валута

@pytest.mark.parametrize("value", ["500 лева", "50 евро", "€500", "EUR 500", "500 eur.",
                                   "500 EURO", "100 USD", "500 лв."])
def test_14_amount_with_a_written_currency_gets_no_second_euro_sign(value):
    from appcore import format_eur_amount
    assert format_eur_amount(value) == value


@pytest.mark.parametrize("value,expected", [("500", "500 €"), ("12,50", "12,50 €"),
                                            ("1 200,00", "1 200,00 €")])
def test_14_plain_amount_still_gets_the_euro_sign(value, expected):
    from appcore import format_eur_amount
    assert format_eur_amount(value) == expected


# ---------------------------------------------------------------- №23 прекалено голяма заявка

def test_23_too_large_request_never_redirects_to_a_foreign_referer(client):
    resp = client.post("/login", data=b"x" * (26 * 1024 * 1024),
                       content_type="application/x-www-form-urlencoded",
                       headers={"Referer": "https://evil.example.com/phish"})
    assert resp.status_code == 302
    assert "evil.example.com" not in resp.headers["Location"]


# ---------------------------------------------------------------- №25 огромно кол. × цена

def test_25_invoice_with_a_huge_quantity_times_price_still_opens_and_exports(admin_client, db_module):
    """Баркод, поставен в „Количество“ (4006381333931 × 25 000 000 000 000
    ≈ 1e26): фактурата се записваше, но quantize гърмеше с InvalidOperation
    — не можеше да бъде отворена, отпечатана или изнесена."""
    resp = post_with_csrf(admin_client, "/invoice-br/new", {
        "consignee_name": "Клиент",
        "items_json": json.dumps([{"description": "Стока", "qty": "4006381333931",
                                   "unit_price": "25000000000000", "net_weight": "1"}]),
    }, csrf_source_url="/invoice-br/new")
    assert resp.status_code == 302
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    view = admin_client.get("/doc/%d" % doc_id)
    assert view.status_code == 200
    assert "100159533348275000000000000.00" in view.get_data(as_text=True)
    assert admin_client.get("/doc/%d/export.xlsx" % doc_id).status_code == 200
    assert admin_client.get("/doc/%d/export.pdf" % doc_id).status_code == 200


def test_25_absurdly_long_number_is_treated_as_invalid_not_a_crash():
    from appcore import invoice_row_total, invoice_totals
    assert invoice_row_total({"qty": "1" * 60, "unit_price": "1"}) == ""
    assert invoice_totals([{"qty": "1" * 60, "unit_price": "1"}])["price"] == ""


# ---------------------------------------------------------------- №26 боклук във входа

HUGE = "99999999999999999999999"


def test_26_huge_id_in_the_url_is_a_404_not_a_crash(admin_client):
    for url in ("/doc/%s" % HUGE, "/doc/%s/edit" % HUGE, "/doc/%s/export.xlsx" % HUGE):
        assert admin_client.get(url).status_code == 404, url


def test_26_huge_or_negative_page_is_clamped(admin_client, db_module):
    _issue(admin_client, "/cmr/new", {"consignee_name": "R"})
    for page in (HUGE, "-1", "0"):
        resp = admin_client.get("/docs", query_string={"page": page})
        assert resp.status_code == 200, page
        assert "-1 от" not in resp.get_data(as_text=True)


def test_26_preview_with_non_ascii_digit_ids_is_not_a_crash(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/cmr/preview",
                          {"consignee_name": "R", "edit_doc_id": "²", "edit_doc_version": "²"},
                          csrf_source_url="/cmr/new")
    assert resp.status_code == 302
    assert "/preview/" in resp.headers["Location"]


def test_26_non_string_item_values_are_saved_as_text(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/invoice-br/new", {
        "consignee_name": "Клиент",
        "items_json": json.dumps([{"po_no": 5, "description": ["a"], "qty": 2, "unit_price": "3"}]),
    }, csrf_source_url="/invoice-br/new")
    assert resp.status_code == 302 and "/doc/" in resp.headers["Location"]
    item = _doc_data(db_module, 1)["items"][0]
    assert item["po_no"] == "5" and item["qty"] == "2" and item["description"] == ""
    assert admin_client.get("/doc/1/export.xlsx").status_code == 200


def test_26_barcode_with_characters_outside_code128_is_a_404(admin_client):
    assert admin_client.get("/barcode/Жаба.svg").status_code == 404
    assert admin_client.get("/barcode/ABC123.svg").status_code == 200


# ---------------------------------------------------------------- №27 U+FFFE в Excel износа

def test_27_excel_export_survives_xml_noncharacters(admin_client, db_module):
    _issue(admin_client, "/cmr/new", {"consignee_name": "R￾x￿y"})
    resp = admin_client.get("/doc/1/export.xlsx")
    assert resp.status_code == 200
    assert resp.mimetype.endswith("spreadsheetml.sheet")


# ---------------------------------------------------------------- №33 топ клиенти

def test_33_dashboard_groups_client_spellings_case_insensitively(admin_client, db_module):
    import routes_dashboard
    for name in ("ACME Ltd", "Acme Ltd", "acme ltd ", "Иван ООД", "иван оод"):
        _issue(admin_client, "/cmr/new", {"consignee_name": name})
    con = db_module.get_db()
    try:
        stats = routes_dashboard._dashboard_stats(con)
    finally:
        con.close()
    counts = sorted(c for _name, c in stats["top_clients"])
    assert counts == [2, 3], stats["top_clients"]


# ---------------------------------------------------------------- №21 заключени версии за билда

def test_21_release_build_installs_with_the_pinned_constraints():
    import os
    from packaging.requirements import Requirement
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workflow = open(os.path.join(root, ".github", "workflows", "release.yml"), encoding="utf-8").read()
    install_lines = [line.strip() for line in workflow.splitlines()
                     if line.strip().startswith("pip install")]
    assert install_lines and all("-c constraints-release.txt" in l for l in install_lines), install_lines

    pins = {}
    for line in open(os.path.join(root, "constraints-release.txt"), encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            name, version = line.split("==")
            pins[name.lower().replace("_", "-")] = version
    for line in open(os.path.join(root, "requirements.txt"), encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        name = req.name.lower().replace("_", "-")
        assert name in pins, "%s липсва в constraints-release.txt" % name
        assert req.specifier.contains(pins[name], prereleases=True), (name, pins[name])


def test_11_invoice_search_finds_values_with_quotes(admin_client, db_module):
    for name in ('Фирма "Ромашка" ЕООД', "Друга"):
        resp = post_with_csrf(admin_client, "/invoice-br/new", {
            "consignee_name": name,
            "items_json": json.dumps([{"description": "Стока", "qty": "1", "unit_price": "2"}]),
        }, csrf_source_url="/invoice-br/new")
        assert resp.status_code == 302
    body = admin_client.get("/invoices", query_string={"q": '"Ромашка"'}).get_data(as_text=True)
    assert "Ромашка" in html.unescape(body) and "Друга" not in body
    body = admin_client.get("/invoices", query_string={"q": "consignee"}).get_data(as_text=True)
    assert "Ромашка" not in html.unescape(body) and "Друга" not in body
