# -*- coding: utf-8 -*-
"""Данните на издадена фактура за счетоводството и е-фактурата.

Заявка (06.11.2026, FIN): „Износ на фактурите към счетоводството“, „Е-фактура
по EN 16931“, „Подготовка за SAF-T“. Тук е само сглобяването на данните (без
Flask маршрути): продавач (от Настройки), купувач и получател (от фактурата +
адресните книги), държави, ДДС номера, ДДС категория (K/G), редове и суми.
Общо за CSV/Excel/Microinvest (finance_export.py) и UBL (einvoice.py), за да
не се разминат.

Фактурите са САМО в евро и без ДДС (износ/ВОД) — сумата без ДДС = общо.
Теглото НЕ се изнася никъде (фактурата за Бразилия е без „Total weight“)."""
import decimal
import re
import unicodedata

import db
from appcore import _parse_decimal_exact, invoice_row_total, safe_json_data

CURRENCY = "EUR"

#: ДДС префиксите на ЕС (Гърция е „EL“ в ДДС номера, „GR“ като държава).
EU_COUNTRIES = frozenset((
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE",
    "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"))
_VAT_PREFIX_TO_COUNTRY = {"EL": "GR"}

#: Подразбираща се държава на получателя по вида фактура (бланките са за тези държави).
DOC_TYPE_COUNTRY = {"invoice_br": "BR", "invoice_no": "NO", "invoice_dubai": "AE"}

#: Имена на държави (EN/BG/местно, без ударения, малки букви) → ISO 3166-1 alpha-2.
_COUNTRY_NAMES = {
    "AT": ("austria", "osterreich", "австрия"),
    "BE": ("belgium", "belgique", "belgie", "белгия"),
    "BG": ("bulgaria", "българия", "republic of bulgaria"),
    "CY": ("cyprus", "кипър"),
    "CZ": ("czech republic", "czechia", "cesko", "чехия"),
    "DE": ("germany", "deutschland", "германия"),
    "DK": ("denmark", "danmark", "дания"),
    "EE": ("estonia", "eesti", "естония"),
    "ES": ("spain", "espana", "испания"),
    "FI": ("finland", "suomi", "финландия"),
    "FR": ("france", "франция"),
    "GR": ("greece", "hellas", "гърция"),
    "HR": ("croatia", "hrvatska", "хърватия"),
    "HU": ("hungary", "magyarorszag", "унгария"),
    "IE": ("ireland", "ирландия"),
    "IT": ("italy", "italia", "италия"),
    "LT": ("lithuania", "lietuva", "литва"),
    "LU": ("luxembourg", "люксембург"),
    "LV": ("latvia", "latvija", "латвия"),
    "MT": ("malta", "малта"),
    "NL": ("netherlands", "the netherlands", "nederland", "holland", "нидерландия", "холандия"),
    "PL": ("poland", "polska", "полша"),
    "PT": ("portugal", "португалия"),
    "RO": ("romania", "румъния"),
    "SE": ("sweden", "sverige", "швеция"),
    "SI": ("slovenia", "slovenija", "словения"),
    "SK": ("slovakia", "slovensko", "словакия"),
    "NO": ("norway", "norge", "noreg", "норвегия"),
    "BR": ("brazil", "brasil", "бразилия"),
    "AE": ("united arab emirates", "uae", "u.a.e.", "dubai", "abu dhabi", "sharjah", "оае",
           "обединени арабски емирства", "дубай"),
    "CH": ("switzerland", "schweiz", "suisse", "svizzera", "швейцария"),
    "GB": ("united kingdom", "great britain", "england", "scotland", "uk", "обединено кралство",
           "великобритания"),
    "TR": ("turkey", "turkiye", "турция"),
    "RS": ("serbia", "srbija", "сърбия"),
    "MK": ("north macedonia", "macedonia", "северна македония", "македония"),
    "UA": ("ukraine", "украйна"),
    "US": ("united states", "usa", "u.s.a.", "сащ"),
    "CN": ("china", "китай"),
    "IN": ("india", "индия"),
    "SA": ("saudi arabia", "саудитска арабия"),
    "QA": ("qatar", "катар"),
    "EG": ("egypt", "египет"),
}


def _fold(text):
    """Малки букви без ударения (кирилицата не се пипа — „й“ остава „й“)."""
    out, base = [], ""
    for ch in unicodedata.normalize("NFD", str(text or "").lower()):
        if unicodedata.combining(ch):
            if base and ord(base) < 0x0250:  # ударение върху латинска буква се маха
                continue
        else:
            base = ch
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


_NAME_TO_CODE = {}
for _code, _names in _COUNTRY_NAMES.items():
    for _name in _names:
        _NAME_TO_CODE[_fold(_name)] = _code
_KNOWN_CODES = frozenset(_COUNTRY_NAMES)


def country_code(text):
    """„Italy“, „Италия“, „IT“, „Brasil“ → ISO код; „“ ако не е разпознат."""
    raw = " ".join(str(text or "").split()).strip(" .,;")
    if not raw:
        return ""
    if len(raw) == 2 and raw.upper() in _KNOWN_CODES | EU_COUNTRIES:
        return raw.upper()
    return _NAME_TO_CODE.get(_fold(raw), "")


_WORD_SPLIT_RE = re.compile(r"[\s,;/()\-–]+")


def country_from_text(text):
    """Държавата в свободен адрес: първо последният ред (обичайното място),
    после всеки ред отзад напред — цял ред или дума/двойка думи в него."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        code = country_code(line)
        if code:
            return code
        words = [w for w in _WORD_SPLIT_RE.split(line) if w]
        for size in (3, 2, 1):
            for i in range(len(words) - size, -1, -1):
                chunk = " ".join(words[i:i + size])
                code = _NAME_TO_CODE.get(_fold(chunk), "")
                # Двубуквен код сам по себе в адреса („SP“ в „Sorocaba - SP“) не се приема.
                if code:
                    return code
    return ""


#: „VAT No: IT 01234567890“, „P.IVA IT…“, „ДДС № BG…“, „TRN …“, „Org.nr …“, „CNPJ …“.
_VAT_LABEL_RE = re.compile(
    r"(?:\bVAT(?:\s*(?:No|Nr|Number|ID|Reg(?:istration)?))?|\bUSt-?Id(?:Nr)?|\bP\.?\s?IVA|"
    r"\bTVA|\bNIF|\bCIF|\bДДС(?:\s*№)?|\bTRN|\bOrg\.?\s*(?:nr|no)|\bMVA|\bCNPJ|\bTax\s*ID)"
    r"\s*[.:#№]*\s*([A-Z]{0,2}[\s.\-/]?[0-9][0-9A-Z\s.\-/]{3,20})", re.IGNORECASE)


def normalize_vat(value):
    """„it 0123 4567 890“ → „IT01234567890“ (без интервали, точки, тирета)."""
    return re.sub(r"[\s.\-/]", "", str(value or "")).upper()


def vat_from_text(text):
    """Първият ДДС/данъчен номер, изписан с етикет в свободния адрес."""
    for line in str(text or "").splitlines():
        m = _VAT_LABEL_RE.search(line)
        if m:
            vat = normalize_vat(m.group(1))
            if sum(ch.isdigit() for ch in vat) >= 4:
                return vat
    return ""


def vat_country(vat):
    """ISO кодът от префикса на ДДС номера („EL…“ → „GR“) или „“."""
    prefix = normalize_vat(vat)[:2]
    if len(prefix) == 2 and prefix.isalpha():
        code = _VAT_PREFIX_TO_COUNTRY.get(prefix, prefix)
        if code in EU_COUNTRIES or code in _KNOWN_CODES:
            return code
    return ""


def is_eu_vat(vat):
    return vat_country(vat) in EU_COUNTRIES


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def email_from_text(text):
    m = _EMAIL_RE.search(str(text or ""))
    return m.group(0) if m else ""


def valid_email(value):
    value = str(value or "").strip()
    return bool(value) and _EMAIL_RE.fullmatch(value) is not None


# ---------------------------------------------------------------- адресни книги

def _columns(con, table):
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}  # nosec B608 -- таблицата е литерал
    except Exception:  # pylint: disable=broad-except
        return set()


def _book_lookup(con, names, cache=None):
    """Като _book_lookup_uncached, с кеш за един износ (много фактури на
    един и същ клиент — без повторни заявки към адресните книги)."""
    if cache is None:
        return _book_lookup_uncached(con, names)
    key = tuple(db.client_match_key(n) for n in names if str(n or "").strip())
    if key not in cache:
        cache[key] = _book_lookup_uncached(con, names)
    return dict(cache[key])


def _book_lookup_uncached(con, names):
    """Данните от адресните книги за първото съвпадащо име: клиент за
    фактури (и свързания му клиент — колоната client_id на група BOOK, ако я
    има) и общата адресна книга (clients: vat, country, email). Колоните се
    проверяват динамично — по-стара база без тях не гърми."""
    found = {"vat": "", "country": "", "email": "", "eik": ""}
    keys = [db.client_match_key(n) for n in names if str(n or "").strip()]
    if not keys:
        return found
    inv_cols = _columns(con, "invoice_clients")
    cli_cols = _columns(con, "clients")
    client_ids = []
    if inv_cols:
        wanted = [c for c in ("name", "billing_name", "delivery_name") if c in inv_cols]
        extra = [c for c in ("client_id", "vies_vat", "vies_valid", "email", "vat", "country")
                 if c in inv_cols]
        for key in keys:
            for col in wanted:
                row = con.execute(
                    "SELECT %s FROM invoice_clients WHERE ci_client_key(%s) = ? LIMIT 1"  # nosec B608 -- колоните са от PRAGMA
                    % (", ".join(["id"] + extra), col), (key,)).fetchone()
                if row is None:
                    continue
                row = dict(row)
                if row.get("vies_vat") and row.get("vies_valid") in (1, None):
                    found["vat"] = found["vat"] or normalize_vat(row["vies_vat"])
                for k in ("vat", "email"):
                    if row.get(k):
                        found[k] = found[k] or str(row[k]).strip()
                if row.get("country"):
                    found["country"] = found["country"] or country_code(row["country"])
                if row.get("client_id"):
                    client_ids.append(row["client_id"])
                break
    if cli_cols:
        cols = [c for c in ("vat", "country", "email", "eik") if c in cli_cols]
        rows = []
        for cid in client_ids:
            r = con.execute("SELECT %s FROM clients WHERE id = ?" % ", ".join(cols),  # nosec B608 -- колоните са от PRAGMA
                            (cid,)).fetchone()
            if r is not None:
                rows.append(r)
        for key in keys:
            r = con.execute("SELECT %s FROM clients WHERE ci_client_key(name) = ? LIMIT 1"  # nosec B608
                            % ", ".join(cols), (key,)).fetchone()
            if r is not None:
                rows.append(r)
        for r in rows:
            r = dict(r)
            for k in ("vat", "email", "eik"):
                if r.get(k) and not found[k]:
                    found[k] = normalize_vat(r[k]) if k == "vat" else str(r[k]).strip()
            if r.get("country") and not found["country"]:
                found["country"] = country_code(r["country"])
    return found


def recipient_email(con, row, data):
    """Имейлът на клиента на документа — за „Отвори като имейл“: от полетата
    на документа, от адресните книги по името, от адреса (свободен текст)."""
    for key in ("consignee_email", "receiver_email", "client_email", "billto_email", "email"):
        if valid_email(data.get(key)):
            return str(data[key]).strip()
    names = [row["client_name"] if "client_name" in row.keys() else ""]
    names += [data.get(k) for k in ("billto_name", "consignee_name", "receiver_name",
                                     "client_name", "dest_name")]
    email = _book_lookup(con, [n for n in names if n]).get("email")
    if valid_email(email):
        return email
    for key in ("billto_address", "consignee_address", "receiver_address", "client_address"):
        email = email_from_text(data.get(key))
        if email:
            return email
    return ""


# ---------------------------------------------------------------- продавач / купувач

def _first(*values):
    for v in values:
        text = " ".join(str(v or "").split())
        if text:
            return text
    return ""


def seller_info(settings, data=None):
    """Продавачът от Настройки (английската версия, ако е попълнена), с
    резерва — данните, отпечатани на самата фактура."""
    data = data or {}
    s = settings
    vat = normalize_vat(_first(s.get("sender_vat"), data.get("sender_vat")))
    if vat and vat.isdigit():
        vat = "BG" + vat
    country = (country_code(_first(s.get("sender_country_en"), s.get("sender_country")))
               or vat_country(vat) or "BG")
    iban = normalize_vat(s.get("sender_iban"))
    bic = normalize_vat(s.get("sender_swift"))
    if not iban:
        m = re.search(r"IBAN\s*:?\s*([A-Z]{2}\d{2}[A-Z0-9 ]{10,32}?)(?=\s*(?:SWIFT|BIC|/|$))",
                      str(data.get("bank_details") or ""), re.IGNORECASE)
        iban = normalize_vat(m.group(1)) if m else ""
    if not bic:
        m = re.search(r"(?:SWIFT|BIC)\s*:?\s*([A-Z0-9]{8,11})", str(data.get("bank_details") or ""),
                      re.IGNORECASE)
        bic = m.group(1).upper() if m else ""
    return {
        "name": _first(s.get("sender_name_en"), s.get("sender_name"), data.get("sender_name")),
        "legal_name": _first(s.get("sender_name"), s.get("sender_name_en"), data.get("sender_name")),
        "vat": vat,
        "eik": normalize_vat(s.get("sender_eik")),
        "street": _first(s.get("sender_address_en"), s.get("sender_address"),
                         data.get("sender_address")),
        "city": _first(s.get("sender_city_en"), s.get("sender_city")),
        "postcode": _first(s.get("sender_postcode_en"), s.get("sender_postcode")),
        "country": country,
        "email": _first(s.get("sender_email")),
        "phone": _first(s.get("sender_phone"), data.get("sender_phone")),
        "iban": iban,
        "bic": bic,
        "bank": _first(s.get("sender_bank")),
    }


_POSTCODE_RE = re.compile(r"\b(?:[A-Z]{1,2}-?)?\d{4,6}(?:-\d{3,4})?\b")
_SKIP_LINE_RE = re.compile(r"^\s*(?:tel|phone|fax|e-?mail|тел|факс)\b|@", re.IGNORECASE)


def split_address(text, country):
    """Свободният многоредов адрес → улица, допълнение, град, пощенски код
    (държавата и редовете с ДДС/телефон/имейл се махат)."""
    lines = []
    for ln in str(text or "").splitlines():
        ln = ln.strip().strip(",")
        if not ln or _SKIP_LINE_RE.search(ln) or _VAT_LABEL_RE.search(ln):
            continue
        if country and country_code(ln) == country:
            continue
        lines.append(ln)
    out = {"street": "", "additional": "", "city": "", "postcode": ""}
    if not lines:
        return out
    if len(lines) == 1:
        out["street"] = lines[0]
        return out
    out["street"] = lines[0]
    if len(lines) > 2:
        out["additional"] = ", ".join(lines[1:-1])
    city = lines[-1]
    m = _POSTCODE_RE.search(city)
    if m:
        out["postcode"] = m.group(0)
        city = (city[:m.start()] + city[m.end():]).strip(" ,-")
    out["city"] = city
    return out


class FinDataError(ValueError):
    """Липсват данни, без които документът за счетоводството/е-фактурата е невалиден."""


def parties(con, row, data, cache=None):
    """Купувач (Bill To, иначе получателят) и получател (доставка) — име,
    адрес, държава, ДДС номер, имейл. Държавата: адресните книги → адресът
    (свободен текст) → префиксът на ДДС номера → подразбиращата се по вида
    фактура (Бразилия/Норвегия/Дубай)."""
    doc_type = row["doc_type"]
    cons_name = _first(data.get("consignee_name"))
    cons_addr = str(data.get("consignee_address") or "")
    buyer_name = _first(data.get("billto_name"), cons_name)
    buyer_addr = str(data.get("billto_address") or "") if _first(data.get("billto_name")) else cons_addr
    book_buyer = _book_lookup(con, [buyer_name], cache)
    book_cons = _book_lookup(con, [cons_name], cache) if cons_name != buyer_name else book_buyer
    buyer_vat = normalize_vat(_first(data.get("billto_vat"), data.get("buyer_vat"),
                                     vat_from_text(buyer_addr), book_buyer["vat"]))
    if not _first(data.get("billto_name")) and not buyer_vat:
        buyer_vat = normalize_vat(_first(data.get("consignee_vat"), vat_from_text(cons_addr)))
    cons_country = (country_code(data.get("consignee_country")) or country_from_text(cons_addr)
                    or book_cons["country"] or DOC_TYPE_COUNTRY.get(doc_type, ""))
    buyer_country = (country_code(data.get("billto_country")) or country_from_text(buyer_addr)
                     or book_buyer["country"] or vat_country(buyer_vat) or cons_country)
    buyer = {"name": buyer_name, "address": buyer_addr, "country": buyer_country,
             "vat": buyer_vat, "eik": book_buyer["eik"],
             "email": _first(email_from_text(buyer_addr), book_buyer["email"])}
    buyer.update(split_address(buyer_addr, buyer_country))
    consignee = {"name": cons_name, "address": cons_addr, "country": cons_country}
    consignee.update(split_address(cons_addr, cons_country))
    return buyer, consignee


#: Основание за 0 % ДДС (EN 16931 BT-120/121; кодовете VATEX — CEF списък).
VAT_CATEGORIES = {
    "G": {"code": "VATEX-EU-G",
          "reason": "Export outside the EU / Износ извън ЕС (чл. 28 ЗДДС; Art. 146 Directive 2006/112/EC)",
          "basis_bg": "чл. 28 ЗДДС — износ"},
    "K": {"code": "VATEX-EU-IC",
          "reason": "Intra-Community supply / Вътреобщностна доставка (чл. 7 ЗДДС; Art. 138 Directive 2006/112/EC)",
          "basis_bg": "чл. 7 ЗДДС — ВОД"},
}


def vat_category(buyer, consignee):
    """(категория, грешка): K — доставка до друга държава от ЕС за купувач с
    ДДС номер от ЕС; G — доставка извън ЕС. Доставка в България не е
    поддържана (фактурите на програмата са без ДДС)."""
    dest = consignee["country"] or buyer["country"]
    if not dest:
        return "", "country"
    if dest == "BG":
        return "", "domestic"
    if dest in EU_COUNTRIES:
        if not is_eu_vat(buyer["vat"]):
            return "K", "buyer_vat"
        return "K", ""
    return "G", ""


# ---------------------------------------------------------------- редове и суми

_UNIT_CODES = {
    "C62": ("бр", "бр.", "броя", "брой", "pcs", "pc", "pce", "piece", "pieces", "ea", "each",
            "unit", "units", "c62", "h87", "шт"),
    "KGM": ("kg", "кг", "kgs", "kilogram", "kgm"),
    "GRM": ("g", "гр", "gram", "grm"),
    "TNE": ("t", "тон", "тона", "ton", "tonne", "tne"),
    "MTR": ("m", "м", "метър", "метра", "meter", "metre", "mtr"),
    "MTK": ("m2", "м2", "кв.м", "sqm", "mtk"),
    "MTQ": ("m3", "м3", "куб.м", "cbm", "mtq"),
    "LTR": ("l", "л", "литър", "litre", "liter", "ltr"),
    "SET": ("set", "sets", "к-т", "комплект", "компл.", "set"),
    "PR": ("pair", "pairs", "чифт", "pr"),
    "XPX": ("pallet", "pallets", "палет", "палета", "plt", "xpx"),
    "XBX": ("box", "boxes", "кашон", "кашона", "кутия", "xbx"),
    "HUR": ("h", "hour", "hours", "час", "часа", "hur"),
}
_UNIT_LOOKUP = {name: code for code, names in _UNIT_CODES.items() for name in names}
DEFAULT_UNIT_CODE = "C62"


def unit_code(unit):
    """Мерната единица от реда → код по UN/ECE Rec 20/21 (C62 = брой)."""
    text = " ".join(str(unit or "").lower().split())
    if not text:
        return DEFAULT_UNIT_CODE
    if text.upper() in _UNIT_CODES:
        return text.upper()
    return _UNIT_LOOKUP.get(text, _UNIT_LOOKUP.get(text.rstrip("."), DEFAULT_UNIT_CODE))


def _plain(value):
    """Decimal → текст без експонента и без излишни нули („20“, „13.66“)."""
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def invoice_lines(data):
    """Годните редове (количество и цена) с номера по ред на бланката."""
    lines, skipped = [], []
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
    for idx, it in enumerate(items, 1):
        total = invoice_row_total(it)
        qty = _parse_decimal_exact(it.get("qty"))
        price = _parse_decimal_exact(it.get("unit_price"))
        if not total or qty is None or price is None:
            if any(str(it.get(k) or "").strip() for k in ("material_code", "description", "qty")):
                skipped.append(idx)
            continue
        lines.append({
            "no": idx, "hs_code": _first(it.get("hs_code")), "po_no": _first(it.get("po_no")),
            "pos": _first(it.get("pos")), "material_code": _first(it.get("material_code")),
            "description": _first(it.get("description"), data.get("description")),
            "pallet_no": _first(it.get("pallet_no")),
            "qty": qty, "qty_text": _plain(qty), "unit": _first(it.get("unit")),
            "unit_code": unit_code(it.get("unit")),
            "price": price, "price_text": _plain(price),
            "total": decimal.Decimal(total), "total_text": total,
        })
    return lines, skipped


def order_numbers(data):
    out = []
    for it in (data.get("items") or []):
        if isinstance(it, dict):
            po = _first(it.get("po_no"))
            if po and po not in out:
                out.append(po)
    return out


def doc_date(row, data):
    raw = _first(data.get("doc_date"), str(row["created_at"] or "")[:10])
    return raw[:10]


_DAYS_RE = re.compile(r"(\d{1,3})\s*(?:days?|дни|ден|d\b|gün)", re.IGNORECASE)


def payment_days(terms):
    m = _DAYS_RE.search(str(terms or ""))
    return int(m.group(1)) if m else None


def invoice_record(con, row, settings=None, data=None, cache=None):
    """Един речник с всичко нужно за износа/е-фактурата на ЕДНА фактура."""
    data = safe_json_data(row["data"]) if data is None else data
    settings = db.get_settings(con) if settings is None else settings
    buyer, consignee = parties(con, row, data, cache)
    category, problem = vat_category(buyer, consignee)
    lines, skipped = invoice_lines(data)
    total = sum((ln["total"] for ln in lines), decimal.Decimal("0")).quantize(
        decimal.Decimal("0.01"))
    title = db.DOC_TYPES.get(row["doc_type"], {}).get("title", row["doc_type"])
    return {
        "id": row["id"], "doc_type": row["doc_type"], "type_title": title,
        "number": row["number"], "date": doc_date(row, data),
        "seller": seller_info(settings, data), "buyer": buyer, "consignee": consignee,
        "currency": CURRENCY, "net": total, "vat": decimal.Decimal("0.00"), "total": total,
        "vat_category": category, "vat_problem": problem,
        "vat_reason": VAT_CATEGORIES.get(category, {}).get("reason", ""),
        "vat_basis": VAT_CATEGORIES.get(category, {}).get("basis_bg", ""),
        "terms_payment": _first(data.get("terms_payment")),
        "terms_delivery": _first(data.get("terms_delivery")),
        "transport_way": _first(data.get("transport_way")),
        "country_origin": _first(data.get("country_origin")),
        "lc_number": _first(data.get("lc_number")),
        "orders": order_numbers(data), "lines": lines, "skipped_lines": skipped,
        "description": _first(data.get("description")),
        "author": row["author"] if "author" in row.keys() else "",
    }
