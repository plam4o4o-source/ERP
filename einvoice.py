# -*- coding: utf-8 -*-
"""Е-фактура по EN 16931 — UBL 2.1 XML по Peppol BIS Billing 3.0.

Заявка (06.11.2026, FIN): „Е-фактура по EN 16931“. Само файлов износ (без
изпращане по Peppol мрежата). Фактурите на програмата са в евро и без ДДС:
  * K — вътреобщностна доставка 0 % (купувач с ДДС № от ЕС, доставка до
    друга държава от ЕС; изисква се и датата на доставка, и държавата);
  * G — износ извън ЕС 0 % (Бразилия, Норвегия, ОАЕ…).
Доставка в България (S 20 %) НЕ се поддържа — отказва се с ясна грешка.

Редът на елементите следва XSD последователностите на UBL 2.1 (Invoice,
Party, Item…) — иначе файлът не минава схемата. Никакво тегло (Бразилия е
без „Total weight“)."""
import datetime
import decimal
import xml.etree.ElementTree as ET  # nosec B405 -- само ГЕНЕРИРАНЕ, не се чете чужд XML

import fin_data

NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CUSTOMIZATION_ID = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"

#: Peppol EAS (електронен адрес по ДДС номер) за държавите от ЕС + Норвегия.
EAS_BY_COUNTRY = {
    "AT": "9914", "BE": "9925", "BG": "9926", "CY": "9928", "CZ": "9929", "DE": "9930",
    "EE": "9931", "GR": "9933", "HR": "9934", "IE": "9935", "IT": "0211", "LT": "9937",
    "LU": "9938", "LV": "9939", "MT": "9943", "NL": "9944", "PL": "9945", "PT": "9946",
    "RO": "9947", "SI": "9949", "SK": "9950", "FR": "9957", "HU": "9910", "ES": "9920",
    "DK": "0198", "FI": "0213",
}

_CENT = decimal.Decimal("0.01")


class EInvoiceError(fin_data.FinDataError):
    """Фактурата не може да се изнесе като е-фактура (code — за превода)."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _money(value):
    return str(decimal.Decimal(value).quantize(_CENT, rounding=decimal.ROUND_HALF_UP))


def _cbc(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, "{%s}%s" % (NS_CBC, tag), {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def _cac(parent, tag):
    return ET.SubElement(parent, "{%s}%s" % (NS_CAC, tag))


def _address(parent, tag, addr, country):
    a = _cac(parent, tag)
    for key, name in (("street", "StreetName"), ("additional", "AdditionalStreetName"),
                      ("city", "CityName"), ("postcode", "PostalZone")):
        if addr.get(key):
            _cbc(a, name, addr[key])
    c = _cac(a, "Country")
    _cbc(c, "IdentificationCode", country)
    return a


def _tax_scheme(parent):
    ts = _cac(parent, "TaxScheme")
    _cbc(ts, "ID", "VAT")


def buyer_endpoint(buyer):
    """(схема, стойност) за BT-49 или None: имейл (EM) → ДДС № с EAS по
    държавата → норвежки организационен № (0192)."""
    if fin_data.valid_email(buyer.get("email")):
        return "EM", buyer["email"]
    vat = buyer.get("vat") or ""
    country = fin_data.vat_country(vat)
    if country in EAS_BY_COUNTRY:
        return EAS_BY_COUNTRY[country], vat
    if buyer.get("country") == "NO":
        digits = "".join(ch for ch in vat if ch.isdigit())
        if len(digits) == 9:
            return "0192", digits
    return None


def seller_endpoint(seller, scheme=""):
    scheme = (scheme or "").strip().upper()
    if scheme == "EM" and fin_data.valid_email(seller.get("email")):
        return "EM", seller["email"]
    if seller.get("vat"):
        return (scheme if scheme and scheme != "EM" else
                EAS_BY_COUNTRY.get(seller.get("country"), "9926")), seller["vat"]
    if fin_data.valid_email(seller.get("email")):
        return "EM", seller["email"]
    return None


def check_record(rec):
    """Блокиращите грешки (EInvoiceError) преди сглобяването."""
    if not rec["lines"]:
        raise EInvoiceError("no_lines")
    if not rec["seller"]["vat"]:
        raise EInvoiceError("seller_vat")
    if not rec["seller"]["name"]:
        raise EInvoiceError("seller_name")
    if not rec["buyer"]["name"]:
        raise EInvoiceError("buyer_name")
    problem = rec["vat_problem"]
    if problem:
        raise EInvoiceError(problem, rec["consignee"]["country"] or rec["buyer"]["country"])
    if not rec["buyer"]["country"]:
        raise EInvoiceError("country")


def build_invoice(rec, endpoint_scheme=""):
    """UBL 2.1 Invoice (bytes, UTF-8) + списък с предупреждения."""
    check_record(rec)
    warnings = []
    seller, buyer, cons = rec["seller"], rec["buyer"], rec["consignee"]
    cat = rec["vat_category"]
    ET.register_namespace("", NS_INVOICE)
    ET.register_namespace("cac", NS_CAC)
    ET.register_namespace("cbc", NS_CBC)
    root = ET.Element("{%s}Invoice" % NS_INVOICE)
    _cbc(root, "CustomizationID", CUSTOMIZATION_ID)
    _cbc(root, "ProfileID", PROFILE_ID)
    _cbc(root, "ID", rec["number"])
    _cbc(root, "IssueDate", rec["date"])
    days = fin_data.payment_days(rec["terms_payment"])
    if days is not None:
        try:
            issue = datetime.date.fromisoformat(rec["date"])
            _cbc(root, "DueDate", (issue + datetime.timedelta(days=days)).isoformat())
        except ValueError:
            pass
    _cbc(root, "InvoiceTypeCode", "380")
    notes = []
    if rec["description"]:
        notes.append(rec["description"])
    if len(rec["orders"]) > 1:
        notes.append("P.O. NO: " + ", ".join(rec["orders"]))
    if rec["terms_delivery"]:
        notes.append("Terms of delivery: " + rec["terms_delivery"])
    if rec["lc_number"]:
        notes.append("Letter of credit: " + rec["lc_number"])
    if notes:
        _cbc(root, "Note", " | ".join(notes))
    _cbc(root, "DocumentCurrencyCode", fin_data.CURRENCY)
    # PEPPOL-EN16931-R003: BuyerReference или OrderReference — подаваме и двете при PO.
    _cbc(root, "BuyerReference", rec["orders"][0] if rec["orders"] else rec["number"])
    if rec["orders"]:
        order = _cac(root, "OrderReference")
        _cbc(order, "ID", rec["orders"][0])

    # --- продавач (BG-4)
    sp = _cac(_cac(root, "AccountingSupplierParty"), "Party")
    ep = seller_endpoint(seller, endpoint_scheme)
    if ep:
        _cbc(sp, "EndpointID", ep[1], schemeID=ep[0])
    _cbc(_cac(sp, "PartyName"), "Name", seller["name"])
    _address(sp, "PostalAddress", seller, seller["country"])
    pts = _cac(sp, "PartyTaxScheme")
    _cbc(pts, "CompanyID", seller["vat"])
    _tax_scheme(pts)
    le = _cac(sp, "PartyLegalEntity")
    _cbc(le, "RegistrationName", seller["legal_name"] or seller["name"])
    if seller["eik"]:
        _cbc(le, "CompanyID", seller["eik"])
    if seller["email"] or seller["phone"]:
        ct = _cac(sp, "Contact")
        if seller["phone"]:
            _cbc(ct, "Telephone", seller["phone"])
        if seller["email"]:
            _cbc(ct, "ElectronicMail", seller["email"])

    # --- купувач (BG-7)
    bp = _cac(_cac(root, "AccountingCustomerParty"), "Party")
    ep = buyer_endpoint(buyer)
    if ep:
        _cbc(bp, "EndpointID", ep[1], schemeID=ep[0])
    else:
        warnings.append("buyer_endpoint")
    _cbc(_cac(bp, "PartyName"), "Name", buyer["name"])
    _address(bp, "PostalAddress", buyer, buyer["country"])
    # BR-CO-09: ДДС № в PartyTaxScheme започва с код на държава — иначе
    # (CNPJ, TRN…) отива като идентификатор на юридическото лице.
    if buyer["vat"] and fin_data.vat_country(buyer["vat"]):
        pts = _cac(bp, "PartyTaxScheme")
        _cbc(pts, "CompanyID", buyer["vat"])
        _tax_scheme(pts)
    le = _cac(bp, "PartyLegalEntity")
    _cbc(le, "RegistrationName", buyer["name"])
    if buyer["vat"] and not fin_data.vat_country(buyer["vat"]):
        _cbc(le, "CompanyID", buyer["vat"])
    if buyer.get("email"):
        _cbc(_cac(bp, "Contact"), "ElectronicMail", buyer["email"])

    # --- доставка (BG-13): BR-IC-11/12 — дата и държава на доставката
    dl = _cac(root, "Delivery")
    _cbc(dl, "ActualDeliveryDate", rec["date"])
    loc = _cac(dl, "DeliveryLocation")
    _address(loc, "Address", cons, cons["country"] or buyer["country"])
    if cons["name"]:
        _cbc(_cac(_cac(dl, "DeliveryParty"), "PartyName"), "Name", cons["name"])

    # --- плащане (BG-16/17)
    if seller["iban"]:
        pm = _cac(root, "PaymentMeans")
        _cbc(pm, "PaymentMeansCode", "30", name="Credit transfer")
        _cbc(pm, "PaymentID", rec["number"])
        acc = _cac(pm, "PayeeFinancialAccount")
        _cbc(acc, "ID", seller["iban"])
        _cbc(acc, "Name", seller["legal_name"] or seller["name"])
        if seller["bic"]:
            _cbc(_cac(acc, "FinancialInstitutionBranch"), "ID", seller["bic"])
    else:
        warnings.append("seller_iban")
    if rec["terms_payment"]:
        _cbc(_cac(root, "PaymentTerms"), "Note", rec["terms_payment"])

    # --- ДДС (BG-23) и суми (BG-22)
    cur = {"currencyID": fin_data.CURRENCY}
    tt = _cac(root, "TaxTotal")
    _cbc(tt, "TaxAmount", "0.00", **cur)
    sub = _cac(tt, "TaxSubtotal")
    _cbc(sub, "TaxableAmount", _money(rec["net"]), **cur)
    _cbc(sub, "TaxAmount", "0.00", **cur)
    tc = _cac(sub, "TaxCategory")
    _cbc(tc, "ID", cat)
    _cbc(tc, "Percent", "0")
    _cbc(tc, "TaxExemptionReasonCode", fin_data.VAT_CATEGORIES[cat]["code"])
    _cbc(tc, "TaxExemptionReason", fin_data.VAT_CATEGORIES[cat]["reason"])
    _tax_scheme(tc)
    lmt = _cac(root, "LegalMonetaryTotal")
    _cbc(lmt, "LineExtensionAmount", _money(rec["net"]), **cur)
    _cbc(lmt, "TaxExclusiveAmount", _money(rec["net"]), **cur)
    _cbc(lmt, "TaxInclusiveAmount", _money(rec["total"]), **cur)
    _cbc(lmt, "PayableAmount", _money(rec["total"]), **cur)

    # --- редове (BG-25)
    origin = fin_data.country_code(rec["country_origin"])
    for ln in rec["lines"]:
        il = _cac(root, "InvoiceLine")
        _cbc(il, "ID", ln["no"])
        _cbc(il, "InvoicedQuantity", ln["qty_text"], unitCode=ln["unit_code"])
        _cbc(il, "LineExtensionAmount", _money(ln["total"]), **cur)
        if ln["pos"]:
            _cbc(_cac(il, "OrderLineReference"), "LineID", ln["pos"])
        item = _cac(il, "Item")
        desc_parts = [p for p in (ln["description"],
                                  ("P.O. %s" % ln["po_no"]) if ln["po_no"] else "",
                                  ("Pallet %s" % ln["pallet_no"]) if ln["pallet_no"] else "") if p]
        if desc_parts:
            _cbc(item, "Description", " · ".join(desc_parts))
        _cbc(item, "Name", (ln["description"] or ln["material_code"] or "Item")[:200])
        if ln["material_code"]:
            _cbc(_cac(item, "SellersItemIdentification"), "ID", ln["material_code"])
        if origin:
            _cbc(_cac(item, "OriginCountry"), "IdentificationCode", origin)
        if ln["hs_code"]:
            _cbc(_cac(item, "CommodityClassification"), "ItemClassificationCode",
                 ln["hs_code"], listID="HS")
        ctc = _cac(item, "ClassifiedTaxCategory")
        _cbc(ctc, "ID", cat)
        _cbc(ctc, "Percent", "0")
        _tax_scheme(ctc)
        price = _cac(il, "Price")
        _cbc(price, "PriceAmount", ln["price_text"], **cur)
    if rec["skipped_lines"]:
        warnings.append("skipped_lines")
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8"), warnings


def einvoice_filename(rec):
    import client_export
    return "einvoice_%s.xml" % client_export.sanitize_number_stub(rec["number"])
