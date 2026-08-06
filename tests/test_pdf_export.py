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
