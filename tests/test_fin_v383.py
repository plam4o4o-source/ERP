# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, FIN): износ на фактурите към счетоводството (CSV,
Excel, Microinvest), е-фактура UBL 2.1 / Peppol BIS 3.0 (EN 16931), пакетът
по имейл (.eml чернова, SMTP само при изрично действие) и SAF-T полетата.

Мрежа няма: SMTP е подменен (FakeSMTP). XSD валидацията тръгва само при
зададена UBL_XSD_DIR (папка с UBL 2.1 схемите: maindoc/UBL-Invoice-2.1.xsd)."""
import csv
import decimal
import email
import email.policy
import io
import json
import os
import smtplib
import socket
import zipfile
import xml.etree.ElementTree as ET  # nosec B405 -- тестът чете собствения си изход

import pytest
from openpyxl import load_workbook

from conftest import post_with_csrf

NS = {"inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"}

SELLER = {"sender_name": "БиБиЕс България ЕООД", "sender_name_en": "BBS Bulgaria EOOD",
          "sender_vat": "BG123456789", "sender_eik": "123456789",
          "sender_address": "ул. Индустриална 5", "sender_address_en": "5 Industrialna Str.",
          "sender_city": "Габрово", "sender_city_en": "Gabrovo", "sender_postcode": "5300",
          "sender_country": "България", "sender_email": "office@bbs.example",
          "sender_iban": "BG80 BNBG 9661 1020 3456 78", "sender_swift": "BNBGBGSD",
          "sender_bank": "Postbank Gabrovo"}

BR_ITEMS = [{"hs_code": "85389099", "po_no": "4700200362", "pos": "30", "net_weight": "2.21",
             "material_code": "GLBK400002P0012", "qty": "20", "unit_price": "13.66"},
            {"hs_code": "85389099", "po_no": "4700200362", "pos": "40", "net_weight": "0.383",
             "material_code": "GLBK400001P0200", "qty": "3", "unit_price": "0.005"}]


def _seller(db_module, **extra):
    con = db_module.get_db()
    db_module.save_settings(con, dict(SELLER, **extra))
    con.commit()
    con.close()


def _issue(client, url, items, **fields):
    data = {"consignee_name": "Получател", "items_json": json.dumps(items)}
    data.update(fields)
    resp = post_with_csrf(client, url, data, csrf_source_url=url, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:400]
    return int(resp.headers["Location"].split("?")[0].rstrip("/").rsplit("/", 1)[-1])


def _brazil(client, number="BR-0001", **fields):
    base = {"invoice_number": number, "doc_date": "2026-11-02",
            "consignee_name": "ABB ELETRIFICACAO LTDA",
            "consignee_address": "Av. Dr. Ulysses Guimaraes 1000\n18087-000 Sorocaba - SP\nBrasil",
            "billto_name": "ABB ELETRIFICACAO LTDA",
            "billto_address": "Av. Dr. Ulysses Guimaraes 1000\nSorocaba - SP\nCNPJ: 61.074.829/0001-23\nBrazil",
            "terms_payment": "Net in 90 Days"}
    base.update(fields)
    return _issue(client, "/invoice-br/new", BR_ITEMS, **base)


def _italy(client, number="DU-IT-1", vat=True, **fields):
    billto = "Via Roma 1\n20100 Milano\nItaly"
    if vat:
        billto = "Via Roma 1\n20100 Milano\nVAT No: IT 01234567890\nItaly"
    base = {"invoice_number": number, "doc_date": "2026-11-03",
            "consignee_name": "ABB S.p.A.", "consignee_address": "Via Roma 1\n20100 Milano\nItalia",
            "billto_name": "ABB S.p.A.", "billto_address": billto}
    base.update(fields)
    return _issue(client, "/invoice-dubai/new",
                  [{"hs_code": "85389099", "po_no": "PO-77", "pos": "10",
                    "material_code": "1TFL151621P0550", "qty": "5", "unit_price": "10.10"}], **base)


def _xml(client, doc_id):
    resp = client.get("/doc/%d/einvoice.xml" % doc_id)
    assert resp.status_code == 200, resp.headers.get("Location")
    assert resp.mimetype == "application/xml"
    return resp.data


def _t(root, path):
    el = root.find(path, NS)
    return None if el is None else (el.text or "")


# ---------------------------------------------------------------- помощни функции

def test_country_vat_and_unit_helpers():
    import fin_data
    assert fin_data.country_from_text("Sorocaba - SP\nBrasil") == "BR"
    assert fin_data.country_from_text("Via Roma 1\n20100 Milano\nItalia") == "IT"
    assert fin_data.country_from_text("P.O. Box 1\nJebel Ali, Dubai") == "AE"
    assert fin_data.country_from_text("Oslo\nНорвегия") == "NO"
    assert fin_data.country_from_text("Sorocaba - SP") == ""  # „SP“ не е държава
    assert fin_data.vat_from_text("Milano\nVAT No: IT 0123 4567 890") == "IT01234567890"
    assert fin_data.vat_from_text("Organisation\nOrg.nr 923 609 016 MVA") == "923609016MVA"
    assert fin_data.vat_country("EL123456789") == "GR"
    assert fin_data.is_eu_vat("IT01234567890") and not fin_data.is_eu_vat("NO923609016MVA")
    assert [fin_data.unit_code(u) for u in ("", "бр", "PCS", "кг", "m", "палет", "xyz")] == \
        ["C62", "C62", "C62", "KGM", "MTR", "XPX", "C62"]
    assert fin_data.payment_days("Net in 90 Days") == 90
    assert fin_data.payment_days("30 дни") == 30


# ---------------------------------------------------------------- е-фактура

def test_brazil_einvoice_is_export_g_with_consistent_totals_and_no_weight(admin_client, db_module):
    _seller(db_module)
    doc_id = _brazil(admin_client)
    data = _xml(admin_client, doc_id)
    text = data.decode("utf-8")
    root = ET.fromstring(data)
    assert root.tag == "{%s}Invoice" % NS["inv"]
    assert _t(root, "cbc:CustomizationID") == \
        "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
    assert _t(root, "cbc:ProfileID") == "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
    assert _t(root, "cbc:ID") == "BR-0001"                         # BT-1
    assert _t(root, "cbc:IssueDate") == "2026-11-02"               # BT-2
    assert _t(root, "cbc:DueDate") == "2027-01-31"                 # BT-9 (90 дни)
    assert _t(root, "cbc:InvoiceTypeCode") == "380"                # BT-3
    assert _t(root, "cbc:DocumentCurrencyCode") == "EUR"           # BT-5
    assert _t(root, "cac:OrderReference/cbc:ID") == "4700200362"   # BT-13
    sp = "cac:AccountingSupplierParty/cac:Party/"
    assert _t(root, sp + "cac:PartyTaxScheme/cbc:CompanyID") == "BG123456789"  # BT-31
    assert _t(root, sp + "cac:PartyLegalEntity/cbc:RegistrationName") == SELLER["sender_name"]
    assert _t(root, sp + "cac:PostalAddress/cac:Country/cbc:IdentificationCode") == "BG"
    assert root.find(sp + "cbc:EndpointID", NS).get("schemeID") == "9926"
    bp = "cac:AccountingCustomerParty/cac:Party/"
    assert _t(root, bp + "cac:PartyLegalEntity/cbc:RegistrationName") == "ABB ELETRIFICACAO LTDA"
    assert _t(root, bp + "cac:PostalAddress/cac:Country/cbc:IdentificationCode") == "BR"
    # CNPJ няма префикс на държава → не е в PartyTaxScheme (BR-CO-09), а е BT-47.
    assert root.find(bp + "cac:PartyTaxScheme", NS) is None
    assert _t(root, bp + "cac:PartyLegalEntity/cbc:CompanyID") == "61074829000123"
    assert _t(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID") == "BG80BNBG96611020345678"
    assert _t(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cac:FinancialInstitutionBranch/cbc:ID") == "BNBGBGSD"
    assert _t(root, "cac:PaymentTerms/cbc:Note") == "Net in 90 Days"
    cat = root.find("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", NS)
    assert _t(cat, "cbc:ID") == "G" and _t(cat, "cbc:Percent") == "0"
    assert _t(cat, "cbc:TaxExemptionReasonCode") == "VATEX-EU-G"
    assert "Export outside the EU" in _t(cat, "cbc:TaxExemptionReason")
    # Суми: редовете = LineExtensionAmount = TaxExclusive = TaxInclusive = Payable.
    lines = root.findall("cac:InvoiceLine", NS)
    assert len(lines) == 2
    line_sum = sum(decimal.Decimal(_t(ln, "cbc:LineExtensionAmount")) for ln in lines)
    assert line_sum == decimal.Decimal("273.22")  # 20×13.66 + 3×0.005 (0.015 → 0.02)
    lmt = root.find("cac:LegalMonetaryTotal", NS)
    for tag in ("LineExtensionAmount", "TaxExclusiveAmount", "TaxInclusiveAmount", "PayableAmount"):
        assert decimal.Decimal(_t(lmt, "cbc:" + tag)) == line_sum
        assert lmt.find("cbc:" + tag, NS).get("currencyID") == "EUR"
    assert _t(root, "cac:TaxTotal/cbc:TaxAmount") == "0.00"
    first = lines[0]
    assert first.find("cbc:InvoicedQuantity", NS).get("unitCode") == "C62"
    assert _t(first, "cbc:InvoicedQuantity") == "20"
    assert _t(first, "cac:Price/cbc:PriceAmount") == "13.66"
    assert _t(first, "cac:Item/cac:SellersItemIdentification/cbc:ID") == "GLBK400002P0012"
    assert _t(first, "cac:Item/cac:CommodityClassification/cbc:ItemClassificationCode") == "85389099"
    assert _t(first, "cac:OrderLineReference/cbc:LineID") == "30"
    assert _t(first, "cac:Item/cac:ClassifiedTaxCategory/cbc:ID") == "G"
    for ln in lines:  # PEPPOL-EN16931-R120: ред = количество × цена (до цент)
        q = decimal.Decimal(_t(ln, "cbc:InvoicedQuantity"))
        p = decimal.Decimal(_t(ln, "cac:Price/cbc:PriceAmount"))
        assert (q * p).quantize(decimal.Decimal("0.01"), decimal.ROUND_HALF_UP) == \
            decimal.Decimal(_t(ln, "cbc:LineExtensionAmount"))
    # Фактурата за Бразилия е без „Total weight“ — никакво тегло в е-фактурата.
    assert "weight" not in text.lower() and "2.21" not in text and "Weight" not in text


def test_norway_and_dubai_invoices_are_exports_g(admin_client, db_module):
    _seller(db_module)
    no_id = _issue(admin_client, "/invoice-no/new",
                   [{"hs_code": "85389099", "description": "C-Profile", "pallet_no": "1",
                     "po_no": "PO-1", "pos": "1", "material_code": "X1", "qty": "2",
                     "unit_price": "5"}],
                   invoice_number="NO-1", consignee_name="ABB AS",
                   consignee_address="Bergerveien 12\n1396 Billingstad\nNorway")
    du_id = _issue(admin_client, "/invoice-dubai/new",
                   [{"hs_code": "85389099", "po_no": "PO-2", "pos": "1", "material_code": "X2",
                     "qty": "1", "unit_price": "99.99"}],
                   invoice_number="DU-1", consignee_name="ABB INDUSTRIES LLC",
                   consignee_address="Jebel Ali Free Zone\nDubai")
    for doc_id, country in ((no_id, "NO"), (du_id, "AE")):
        root = ET.fromstring(_xml(admin_client, doc_id))
        assert _t(root, "cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:ID") == "G"
        assert _t(root, "cac:Delivery/cac:DeliveryLocation/cac:Address/cac:Country/"
                        "cbc:IdentificationCode") == country
    root = ET.fromstring(_xml(admin_client, no_id))
    assert _t(root, "cac:InvoiceLine/cac:Item/cbc:Name") == "C-Profile"


def test_eu_buyer_italy_is_intra_community_k_with_buyer_vat(admin_client, db_module):
    _seller(db_module)
    root = ET.fromstring(_xml(admin_client, _italy(admin_client)))
    cat = root.find("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", NS)
    assert _t(cat, "cbc:ID") == "K"
    assert _t(cat, "cbc:TaxExemptionReasonCode") == "VATEX-EU-IC"
    assert "Intra-Community" in _t(cat, "cbc:TaxExemptionReason")
    bp = "cac:AccountingCustomerParty/cac:Party/"
    assert _t(root, bp + "cac:PartyTaxScheme/cbc:CompanyID") == "IT01234567890"   # BT-48
    assert _t(root, bp + "cac:PostalAddress/cac:Country/cbc:IdentificationCode") == "IT"
    assert _t(root, bp + "cac:PostalAddress/cbc:PostalZone") == "20100"
    assert root.find(bp + "cbc:EndpointID", NS).get("schemeID") == "0211"
    # BR-IC-11/12: дата и държава на доставката.
    assert _t(root, "cac:Delivery/cbc:ActualDeliveryDate") == "2026-11-03"
    assert _t(root, "cac:Delivery/cac:DeliveryLocation/cac:Address/cac:Country/"
                    "cbc:IdentificationCode") == "IT"
    assert _t(root, "cac:InvoiceLine/cac:Item/cac:ClassifiedTaxCategory/cbc:ID") == "K"


def test_eu_buyer_without_vat_is_refused_with_clear_message(admin_client, db_module):
    _seller(db_module)
    doc_id = _italy(admin_client, vat=False)
    resp = admin_client.get("/doc/%d/einvoice.xml" % doc_id)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/doc/%d" % doc_id)
    body = admin_client.get("/doc/%d" % doc_id).data.decode()
    assert "Е-фактурата не е създадена" in body and "ДДС № на купувача" in body


def test_buyer_vat_from_the_address_book(admin_client, db_module):
    """ДДС № и имейлът на купувача могат да идват от общата адресна книга."""
    _seller(db_module)
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, vat, country, email) VALUES (?, ?, ?, ?)",
                ("ABB S.p.A.", "IT 09999999999", "Италия", "ap@abb-it.example"))
    con.commit()
    con.close()
    root = ET.fromstring(_xml(admin_client, _italy(admin_client, vat=False)))
    bp = "cac:AccountingCustomerParty/cac:Party/"
    assert _t(root, bp + "cac:PartyTaxScheme/cbc:CompanyID") == "IT09999999999"
    ep = root.find(bp + "cbc:EndpointID", NS)
    assert ep.get("schemeID") == "EM" and ep.text == "ap@abb-it.example"


def test_missing_seller_vat_or_domestic_delivery_is_refused(admin_client, db_module):
    _seller(db_module, sender_vat="")
    doc_id = _brazil(admin_client)
    assert admin_client.get("/doc/%d/einvoice.xml" % doc_id).status_code == 302
    assert "ДДС № на фирмата изпращач" in admin_client.get("/doc/%d" % doc_id).data.decode()
    _seller(db_module)
    bg_id = _issue(admin_client, "/invoice-dubai/new",
                   [{"material_code": "A", "qty": "1", "unit_price": "1"}],
                   invoice_number="BG-1", consignee_name="Клиент ЕООД",
                   consignee_address="ул. Шипка 1\n1000 София\nБългария")
    assert admin_client.get("/doc/%d/einvoice.xml" % bg_id).status_code == 302
    assert "Доставката е в България" in admin_client.get("/doc/%d" % bg_id).data.decode()


def test_einvoice_only_for_invoices(admin_client, db_module):
    from conftest import issue_cmr
    cmr_id = issue_cmr(admin_client)
    assert admin_client.get("/doc/%d/einvoice.xml" % cmr_id).status_code == 404


#: Последователностите на UBL 2.1 XSD (само елементите, които ползваме).
_UBL_ORDER = {
    "Invoice": ["CustomizationID", "ProfileID", "ID", "IssueDate", "DueDate", "InvoiceTypeCode",
                "Note", "DocumentCurrencyCode", "BuyerReference", "OrderReference",
                "AccountingSupplierParty", "AccountingCustomerParty", "Delivery", "PaymentMeans",
                "PaymentTerms", "TaxTotal", "LegalMonetaryTotal", "InvoiceLine"],
    "Party": ["EndpointID", "PartyName", "PostalAddress", "PartyTaxScheme", "PartyLegalEntity",
              "Contact"],
    "PostalAddress": ["StreetName", "AdditionalStreetName", "CityName", "PostalZone", "Country"],
    "Address": ["StreetName", "AdditionalStreetName", "CityName", "PostalZone", "Country"],
    "InvoiceLine": ["ID", "InvoicedQuantity", "LineExtensionAmount", "OrderLineReference",
                    "Item", "Price"],
    "Item": ["Description", "Name", "SellersItemIdentification", "OriginCountry",
             "CommodityClassification", "ClassifiedTaxCategory"],
    "TaxCategory": ["ID", "Percent", "TaxExemptionReasonCode", "TaxExemptionReason", "TaxScheme"],
    "LegalMonetaryTotal": ["LineExtensionAmount", "TaxExclusiveAmount", "TaxInclusiveAmount",
                           "PayableAmount"],
    "Delivery": ["ActualDeliveryDate", "DeliveryLocation", "DeliveryParty"],
    "PaymentMeans": ["PaymentMeansCode", "PaymentID", "PayeeFinancialAccount"],
}


def _check_order(el):
    local = el.tag.split("}")[-1]
    order = _UBL_ORDER.get(local)
    if order:
        seen = [c.tag.split("}")[-1] for c in el]
        idx = [order.index(t) for t in seen if t in order]
        assert idx == sorted(idx), "%s: %s" % (local, seen)
        assert set(seen) <= set(order), "%s: непознат елемент %s" % (local, set(seen) - set(order))
    for child in el:
        _check_order(child)


def test_einvoice_element_order_follows_the_ubl_sequences(admin_client, db_module):
    _seller(db_module)
    for doc_id in (_brazil(admin_client), _italy(admin_client)):
        _check_order(ET.fromstring(_xml(admin_client, doc_id)))


@pytest.mark.skipif(not os.environ.get("UBL_XSD_DIR"), reason="UBL 2.1 XSD не е налична (UBL_XSD_DIR)")
def test_einvoice_validates_against_ubl21_xsd(admin_client, db_module):
    lxml_etree = pytest.importorskip("lxml.etree")
    schema = lxml_etree.XMLSchema(lxml_etree.parse(
        os.path.join(os.environ["UBL_XSD_DIR"], "maindoc", "UBL-Invoice-2.1.xsd")))
    _seller(db_module)
    for doc_id in (_brazil(admin_client), _italy(admin_client)):
        doc = lxml_etree.fromstring(_xml(admin_client, doc_id))
        assert schema.validate(doc), schema.error_log


def test_invoice_view_has_einvoice_and_email_buttons(admin_client, employee_client, db_module):
    _seller(db_module)
    doc_id = _brazil(admin_client)
    body = employee_client.get("/doc/%d" % doc_id).data.decode()
    assert "/doc/%d/einvoice.xml" % doc_id in body and "Е-фактура (XML)" in body
    assert "/email/compose?ids=%d" % doc_id in body and "Отвори като имейл" in body
    from conftest import issue_cmr
    cmr_id = issue_cmr(admin_client)
    cmr_body = admin_client.get("/doc/%d" % cmr_id).data.decode()
    assert "einvoice.xml" not in cmr_body and "Отвори като имейл" in cmr_body


def test_bulk_einvoice_zip_respects_filter_and_lists_errors(admin_client, db_module):
    _seller(db_module)
    _brazil(admin_client, "BR-0001")
    _italy(admin_client, "IT-OK")
    _italy(admin_client, "IT-NOVAT", vat=False)
    resp = admin_client.get("/invoices/einvoice.zip")
    assert resp.status_code == 200 and resp.mimetype == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    names = sorted(zf.namelist())
    assert names == ["ERRORS.txt", "einvoice_BR-0001.xml", "einvoice_IT-OK.xml"]
    assert "IT-NOVAT" in zf.read("ERRORS.txt").decode("utf-8-sig")
    only_br = zipfile.ZipFile(io.BytesIO(admin_client.get("/invoices/einvoice.zip?type=invoice_br").data))
    assert only_br.namelist() == ["einvoice_BR-0001.xml"]


# ---------------------------------------------------------------- CSV / Excel / Microinvest

def _csv_rows(resp):
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    text = resp.data.decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:]), delimiter=";"))


def test_csv_export_one_row_per_invoice_with_saft_fields_and_filter(admin_client, db_module):
    _seller(db_module)
    _brazil(admin_client, "BR-0001")
    _italy(admin_client, "IT-1")
    rows = _csv_rows(admin_client.get("/invoices/export.csv"))
    head = rows[0]
    for col in ("Дата", "Фактура №", "Код на документа", "Вид фактура", "Купувач",
                "ДДС № на купувача", "Държава на купувача", "Валута", "Сума без ДДС", "ДДС",
                "Сума общо", "ДДС № на продавача", "ДДС категория", "Условия на плащане",
                "Поръчка № (PO)"):
        assert col in head, col
    assert not any("тегло" in h.lower() or "weight" in h.lower() for h in head)
    recs = [dict(zip(head, r)) for r in rows[1:]]
    assert [r["Фактура №"] for r in recs] == ["BR-0001", "IT-1"]  # хронологично
    br, it = recs
    assert br["Държава на купувача"] == "BR" and br["ДДС категория"] == "G"
    assert br["Валута"] == "EUR" and br["Сума общо"] == "273.22" and br["ДДС"] == "0.00"
    assert br["ДДС № на продавача"] == "BG123456789" and br["Код на документа"] == "01"
    assert br["Поръчка № (PO)"] == "4700200362" and br["Условия на плащане"] == "Net in 90 Days"
    assert it["ДДС № на купувача"] == "IT01234567890" and it["ДДС категория"] == "K"
    assert it["Сума без ДДС"] == "50.50"
    # Филтърът на списъка (вид, търсене) важи и за износа.
    only = _csv_rows(admin_client.get("/invoices/export.csv?type=invoice_dubai"))
    assert [r[1] for r in only[1:]] == ["IT-1"]
    searched = _csv_rows(admin_client.get("/invoices/export.csv?q=BR-0001"))
    assert [r[1] for r in searched[1:]] == ["BR-0001"]
    none = admin_client.get("/invoices/export.csv?from=2030-01-01")
    assert none.status_code == 302


def test_csv_per_line_and_formula_injection(admin_client, db_module):
    _seller(db_module)
    _issue(admin_client, "/invoice-no/new",
           [{"description": "=HYPERLINK(\"http://x\")", "material_code": "M1", "qty": "2",
             "unit_price": "1.5", "po_no": "P1", "pos": "1"}],
           invoice_number="NO-9", consignee_name="Kunde AS", consignee_address="Oslo\nNorway")
    rows = _csv_rows(admin_client.get("/invoices/export.csv?lines=1"))
    head, line = rows[0], dict(zip(rows[0], rows[1]))
    assert "Ред №" in head and "Мярка (код)" in head and "Единична цена" in head
    assert line["Описание"].startswith("'=")
    assert line["Количество"] == "2" and line["Единична цена"] == "1.5" and line["Стойност"] == "3.00"
    assert line["Мярка (код)"] == "C62"


def test_accounting_excel_with_lines_sheet(admin_client, db_module):
    _seller(db_module)
    _brazil(admin_client)
    resp = admin_client.get("/invoices/export-accounting.xlsx?lines=1")
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.data))
    assert wb.sheetnames == ["Фактури", "Редове"]
    ws = wb["Фактури"]
    head = [c.value for c in ws[1]]
    row = dict(zip(head, [c.value for c in ws[2]]))
    assert row["Сума общо"] == pytest.approx(273.22) and isinstance(row["Сума общо"], float)
    assert row["Държава на купувача"] == "BR" and row["Валута"] == "EUR"
    assert not any("тегло" in str(h).lower() or "weight" in str(h).lower() for h in head)
    lines = list(wb["Редове"].iter_rows(values_only=True))
    assert len(lines) == 3 and lines[1][7] == "GLBK400002P0012"
    lhead = [str(h).lower() for h in lines[0]]
    assert not any("тегло" in h or "weight" in h for h in lhead)


def test_microinvest_export_default_and_admin_mapping(admin_client, employee_client, db_module):
    _seller(db_module)
    _brazil(admin_client)
    wb = load_workbook(io.BytesIO(admin_client.get("/invoices/export-microinvest.xlsx").data))
    ws = wb["Import"]
    head = [c.value for c in ws[1]]
    assert head[:4] == ["Дата", "Номер на документ", "Вид документ", "Контрагент"]
    row = dict(zip(head, [c.value for c in ws[2]]))
    assert row["Дебит"] == "411" and row["Кредит"] == "702" and row["Сума общо"] == pytest.approx(273.22)
    assert row["Основание"].startswith("чл. 28 ЗДДС")
    note = " ".join(str(c.value) for c in wb["Бележка"]["A"])
    assert "проверете с вашия счетоводител" in note.lower() and "Microinvest" in note
    # Служителят не вижда настройките (само администратор).
    assert employee_client.get("/settings/finance").status_code in (302, 403)
    resp = post_with_csrf(admin_client, "/settings/finance",
                          {"fin_mi_columns": "Док=number\nСума=total\nПартньор=buyer_name",
                           "fin_mi_debit": "4111", "fin_mi_credit": "703"},
                          csrf_source_url="/settings/finance", follow_redirects=True)
    assert "са запазени" in resp.data.decode()
    ws = load_workbook(io.BytesIO(admin_client.get("/invoices/export-microinvest.xlsx").data))["Import"]
    assert [c.value for c in ws[1]] == ["Док", "Сума", "Партньор"]
    bad = post_with_csrf(admin_client, "/settings/finance", {"fin_mi_columns": "Х=nonsense"},
                         csrf_source_url="/settings/finance", follow_redirects=True)
    assert "Непознати редове" in bad.data.decode()
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE '%счетоводство и имейл%'"
                       ).fetchone()[0] == 1
    con.close()


def test_invoices_list_has_accounting_export_menu(employee_client):
    body = employee_client.get("/invoices").data.decode()
    for url in ("/invoices/export.csv", "/invoices/export-accounting.xlsx",
                "/invoices/export-microinvest.xlsx", "/invoices/einvoice.zip"):
        assert 'formaction="%s"' % url in body
    assert 'name="lines"' in body


def test_brazil_exports_have_no_total_weight(admin_client, db_module):
    _seller(db_module)
    _brazil(admin_client)
    for url in ("/invoices/export.csv", "/invoices/export.csv?lines=1"):
        text = admin_client.get(url).data.decode("utf-8-sig").lower()
        assert "weight" not in text and "тегло" not in text and "2.21" not in text
    for url in ("/invoices/export-accounting.xlsx?lines=1", "/invoices/export-microinvest.xlsx"):
        wb = load_workbook(io.BytesIO(admin_client.get(url).data))
        cells = " ".join(str(c) for ws in wb for r in ws.iter_rows(values_only=True) for c in r)
        assert "weight" not in cells.lower() and "тегло" not in cells.lower()


# ---------------------------------------------------------------- имейл

class FakeSMTP:
    sent, instances = [], []
    fail = None

    def __init__(self, host, port, timeout=None, **kw):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        FakeSMTP.instances.append(self)
        if FakeSMTP.fail == "timeout":
            raise socket.timeout("timed out")

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if FakeSMTP.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        self.calls.append("quit")


@pytest.fixture
def fake_smtp(monkeypatch):
    import mail_export
    FakeSMTP.sent, FakeSMTP.instances, FakeSMTP.fail = [], [], None
    monkeypatch.setattr(mail_export.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail_export.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def _smtp_settings(db_module, **extra):
    con = db_module.get_db()
    db_module.save_settings(con, dict({"fin_smtp_host": "smtp.example.com", "fin_smtp_port": "587",
                                       "fin_smtp_security": "starttls",
                                       "fin_smtp_user": "office@bbs.example",
                                       "fin_smtp_timeout": "7"}, **extra))
    con.commit()
    con.close()


def _parse_eml(data):
    return email.message_from_bytes(data, policy=email.policy.default)


def test_compose_prefills_recipient_from_client_email_and_templates(admin_client, db_module):
    _seller(db_module)
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, email) VALUES (?, ?)",
                ("ABB ELETRIFICACAO LTDA", "compras@abb-br.example"))
    con.commit()
    con.close()
    doc_id = _brazil(admin_client)
    body = admin_client.get("/email/compose?ids=%d&from=%d" % (doc_id, doc_id)).data.decode()
    assert 'value="compras@abb-br.example"' in body
    assert "Invoice No. BR-0001 — BBS Bulgaria EOOD" in body        # EN по подразбиране
    assert "P.O. 4700200362" in body
    bg = admin_client.get("/email/compose?ids=%d&lang=bg" % doc_id).data.decode()
    assert "Фактура № BR-0001 — БиБиЕс България ЕООД" in bg and "поръчка 4700200362" in bg
    assert "Изпрати сега" not in body  # SMTP не е настроен → само .eml


def test_eml_draft_has_x_unsent_and_pdf_attachment_without_weight(admin_client, db_module):
    from pypdf import PdfReader
    _seller(db_module)
    doc_id = _brazil(admin_client)
    resp = post_with_csrf(admin_client, "/email/draft.eml",
                          {"ids": str(doc_id), "to": "a@b.example", "subject": "Тема",
                           "body": "Текст", "mode": "separate", "lang": "en"},
                          csrf_source_url="/email/compose?ids=%d" % doc_id)
    assert resp.status_code == 200 and resp.mimetype == "message/rfc822"
    assert "attachment" in resp.headers["Content-Disposition"] and ".eml" in resp.headers["Content-Disposition"]
    msg = _parse_eml(resp.data)
    assert msg["X-Unsent"] == "1" and msg["To"] == "a@b.example" and msg["Subject"] == "Тема"
    atts = list(msg.iter_attachments())
    assert len(atts) == 1 and atts[0].get_content_type() == "application/pdf"
    pdf = atts[0].get_content()
    assert pdf.startswith(b"%PDF")
    text = " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "BR-0001" in text and "total weight" not in text.lower()
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'имейл чернова (.eml)'"
                       ).fetchone()[0] == 1
    con.close()


def test_bundle_email_merged_and_separate(admin_client, db_module):
    from pypdf import PdfReader
    from conftest import issue_cmr
    _seller(db_module)
    inv = _brazil(admin_client)
    cmr = issue_cmr(admin_client, consignee_name="ABB ELETRIFICACAO LTDA")
    ids = "%d:3,%d:1" % (cmr, inv)
    sep = _parse_eml(admin_client.get("/email/draft.eml?ids=%s&mode=separate" % ids).data)
    sep_atts = list(sep.iter_attachments())
    assert len(sep_atts) == 2 and sep["X-Unsent"] == "1"
    assert "Shipment documents" in sep["Subject"]
    pages = sum(len(PdfReader(io.BytesIO(a.get_content())).pages) for a in sep_atts)
    merged = _parse_eml(admin_client.get("/email/draft.eml?ids=%s&mode=merged" % ids).data)
    m_atts = list(merged.iter_attachments())
    assert len(m_atts) == 1 and m_atts[0].get_filename().startswith("shipment_")
    assert len(PdfReader(io.BytesIO(m_atts[0].get_content())).pages) == pages
    # Бутонът в печата на пратката води към същото (включените документи).
    page = admin_client.get("/shipment/print?ids=%s&from=%d" % (ids, cmr)).data.decode()
    assert "/email/compose?ids=%d%%2C%d" % (cmr, inv) in page or \
        "/email/compose?ids=%d,%d" % (cmr, inv) in page


def test_invalid_recipient_on_draft_is_rejected(admin_client, db_module):
    _seller(db_module)
    doc_id = _brazil(admin_client)
    resp = admin_client.get("/email/draft.eml?ids=%d&to=not-an-email&subject=x&body=y" % doc_id)
    assert resp.status_code == 302 and "/email/compose" in resp.headers["Location"]


def test_smtp_sends_only_on_explicit_post_and_audits(admin_client, db_module, fake_smtp):
    _seller(db_module)
    _smtp_settings(db_module)
    doc_id = _brazil(admin_client)
    page = admin_client.get("/email/compose?ids=%d" % doc_id).data.decode()
    assert "Изпрати сега" in page and 'type="password"' in page
    admin_client.get("/email/draft.eml?ids=%d" % doc_id)
    assert fake_smtp.sent == [] and fake_smtp.instances == []   # нищо автоматично
    assert admin_client.get("/email/send?ids=%d" % doc_id).status_code == 405
    resp = post_with_csrf(admin_client, "/email/send",
                          {"ids": str(doc_id), "to": "x@client.example", "subject": "S",
                           "body": "B", "mode": "separate", "password": "secret-pw"},
                          csrf_source_url="/email/compose?ids=%d" % doc_id, follow_redirects=True)
    assert "Имейлът е изпратен до x@client.example" in resp.data.decode()
    assert len(fake_smtp.sent) == 1
    srv = fake_smtp.instances[0]
    assert (srv.host, srv.port, srv.timeout) == ("smtp.example.com", 587, 7)
    assert "starttls" in srv.calls and ("login", "office@bbs.example", "secret-pw") in srv.calls
    msg = fake_smtp.sent[0]
    assert msg["X-Unsent"] is None and msg["From"] == "office@bbs.example"
    assert len(list(msg.iter_attachments())) == 1
    con = db_module.get_db()
    rows = con.execute("SELECT action, detail FROM audit_log WHERE action LIKE '%имейл%'").fetchall()
    con.close()
    sent = [r for r in rows if r["action"] == "изпратен имейл с документи"]
    assert len(sent) == 1 and "x@client.example" in sent[0]["detail"]
    assert all("secret-pw" not in (r["detail"] or "") for r in rows)  # паролата не се пази
    settings = db_module.get_settings(db_module.get_db())
    assert "secret-pw" not in json.dumps(settings)


@pytest.mark.parametrize("fail,needle", [("auth", "отказа потребителя или паролата"),
                                         ("timeout", "не отговори за 7 секунди")])
def test_smtp_errors_are_clear(admin_client, db_module, fake_smtp, fail, needle):
    _seller(db_module)
    _smtp_settings(db_module)
    fake_smtp.fail = fail
    doc_id = _brazil(admin_client)
    resp = post_with_csrf(admin_client, "/email/send",
                          {"ids": str(doc_id), "to": "x@client.example", "subject": "S", "body": "B"},
                          csrf_source_url="/email/compose?ids=%d" % doc_id, follow_redirects=True)
    text = resp.data.decode()
    assert "Имейлът НЕ е изпратен" in text and needle in text
    assert fake_smtp.sent == []
    con = db_module.get_db()
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'неуспешно изпращане на имейл'"
                       ).fetchone()[0] == 1
    con.close()


def test_smtp_not_configured_and_bad_recipient(admin_client, db_module, fake_smtp):
    _seller(db_module)
    doc_id = _brazil(admin_client)
    resp = post_with_csrf(admin_client, "/email/send",
                          {"ids": str(doc_id), "to": "x@client.example", "subject": "S", "body": "B"},
                          csrf_source_url="/email/compose?ids=%d" % doc_id, follow_redirects=True)
    assert "SMTP сървърът не е настроен" in resp.data.decode()
    _smtp_settings(db_module)
    resp = post_with_csrf(admin_client, "/email/send",
                          {"ids": str(doc_id), "to": "nope", "subject": "S", "body": "B"},
                          csrf_source_url="/email/compose?ids=%d" % doc_id, follow_redirects=True)
    assert "валиден имейл адрес" in resp.data.decode()
    assert fake_smtp.instances == []


def test_smtp_send_uses_real_timeout_and_ssl(monkeypatch):
    """mail_export.smtp_send: SSL режим през SMTP_SSL, таймаутът се подава."""
    import mail_export
    seen = {}

    class Srv(FakeSMTP):
        def __init__(self, host, port, timeout=None, context=None):
            seen.update(host=host, port=port, timeout=timeout, ctx=context)
            super().__init__(host, port, timeout)
    FakeSMTP.sent, FakeSMTP.instances, FakeSMTP.fail = [], [], None
    monkeypatch.setattr(mail_export.smtplib, "SMTP_SSL", Srv)
    cfg = mail_export.smtp_config({"fin_smtp_host": "h", "fin_smtp_security": "ssl",
                                   "fin_smtp_timeout": "999"})
    assert cfg["port"] == 465 and cfg["timeout"] == 120
    mail_export.smtp_send(cfg, mail_export.build_message("a@b.c", "s", "b", [], draft=False))
    assert seen["timeout"] == 120 and seen["ctx"] is not None and len(FakeSMTP.sent) == 1
    with pytest.raises(mail_export.MailError) as exc:
        mail_export.smtp_send(dict(cfg, host=""), None)
    assert exc.value.code == "not_configured"


def test_finance_settings_page_validation(admin_client):
    page = admin_client.get("/settings/finance").data.decode()
    assert "Проверете с вашия счетоводител" in page and "Паролата НЕ се запазва" in page
    assert "/settings/finance" in admin_client.get("/settings").data.decode()
    resp = post_with_csrf(admin_client, "/settings/finance",
                          {"fin_smtp_port": "99999", "fin_smtp_from": "bad"},
                          csrf_source_url="/settings/finance", follow_redirects=True)
    text = resp.data.decode()
    assert "Портът трябва да е число" in text and "Невалиден имейл адрес на подателя" in text
