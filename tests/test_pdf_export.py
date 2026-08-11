# -*- coding: utf-8 -*-
"""Регресионни тестове за PDF износа на документи (бутон „Изтегли PDF“ —
задача 25 от заявката за търсене/клиентски папки/PDF+Excel износ). Виж
pdf_export.py за пълния коментар защо е ЕДИН споделен генеричен PDF шаблон
(преизползващ _XLSX_FIELDS/_XLSX_ITEM_COLUMNS), не 6 pixel-perfect копия.

Съдържателните тестове тук четат ИЗВЛЕЧЕН текст от готовия PDF (pypdf), не
само проверяват дължина/статус — така реално се хваща регресия от типа
"кирилицата пак излиза като плътни правоъгълници" (виж Errors and fixes в
резюмето на сесията): ако DejaVu Sans спре да се зарежда правилно, PDF-ът
пак ще се генерира без грешка (xhtml2pdf не гърми), но извлеченият текст
би излязъл празен/грешен вместо реалната кирилица."""
import io
import json

import pytest
from pypdf import PdfReader

from conftest import post_with_csrf

import barcode128
import pdf_export


def _pdf_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


# ---------------------------------------------------------------- barcode128.code128_png_data_uri (unit)

def test_code128_png_data_uri_returns_valid_png():
    from PIL import Image
    import base64

    uri = barcode128.code128_png_data_uri("PAL-000123")
    assert uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    img = Image.open(io.BytesIO(raw))
    assert img.format == "PNG"
    assert img.width > 0 and img.height == 50  # height по подразбиране


def test_code128_png_data_uri_rejects_non_ascii_b_charset():
    with pytest.raises(ValueError):
        barcode128.code128_png_data_uri("кирилица не е Code128-B")


# ---------------------------------------------------------------- pdf_export.generate_document_pdf (unit,
# в реален Flask app/request context — render_template го изисква)

def test_generate_document_pdf_embeds_extractable_cyrillic_text(flask_app):
    with flask_app.test_request_context():
        pdf_bytes = pdf_export.generate_document_pdf(
            "ЧМР товарителница", "0001/2026", "PAL-000123",
            [("Изпращач", "Тест Кирилица ЙЪЬЯЮ, щ")], [], [])
    text = _pdf_text(pdf_bytes)
    assert "ЧМР товарителница" in text
    assert "0001/2026" in text
    assert "Тест Кирилица ЙЪЬЯЮ, щ" in text


def test_generate_document_pdf_without_barcode_omits_image(flask_app):
    with flask_app.test_request_context():
        pdf_bytes = pdf_export.generate_document_pdf(
            "Тест", "1", "", [("Поле", "Стойност")], [], [])
    assert pdf_bytes[:4] == b"%PDF"  # генерира се коректно и без баркод


def test_generate_document_pdf_renders_items_table(flask_app):
    with flask_app.test_request_context():
        pdf_bytes = pdf_export.generate_document_pdf(
            "Палетна карта", "0002/2026", "PAL-000124", [],
            [{"code": "ART-1", "description": "Тестов артикул", "qty": "10"}],
            [("code", "Артикул/код"), ("description", "Описание"), ("qty", "Количество")])
    text = _pdf_text(pdf_bytes)
    assert "ART-1" in text
    assert "Тестов артикул" in text


# ---------------------------------------------------------------- HTTP маршрут /doc/<id>/export.pdf

def test_export_document_pdf_returns_pdf(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач за PDF износ",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/pdf"
    assert resp.data[:4] == b"%PDF"
    assert "Изпращач за PDF износ" in _pdf_text(resp.data)


def test_export_document_pdf_download_filename_uses_number(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    disposition = resp.headers.get("Content-Disposition", "")
    assert "cmr_" in disposition
    assert disposition.endswith('.pdf"') or ".pdf" in disposition


def test_export_document_pdf_requires_login(client):
    resp = client.get("/doc/1/export.pdf", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_export_document_pdf_unknown_doc_returns_404(admin_client):
    resp = admin_client.get("/doc/999999/export.pdf")
    assert resp.status_code == 404


def test_pallet_pdf_includes_packaging_type_and_total_qty(admin_client):
    resp = post_with_csrf(admin_client, "/pallet/new", {
        "client_name": "Клиент Палет PDF ООД",
        "pallet_type": "120x80",
        "packaging_type": "Кашон",
        "gross": "300",
        "items_json": ('[{"code": "ART-9", "description": "Стока за PDF", '
                       '"qty": "7", "weight": "20"}]'),
        "items_format": "manual",
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    assert resp.status_code == 200
    text = _pdf_text(resp.data)
    assert "Кашон" in text  # Вид опаковка
    assert "7" in text      # Общ брой (изчислен от items, не суров запис)
    assert "ART-9" in text
    assert "Стока за PDF" in text


def test_export_document_pdf_matches_xlsx_field_labels(admin_client):
    """PDF-ът и Excel износът трябва да показват едни и същи полета (СЪЩИЯТ
    _XLSX_FIELDS речник, виж routes_documents._export_fields_and_items) —
    смисленото на "единен генеричен PDF шаблон вместо 6 отделни"."""
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "weight": "500",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    pdf_text = _pdf_text(admin_client.get("/doc/%s/export.pdf" % doc_id).data)
    assert "Бруто тегло, кг" in pdf_text
    assert "Дата на съставяне" in pdf_text


# ---------------------------------------------------------------- регресия: 500 при "Изтегли PDF"
# Заявка (наживо, скрийншот от потребителя): избор на „Изтегли PDF“ дава
# суров „Internal Server Error“. Възпроизведено локално с fuzz-тест по
# всичките 9 типа документи и „гадни“ стойности (много дълги неразделими
# низове, арабски/китайски текст, емоджита, HTML-специални знаци,
# нечислови стойности в числови полета) — само фактурата за Норвегия
# (9 колони на реда — най-много от всички типове) реално гърмеше: reportlab
# (xhtml2pdf) пресмята ширина на всяка колона от съдържанието ѝ (auto-fit,
# по подразбиране), а с достатъчно колони и достатъчно "тежко" съдържание
# в няколко от тях сумата на "естествените" ширини надвишава страницата —
# reportlab пресмята отрицателна свободна ширина и гърми със сурово
# ValueError/TypeError дълбоко в reportlab.platypus.tables, НЕ просто връща
# result.err (единствения случай, който generate_document_pdf хващаше
# преди тази поправка). ВАЖНО (проверено наживо при диагностиката): нито
# CSS `table-layout`, нито `word-wrap`/`overflow-wrap` се поддържат от
# xhtml2pdf — тихо се игнорират, затова поправка само с тях НЕ работи;
# истинската поправка е ИЗРИЧНА ширина (%) на всяко th в pdf_export.html
# (reportlab четe `width` на клетка, вижте xhtml2pdf/tables.py).
#
# Тестовете тук пресъздават ТОЧНО фактурата за Норвегия сценария (доказано
# възпроизводимо И преди, И след поправката — за разлика от по-опростени
# опити с една-единствена дълга стойност, които НЕ гърмяха дори преди
# поправката и затова не биха хванали регресия).

_NASTY = [
    "", None, "   ", "N/A", "n/a", "-", "—",
    "Тест <b>&</b> \"кавички\" 'единични'",
    "Müller & Söhne GmbH",
    "شركة النقل الدولي",  # арабски
    "包装 测试",  # китайски
    "line1\nline2\r\nline3",
    "🚚📦 emoji тест",
    "A" * 500,  # дълга неразделима дума, без интервали
    "12.5,3", "1e400", "NaN", "Infinity", "-0", "0", "-123.456", "1,234.56", "не е число",
]


def _nasty_dict(keys):
    return {k: _NASTY[i % len(_NASTY)] for i, k in enumerate(keys)}


_INVOICE_NO_FIELD_KEYS = [
    "sender_name", "sender_address", "sender_city", "sender_country",
    "consignee_name", "consignee_address", "consignee_city", "consignee_country",
    "invoice_number", "doc_date", "confirmation_no", "incoterms",
    "payment_terms", "currency", "bank_name", "bank_iban", "bank_swift",
    "notes",
]
_INVOICE_NO_ITEM_KEYS = ["hs_code", "description", "pallet_no", "po_no", "pos",
                        "material_code", "qty", "unit_price"]


def test_generate_document_pdf_does_not_crash_on_the_norway_invoice_worst_case(flask_app):
    """Юнит ниво: директно през _export_fields_and_items — точно както
    routes_documents.export_document_pdf изгражда fields/items/cols за
    реален документ. 9 колони (Норвегия) × 3 реда, всяка стойност „гадна“
    (виж _NASTY по-горе) — точно комбинацията, която гърмеше преди
    поправката."""
    from routes_documents import _export_fields_and_items

    data = _nasty_dict(_INVOICE_NO_FIELD_KEYS)
    data["items"] = [_nasty_dict(_INVOICE_NO_ITEM_KEYS) for _ in range(3)]
    fields, items, cols = _export_fields_and_items("invoice_no", data)
    assert len(cols) == 9  # точно толкова, колкото гърмеше наживо

    with flask_app.test_request_context():
        pdf_bytes = pdf_export.generate_document_pdf(
            "Фактура за Норвегия", "0001/2026", "", fields, items, cols)
    assert pdf_bytes[:4] == b"%PDF"


def test_export_document_pdf_survives_the_norway_invoice_worst_case(admin_client):
    """HTTP ниво: същият сценарий, но през реалния маршрут (издаване +
    /doc/<id>/export.pdf) — преди поправката тук се получаваше суров 500
    без никакво обяснение (точно каквото докладва потребителят)."""
    form_data = {k: (v if v is not None else "") for k, v in
                _nasty_dict(_INVOICE_NO_FIELD_KEYS).items()}
    form_data["invoice_number"] = "PDF-РЕГРЕСИЯ-1"  # ръчният номер не бива да е празен
    form_data["items_json"] = json.dumps(
        [_nasty_dict(_INVOICE_NO_ITEM_KEYS) for _ in range(3)], ensure_ascii=False)

    resp = post_with_csrf(admin_client, "/invoice-no/new", form_data,
                          csrf_source_url="/invoice-no/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    resp = admin_client.get("/doc/%s/export.pdf" % doc_id)
    assert resp.status_code == 200
    assert resp.data[:4] == b"%PDF"


def test_export_document_pdf_shows_friendly_error_instead_of_500(admin_client, monkeypatch):
    """Ако PDF генерирането все пак гръмне (бъдещ, все още непредвиден
    случай в xhtml2pdf/reportlab), маршрутът вече хваща RuntimeError-а
    (виж pdf_export.generate_document_pdf) и показва ясно съобщение +
    връща към документа — НЕ суров „Internal Server Error“ бял екран."""
    import pdf_export as pdf_export_mod

    def _boom(*a, **kw):
        raise RuntimeError("симулирана грешка в reportlab")

    monkeypatch.setattr(pdf_export_mod, "generate_document_pdf", _boom)

    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач за грешка",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]

    resp = admin_client.get("/doc/%s/export.pdf" % doc_id, follow_redirects=True)
    assert resp.status_code == 200  # НЕ 500 — плавно пренасочване с flash
    assert "PDF файлът не можа да се генерира" in resp.data.decode()
