# -*- coding: utf-8 -*-
"""Митнически данни и износ — група CUSTOMS (v3.83).

Заявка (06.11.2026, „Направи всичко“ от анализа на логистични програми):

1. Калкулатор LDM / палетни места / платимо тегло (ЧМР, опаковъчен лист и
   отделна страница /customs/ldm). Формулите са огледални на static/customs.js:
     * LDM = Σ (Д × Ш на НЕстифираните основи) / 2,4 м (ширина на ремаркето);
       основите на ред = ⌈брой / етажи⌉ (етажи 1 = не се стифира);
     * палетни места (EUR еквивалент) = ⌈LDM × 33 / 13,6⌉ — ремарке 13,6 м
       побира 33 EUR палета (120×80);
     * платимо тегло = max(бруто, LDM × коефициент кг/LDM), коефициентът е
       в Настройки (подразбиране 1850, бързи стойности 1750/1850/2000).
2. CN/TARIC код — проверка (8 или 10 цифри, без интервали/точки) и линк към
   сайта TARIC на ЕК (само линк, без API — програмата е офлайн).
3. Норвегия (Digitoll) — проверка преди товарене (червено/зелено, за печат).
4. Двойна употреба — версия на Приложение I и контролни позиции (Настройки).
5. Intrastat — месечен износ (Excel/CSV) на изпращанията в ЕС.
6. eFTI-съвместим износ на ЧМР (JSON/XML) — виж efti_dataset().

Всичко тук е само четене/износ — единствените записи са настройките
(само admin) и колоната materials.cn_code (db._m018_customs_data)."""
import csv
import io
import json
import math
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from xml.etree import ElementTree as ET  # nosec B405 -- само за СЪЗДАВАНЕ на eFTI XML, нищо не се чете/парсва

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_babel import gettext as _

import applog
import db
from appcore import (_parse_decimal, admin_required, fetch_document, get_db, invoice_row_total,
                     invoice_row_weight, invoice_totals, login_required, read_snapshot,
                     safe_json_data)


# ---------------------------------------------------------------- CN / TARIC

_CN_STRIP_RE = re.compile(r"[\s.  ]+")
#: Сайтът на ЕК за справка по код (TARIC consultation). Само линк в браузъра.
TARIC_URL = "https://ec.europa.eu/taxation_customs/dds2/taric/measures.jsp"


def normalize_cn(value):
    """„8538 90 99“ / „8538.90.99“ → „85389099“ (махат се интервали и точки)."""
    return _CN_STRIP_RE.sub("", str(value or ""))


def cn_valid(value):
    """CN код (8 цифри) или TARIC код (10 цифри)."""
    text = normalize_cn(value)
    return text.isascii() and text.isdigit() and len(text) in (8, 10)


def cn_state(value):
    """"" (празно), "ok" или "invalid"."""
    if not normalize_cn(value):
        return ""
    return "ok" if cn_valid(value) else "invalid"


def taric_url(value):
    """Адрес на справката в TARIC за валиден код, иначе ""."""
    if not cn_valid(value):
        return ""
    return "%s?Lang=bg&Taric=%s&LangDescr=bg" % (TARIC_URL, normalize_cn(value))


# ---------------------------------------------------------------- EORI / MRN

_ID_STRIP_RE = re.compile(r"[\s.\-  ]+")
_EORI_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{1,15}$")
#: MRN (UCC): 2 цифри година, 2 букви държава, 13 знака, контролна цифра — 18.
_MRN_RE = re.compile(r"^\d{2}[A-Z]{2}[A-Z0-9]{13}\d$")


def normalize_id(value):
    return _ID_STRIP_RE.sub("", str(value or "")).upper()


def eori_valid(value):
    return bool(_EORI_RE.match(normalize_id(value)))


def mrn_valid(value):
    return bool(_MRN_RE.match(normalize_id(value)))


# ---------------------------------------------------------------- държави

#: ISO код → имена (BG/EN/местни). Гърция е „GR“ (геономенклатурата), а
#: ДДС номерата ѝ започват с „EL“ (виж VAT_PREFIX).
_COUNTRY_NAMES = {
    "AT": ("Austria", "Австрия", "Österreich"),
    "BE": ("Belgium", "Белгия", "Belgique", "België", "Belgien"),
    "BG": ("Bulgaria", "България", "Republic of Bulgaria"),
    "HR": ("Croatia", "Хърватия", "Hrvatska"),
    "CY": ("Cyprus", "Кипър"),
    "CZ": ("Czech Republic", "Czechia", "Чехия", "Česko", "Česká republika"),
    "DK": ("Denmark", "Дания", "Danmark"),
    "EE": ("Estonia", "Естония", "Eesti"),
    "FI": ("Finland", "Финландия", "Suomi"),
    "FR": ("France", "Франция"),
    "DE": ("Germany", "Германия", "Deutschland"),
    "GR": ("Greece", "Гърция", "Hellas", "Ελλάδα"),
    "HU": ("Hungary", "Унгария", "Magyarország"),
    "IE": ("Ireland", "Ирландия"),
    "IT": ("Italy", "Италия", "Italia"),
    "LV": ("Latvia", "Латвия", "Latvija"),
    "LT": ("Lithuania", "Литва", "Lietuva"),
    "LU": ("Luxembourg", "Люксембург"),
    "MT": ("Malta", "Малта"),
    "NL": ("Netherlands", "The Netherlands", "Нидерландия", "Холандия", "Nederland", "Holland"),
    "PL": ("Poland", "Полша", "Polska"),
    "PT": ("Portugal", "Португалия"),
    "RO": ("Romania", "Румъния", "România"),
    "SK": ("Slovakia", "Словакия", "Slovensko"),
    "SI": ("Slovenia", "Словения", "Slovenija"),
    "ES": ("Spain", "Испания", "España"),
    "SE": ("Sweden", "Швеция", "Sverige"),
    # Извън ЕС — за да не се бъркат с ЕС и за Норвегия (Digitoll).
    "NO": ("Norway", "Норвегия", "Norge", "Noreg"),
    "CH": ("Switzerland", "Швейцария", "Schweiz", "Suisse", "Svizzera"),
    "GB": ("United Kingdom", "Great Britain", "England", "Великобритания", "Обединено кралство",
           "Англия"),
    "TR": ("Turkey", "Türkiye", "Turkiye", "Турция"),
    "BR": ("Brazil", "Brasil", "Бразилия"),
    "AE": ("United Arab Emirates", "UAE", "U.A.E.", "Dubai", "ОАЕ", "Дубай",
           "Обединени арабски емирства"),
    "RS": ("Serbia", "Сърбия", "Srbija"),
    "MK": ("North Macedonia", "Северна Македония", "Македония"),
    "US": ("United States", "USA", "U.S.A.", "САЩ"),
    "IS": ("Iceland", "Исландия", "Ísland"),
    "LI": ("Liechtenstein", "Лихтенщайн"),
}
EU_CODES = frozenset(("AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
                      "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
                      "SI", "ES", "SE"))
#: Представка на ДДС номера, когато се различава от кода на държавата.
VAT_PREFIX = {"GR": "EL"}


def _fold(text):
    return db.search_fold(str(text or "")).strip()


def _build_country_patterns():
    pairs = []
    for code, names in _COUNTRY_NAMES.items():
        for name in names:
            pairs.append((_fold(name), code))
    # По-дългите имена първи („United Arab Emirates“ преди „Emirates“…).
    pairs.sort(key=lambda p: -len(p[0]))
    return [(re.compile(r"(?<!\w)%s(?!\w)" % re.escape(name)), code) for name, code in pairs]


_COUNTRY_PATTERNS = _build_country_patterns()


def country_code(text):
    """ISO код на държавата от свободен текст („Германия“, „DE“, „Oslo,
    Norway“, многоредов адрес — последният ред с позната държава печели);
    "" ако не е разпозната."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    upper = normalize_id(raw)
    if len(upper) == 2 and upper.isalpha():
        code = "GR" if upper == "EL" else upper
        return code if code in _COUNTRY_NAMES else ""
    for line in reversed([ln for ln in raw.splitlines() if ln.strip()]):
        folded = _fold(line)
        for pattern, code in _COUNTRY_PATTERNS:
            if pattern.search(folded):
                return code
        # „… 0150 Oslo NO“ — ISO код като последна дума на реда.
        last = normalize_id(line.replace(",", " ").split()[-1]) if line.split() else ""
        if len(last) == 2 and last in _COUNTRY_NAMES and last not in ("NO",) and last.isalpha():
            return last
    return ""


#: Полетата с държавата на местоназначение по тип документ (по реда на
#: доверие). Фактурите за Норвегия са винаги за Норвегия.
_DEST_FIELDS = {
    "cmr": ("consignee_country", "place_delivery", "consignee_city"),
    "packing": ("receiver_country", "receiver_city", "receiver_address"),
    "invoice_br": ("consignee_address",),
    "invoice_dubai": ("consignee_address",),
    "invoice_no": ("consignee_address",),
    "dualuse": ("destination_country",),
    "waybill": ("consignee_country",),
}


def destination_country(doc_type, data):
    if doc_type == "invoice_no":
        return "NO"
    for key in _DEST_FIELDS.get(doc_type, ()):
        code = country_code((data or {}).get(key))
        if code:
            return code
    return ""


def is_norway(doc_type, data):
    return destination_country(doc_type, data) == "NO"


# ---------------------------------------------------------------- LDM

TRAILER_LENGTH_M = 13.6
TRAILER_WIDTH_M = 2.4
TRAILER_EUR_PLACES = 33
#: Размери в мм на стандартните палети.
PALLET_TYPES = {"eur": (1200, 800), "ind": (1200, 1000)}
LDM_FACTOR_DEFAULT = 1850
LDM_FACTOR_PRESETS = (1750, 1850, 2000)
LDM_FACTOR_MIN, LDM_FACTOR_MAX = 100, 10000
LDM_MAX_STACK = 3
LDM_MAX_LINES = 50


def _num(value):
    """Неотрицателно число от текст („1 200“, „4,5“) или None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        number = _parse_decimal(value)
    if number is None or number < 0 or not math.isfinite(number):
        return None
    return number


def _round(value, places=2):
    quant = Decimal(1).scaleb(-places)
    return Decimal(repr(float(value))).quantize(quant, rounding=ROUND_HALF_UP)


def parse_ldm_lines(raw):
    """Скритото поле ldm_lines (JSON списък) → списък с речници; развалено → []."""
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
    if not isinstance(items, list):
        return []
    return [it for it in items[:LDM_MAX_LINES] if isinstance(it, dict)]


def ldm_factor(value, default=LDM_FACTOR_DEFAULT):
    number = _num(value)
    if number is None or not (LDM_FACTOR_MIN <= number <= LDM_FACTOR_MAX):
        return default
    return number


def compute_ldm(lines, gross=None, factor=LDM_FACTOR_DEFAULT):
    """Товарни метри, палетни места и платимо тегло (виж докстринга на модула).

    lines: [{"type": "eur"|"ind"|"custom", "qty", "length", "width" (мм),
    "stack": 1..3}]. Ред без брой или без размери се пропуска."""
    area = 0.0
    used = 0
    for line in lines or []:
        kind = str(line.get("type") or "custom").lower()
        if kind in PALLET_TYPES:
            length, width = PALLET_TYPES[kind]
        else:
            length, width = _num(line.get("length")), _num(line.get("width"))
        qty = _num(line.get("qty"))
        if not length or not width or not qty:
            continue
        stack = _num(line.get("stack")) or 1
        stack = int(min(max(stack, 1), LDM_MAX_STACK))
        footprints = math.ceil(qty / stack - 1e-9)
        area += footprints * (length / 1000.0) * (width / 1000.0)
        used += 1
    ldm_raw = area / TRAILER_WIDTH_M
    ldm = _round(ldm_raw, 2)
    places = math.ceil(ldm_raw * TRAILER_EUR_PLACES / TRAILER_LENGTH_M - 1e-9) if ldm_raw > 0 else 0
    factor = ldm_factor(factor)
    volumetric = int(_round(float(ldm) * factor, 0))
    gross_num = _num(gross)
    chargeable = volumetric
    if gross_num is not None:
        chargeable = max(int(_round(gross_num, 0)), volumetric)
    return {
        "lines": used,
        "ldm": ldm,
        "pallet_places": places,
        "trailer_pct": int(_round(ldm_raw / TRAILER_LENGTH_M * 100, 0)),
        "over_capacity": ldm_raw > TRAILER_LENGTH_M + 1e-9,
        "factor": factor,
        "volumetric_weight": volumetric,
        "chargeable_weight": chargeable if (used or gross_num is not None) else 0,
    }


def packing_ldm_lines(items, stack=1, per_qty=False):
    """Редовете на опаковъчен лист → редове за compute_ldm (размерите са в мм).
    Всеки ред с размери е ЕДИН колет (ред от палетна карта = един палет);
    per_qty=True — колетите на реда са колкото „Брой“."""
    lines = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        qty = _num(it.get("qty")) if per_qty else 1
        lines.append({"type": "custom", "qty": qty or 0, "length": it.get("length"),
                      "width": it.get("width"), "stack": stack})
    return lines


def fmt_ldm(value):
    """LDM за екрана — с десетична запетая („4,00“)."""
    return ("%.2f" % float(value)).replace(".", ",")


# ---------------------------------------------------------------- настройки

SETTING_DEFAULTS = {
    "customs_ldm_factor": str(LDM_FACTOR_DEFAULT),
    "customs_sender_eori": "",
    "customs_dualuse_annex_versions": "Делегиран регламент (ЕС) 2025/2003",
    "customs_dualuse_control_codes": "",
    "customs_intrastat_nature": "11",
    "customs_intrastat_transport": "3",
    "customs_intrastat_origin": "BG",
}
SETTING_KEYS = tuple(SETTING_DEFAULTS)
_CONTROL_CODE_RE = re.compile(r"^([0-9][A-E][0-9]{3}[A-Za-z0-9.]*)\s*(?:[—–:\-|\t]\s*)?(.*)$")


def customs_settings(settings):
    """Настройките на групата с подразбиранията (липсващ ключ = подразбиране)."""
    merged = dict(SETTING_DEFAULTS)
    for key in SETTING_KEYS:
        if settings and settings.get(key) is not None:
            merged[key] = settings.get(key)
    return merged


def _lines(text):
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]


def annex_versions(settings):
    return _lines(customs_settings(settings)["customs_dualuse_annex_versions"])


def control_codes(settings):
    """[(код, описание)] от потребителския списък (ред: „3A001 — описание“)."""
    out = []
    for line in _lines(customs_settings(settings)["customs_dualuse_control_codes"]):
        m = _CONTROL_CODE_RE.match(line)
        if m:
            out.append((m.group(1).upper(), m.group(2).strip()))
    return out


def bad_control_code_lines(text):
    return [ln for ln in _lines(text) if not _CONTROL_CODE_RE.match(ln)]


def form_context(settings):
    """Данните за полетата на формите (шаблонна функция customs_cfg)."""
    cs = customs_settings(settings)
    factor = ldm_factor(cs["customs_ldm_factor"])
    presets = sorted(set(LDM_FACTOR_PRESETS) | {int(factor) if factor == int(factor) else factor})
    return {
        "sender_eori": cs["customs_sender_eori"],
        "ldm_factor": int(factor) if factor == int(factor) else factor,
        "ldm_presets": presets,
        "annex_versions": annex_versions(settings),
        "control_codes": control_codes(settings),
    }


# ---------------------------------------------------------------- проверка Норвегия

def _related_datas(con, row, data):
    """Документите на пратката (самият + силно свързаните от „Печат на
    пратката“, shipment.related_documents) като [(тип, номер, id, data)]."""
    out = [(row["doc_type"], row["number"], row["id"], data)]
    try:
        import shipment
        rows = shipment.related_documents(con, row, data)
    except Exception:  # pylint: disable=broad-except
        applog.log_exception("customs._related_datas: неуспешно търсене на свързани документи")
        return out
    ids = []
    for r in rows:
        if r.get("checked") and not r.get("is_start"):
            ids.extend(i for i in r.get("ids", ()) if i != row["id"])
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for rr in con.execute(
                "SELECT id, doc_type, number, data FROM documents WHERE id IN (%s)" % placeholders,  # nosec B608 -- само „?“ по брой
                ids).fetchall():
            out.append((rr["doc_type"], rr["number"], rr["id"], safe_json_data(rr["data"])))
    return out


def _first(docs, keys, valid=None):
    for _t, _n, _i, d in docs:
        for key in keys:
            value = str(d.get(key) or "").strip()
            if value and (valid is None or valid(value)):
                return value
    return ""


def _sum_rows(items, key):
    total, found = 0.0, False
    for it in items or []:
        if isinstance(it, dict):
            n = _num(it.get(key))
            if n is not None:
                total += n
                found = True
    return total if found else None


#: Етикетите на печатната проверка — двуезично BG/EN, както бланките.
CHECK_PRINT_LABELS = {
    "invoice": "Фактура издадена / Invoice issued",
    "packing": "Опаковъчен лист издаден / Packing list issued",
    "cn": "CN кодове за всички редове / CN codes for all lines",
    "gross": "Бруто тегло / Gross weight",
    "net": "Нето тегло / Net weight",
    "packages": "Брой колети / Number of packages",
    "eori": "EORI на износителя / Exporter EORI",
    "consignee_eori": "EORI на получателя (по избор) / Consignee EORI (optional)",
    "mrn": "MRN на износната декларация / Export declaration MRN",
    "no_ref": "Норвежки референтен № (Digitoll) / Norwegian reference (Digitoll)",
}


def norway_checklist(con, row, data, settings=None, norway=None):
    """Проверка преди товарене за Норвегия (Digitoll) — [{"key", "label",
    "ok", "optional", "detail"}] + свързаните документи. Червено = липсва.
    norway=False — същата проверка за друг износ (без норвежкия референтен №)."""
    if norway is None:
        norway = is_norway(row["doc_type"], data)
    docs = _related_datas(con, row, data)
    settings = settings if settings is not None else db.get_settings(con)
    cs = customs_settings(settings)
    invoices = [(t, n, i, d) for t, n, i, d in docs if t in db.INVOICE_DOC_TYPES]
    packings = [(t, n, i, d) for t, n, i, d in docs if t == "packing"]
    cmrs = [(t, n, i, d) for t, n, i, d in docs if t == "cmr"]
    items = []

    def add(key, label, ok, detail="", optional=False):
        items.append({"key": key, "label": label, "ok": bool(ok), "detail": detail,
                      "optional": optional, "print_label": CHECK_PRINT_LABELS.get(key, label)})

    add("invoice", _("Фактура издадена"), invoices,
        ", ".join("№ %s" % n for _t, n, _i, _d in invoices) or _("няма свързана фактура"))
    add("packing", _("Опаковъчен лист издаден"), packings,
        ", ".join("№ %s" % n for _t, n, _i, _d in packings) or _("няма свързан опаковъчен лист"))

    # CN кодове: всички редове на фактурите; без фактура — HS кодът на
    # опаковъчния лист / кл. 10 на ЧМР.
    bad, total = [], 0
    for _t, number, _i, d in invoices:
        for idx, it in enumerate(d.get("items") or [], start=1):
            if not isinstance(it, dict):
                continue
            total += 1
            if not cn_valid(it.get("hs_code")):
                bad.append("%s/%d" % (number, idx))
    if not invoices:
        for _t, number, _i, d in packings + cmrs:
            value = d.get("hs_code") if _t == "packing" else d.get("stat_no")
            total += 1
            if not cn_valid(value):
                bad.append(str(number))
    if total == 0:
        add("cn", _("CN кодове (8/10 цифри) за всички редове"), False, _("няма редове с CN код"))
    elif bad:
        add("cn", _("CN кодове (8/10 цифри) за всички редове"), False,
            _("липсващ или невалиден код: %(rows)s") % {"rows": ", ".join(bad[:12])
                                                       + (" …" if len(bad) > 12 else "")})
    else:
        add("cn", _("CN кодове (8/10 цифри) за всички редове"), True,
            _("редове: %(num)d") % {"num": total})

    gross = _first(cmrs, ("weight",), lambda v: _num(v) is not None) or \
        _first(packings, ("total_gross",), lambda v: _num(v) is not None)
    if not gross:
        for _t, _n, _i, d in packings:
            s = _sum_rows(d.get("items"), "gross")
            if s:
                gross = "%g" % s
                break
    add("gross", _("Бруто тегло"), gross, (gross + " kg") if gross else _("не е попълнено"))

    net = _first(packings, ("total_net",), lambda v: _num(v) is not None)
    if not net:
        for _t, _n, _i, d in packings:
            s = _sum_rows(d.get("items"), "net")
            if s:
                net = "%g" % s
                break
    if not net:
        for _t, _n, _i, d in invoices:
            w = invoice_totals(d.get("items") or []).get("weight")
            if w:
                net = w
                break
    add("net", _("Нето тегло"), net, (net + " kg") if net else _("не е попълнено"))

    packages = _first(cmrs, ("packages",), lambda v: _num(v) is not None) or \
        _first(packings, ("total_packages",), lambda v: _num(v) is not None)
    add("packages", _("Брой колети"), packages, packages or _("не е попълнено"))

    eori = _first(docs, ("sender_eori",), eori_valid) or (
        cs["customs_sender_eori"] if eori_valid(cs["customs_sender_eori"]) else "")
    add("eori", _("EORI на износителя"), eori, normalize_id(eori) if eori else _("липсва или е невалиден"))
    c_eori = _first(docs, ("consignee_eori",), eori_valid)
    add("consignee_eori", _("EORI на получателя"), c_eori,
        normalize_id(c_eori) if c_eori else _("по избор — не е попълнено"), optional=True)
    mrn = _first(docs, ("mrn",), mrn_valid)
    mrn_any = _first(docs, ("mrn",))
    add("mrn", _("MRN на износната декларация"), mrn,
        normalize_id(mrn) if mrn else (_("невалиден формат: %(value)s") % {"value": mrn_any}
                                       if mrn_any else _("не е попълнен")))
    if norway:
        ref = _first(docs, ("no_declaration_ref",))
        add("no_ref", _("Норвежки референтен № (Digitoll)"), ref, ref or _("не е попълнен"))
    missing = [it for it in items if not it["ok"] and not it["optional"]]
    return {"items": items, "ok": not missing, "missing": len(missing), "norway": norway,
            "docs": [(t, n, i) for t, n, i, _d in docs]}


# ---------------------------------------------------------------- панел на изгледа

def doc_cn_codes(doc_type, data):
    """Различните CN кодове на документа [(код, състояние, taric_url)]."""
    values = []
    if doc_type == "cmr":
        values.append(data.get("stat_no"))
    if doc_type == "packing":
        values.append(data.get("hs_code"))
    for it in data.get("items") or []:
        if isinstance(it, dict) and "hs_code" in it:
            values.append(it.get("hs_code"))
    seen, out = set(), []
    for value in values:
        text = str(value or "").strip()
        key = normalize_cn(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((text, cn_state(text), taric_url(text)))
    return out


_PANEL_TYPES = ("cmr", "packing", "invoice_br", "invoice_no", "invoice_dubai")


def panel_data(doc, data):
    """Шаблонна функция customs_panel_data(doc, d) за печатните шаблони —
    None, ако панелът не се показва. Грешка тук не събаря изгледа."""
    try:
        if not doc or not doc["id"] or doc["doc_type"] not in _PANEL_TYPES \
                or request.endpoint != "view_document":
            return None
    except (KeyError, IndexError, TypeError):
        return None
    try:
        con = get_db()
        doc_type = doc["doc_type"]
        out = {"cn_codes": doc_cn_codes(doc_type, data), "norway": None,
               "efti": doc_type == "cmr", "doc_id": doc["id"],
               "ldm": str(data.get("ldm") or ""), "pallet_places": str(data.get("pallet_places") or ""),
               "chargeable_weight": str(data.get("chargeable_weight") or ""),
               "mrn": str(data.get("mrn") or "")}
        if is_norway(doc_type, data):
            out["norway"] = norway_checklist(con, doc, data)
        return out
    except Exception:  # pylint: disable=broad-except
        applog.log_exception("customs.panel_data: неуспешно сглобяване на митническия панел")
        return None


# ---------------------------------------------------------------- eFTI износ

EFTI_EXPORT_VERSION = "1.0"
EFTI_XML_NS = "urn:ph-logistics:efti-cmr-export:1"


def _split_docs(text):
    return [p.strip() for p in re.split(r"[;,\n]+", str(text or "")) if p.strip()]


def _mass(value):
    n = _num(value)
    return None if n is None else float(_round(n, 3))


def _count(value):
    n = _num(value)
    return None if n is None else int(n) if n == int(n) else float(n)


def _party(name, address, city, country, vat="", eori=""):
    return {
        "name": str(name or ""),
        "postalAddress": {"streetName": str(address or ""), "cityName": str(city or ""),
                          "countryName": str(country or ""), "countryCode": country_code(country)},
        "taxRegistrationId": str(vat or ""),
        "eoriNumber": normalize_id(eori),
    }


def efti_dataset(row, data, settings=None):
    """Данните на ЧМР като структура, съответстваща на общия набор данни
    (common data set) на eFTI (Регламент (ЕС) 2020/1056) и понятията на
    UN/CEFACT e-CMR. НЕ е официална схема — износ, който потребителят да
    подаде на сертифицирана eFTI платформа / доставчик на e-CMR.

    Съответствие (кл. на ЧМР → поле → понятие eFTI / UN/CEFACT MMT):
      номер, кл. 21 → transportDocument.id/issuePlace/issueDate
                      → Transport document identifier / Issue location, date
      кл. 1         → consignor        → Consignor (party: name, address, tax ID)
      кл. 2         → consignee        → Consignee
      кл. 16        → carrier          → Carrier (свободен текст „фирма, адрес“)
      кл. 17        → successiveCarriers
      кл. 4         → takingOver.place/date → Acceptance (taking over) location, date
      кл. 3         → delivery.place   → Delivery location (Place of delivery)
      кл. 6–12      → goodsItems[0]    → Logistics package / Trade line item:
                      marks, packageCount, packageType, description,
                      commodityCode (HS/CN), grossMassKg, volumeM3
      totals        → Consignment gross weight, package quantity, volume
      dangerousGoods.present = false   → Dangerous goods indicator (ЧМР
                      формата няма полета за ADR — товарът е неопасен)
      кл. 5         → documentsAttached → Associated / referenced documents
      кл. 13        → senderInstructions; кл. 14 paymentInstructions;
      кл. 15        → cashOnDelivery;    кл. 18 carrierReservations;
      кл. 19        → specialAgreements
      рег. №        → transportMeans[] → Transport means (road vehicle ID)
      митница       → customs.mrn / norwegianReference / EORI
      LDM           → loading.loadingMetres / palletPlaces / chargeableWeightKg
    QR код няма (забранен)."""
    data = data or {}
    cs = customs_settings(settings or {})
    sender_eori = data.get("sender_eori") or cs["customs_sender_eori"]
    means = []
    if str(data.get("truck_reg") or "").strip():
        means.append({"type": "road_tractor", "registration": str(data.get("truck_reg")).strip()})
    if str(data.get("trailer_reg") or "").strip():
        means.append({"type": "trailer", "registration": str(data.get("trailer_reg")).strip()})
    hs = normalize_cn(data.get("stat_no"))
    goods = {
        "sequence": 1,
        "marks": str(data.get("marks") or ""),
        "packageCount": _count(data.get("packages")),
        "packageType": str(data.get("packing") or ""),
        "description": str(data.get("goods") or ""),
        "commodityCode": hs,
        "commodityCodeValid": cn_valid(hs) if hs else False,
        "grossMassKg": _mass(data.get("weight")),
        "volumeM3": _mass(data.get("volume")),
    }
    return {
        "format": "PH Logistics eFTI/e-CMR data export",
        "formatVersion": EFTI_EXPORT_VERSION,
        "standardReferences": [
            "Regulation (EU) 2020/1056 (eFTI) — common data set (not an official schema)",
            "UN/CEFACT Multi-Modal Transport reference data model / e-CMR",
            "CMR Convention 1956, e-CMR Additional Protocol 2008",
        ],
        "generatedAt": datetime.now().replace(microsecond=0).isoformat(),
        "transportDocument": {
            "type": "CMR",
            "id": str(row["number"]),
            "internalBarcode": str(row["barcode"] or ""),
            "issuePlace": str(data.get("established_place") or ""),
            "issueDate": str(data.get("established_date") or ""),
        },
        "consignor": _party(data.get("sender_name"), data.get("sender_address"),
                            data.get("sender_city"), data.get("sender_country"),
                            data.get("sender_eik"), sender_eori),
        "consignee": _party(data.get("consignee_name"), data.get("consignee_address"),
                            data.get("consignee_city"), data.get("consignee_country"),
                            data.get("consignee_vat"), data.get("consignee_eori")),
        "carrier": {"nameAndAddress": str(data.get("carrier") or ""),
                    "driver": str(data.get("driver") or "")},
        "successiveCarriers": str(data.get("successive_carriers") or ""),
        "takingOver": {"place": str(data.get("place_loading") or ""),
                       "date": str(data.get("date_loading") or "")},
        "delivery": {"place": str(data.get("place_delivery") or ""),
                     "countryCode": country_code(data.get("place_delivery"))
                     or country_code(data.get("consignee_country"))},
        "goodsItems": [goods],
        "totals": {"packageCount": goods["packageCount"], "grossMassKg": goods["grossMassKg"],
                   "volumeM3": goods["volumeM3"]},
        "dangerousGoods": {"present": False},
        "documentsAttached": _split_docs(data.get("attached_docs")),
        "senderInstructions": str(data.get("sender_instructions") or ""),
        "paymentInstructions": str(data.get("payment_instructions") or ""),
        "cashOnDelivery": str(data.get("cod") or ""),
        "carrierReservations": str(data.get("reservations") or ""),
        "specialAgreements": str(data.get("special_agreements") or ""),
        "transportMeans": means,
        "customs": {"mrn": normalize_id(data.get("mrn")),
                    "norwegianReference": str(data.get("no_declaration_ref") or ""),
                    "consignorEori": normalize_id(sender_eori),
                    "consigneeEori": normalize_id(data.get("consignee_eori"))},
        "loading": {"loadingMetres": _mass(str(data.get("ldm") or "").replace(",", ".")),
                    "palletPlaces": _count(data.get("pallet_places")),
                    "chargeableWeightKg": _mass(data.get("chargeable_weight"))},
    }


#: JSON Schema (draft-07, подмножество) на износа — за проверка в тестовете
#: и за потребителя, който подава файла нататък.
EFTI_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["format", "formatVersion", "transportDocument", "consignor", "consignee",
                 "carrier", "takingOver", "delivery", "goodsItems", "totals", "dangerousGoods",
                 "documentsAttached", "transportMeans", "customs"],
    "properties": {
        "format": {"type": "string"},
        "formatVersion": {"type": "string"},
        "transportDocument": {"type": "object", "required": ["type", "id", "issuePlace", "issueDate"],
                              "properties": {"type": {"type": "string"}, "id": {"type": "string"}}},
        "consignor": {"type": "object", "required": ["name", "postalAddress"]},
        "consignee": {"type": "object", "required": ["name", "postalAddress"]},
        "carrier": {"type": "object", "required": ["nameAndAddress"]},
        "takingOver": {"type": "object", "required": ["place", "date"]},
        "delivery": {"type": "object", "required": ["place"]},
        "goodsItems": {"type": "array", "minItems": 1,
                       "items": {"type": "object",
                                 "required": ["description", "packageCount", "grossMassKg",
                                              "commodityCode"],
                                 "properties": {"packageCount": {"type": ["integer", "number", "null"]},
                                                "grossMassKg": {"type": ["number", "null"]},
                                                "description": {"type": "string"}}}},
        "totals": {"type": "object", "required": ["packageCount", "grossMassKg"]},
        "dangerousGoods": {"type": "object", "required": ["present"],
                           "properties": {"present": {"type": "boolean"}}},
        "documentsAttached": {"type": "array", "items": {"type": "string"}},
        "transportMeans": {"type": "array",
                           "items": {"type": "object", "required": ["type", "registration"]}},
        "customs": {"type": "object", "required": ["mrn", "consignorEori"]},
    },
}

_XML_ITEM_NAMES = {"goodsItems": "GoodsItem", "transportMeans": "TransportMeans",
                   "documentsAttached": "Document", "standardReferences": "Reference"}


def _xml_name(key):
    return key[:1].upper() + key[1:]


def _xml_fill(parent, value):
    if isinstance(value, dict):
        for key, sub in value.items():
            child = ET.SubElement(parent, _xml_name(key))
            _xml_fill(child, sub)
    elif isinstance(value, bool):
        parent.text = "true" if value else "false"
    elif value is None:
        parent.set("nil", "true")
    else:
        parent.text = str(value)


def efti_xml(dataset):
    """Същите данни като XML (UTF-8): елементите следват ключовете на JSON."""
    root = ET.Element("eFTIConsignmentExport", {"xmlns": EFTI_XML_NS,
                                                 "version": dataset["formatVersion"]})
    for key, value in dataset.items():
        if key == "formatVersion":
            continue
        el = ET.SubElement(root, _xml_name(key))
        if isinstance(value, list):
            for item in value:
                _xml_fill(ET.SubElement(el, _XML_ITEM_NAMES.get(key, "Item")), item)
        else:
            _xml_fill(el, value)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


# ---------------------------------------------------------------- Intrastat

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
INTRASTAT_SOURCES = ("packing", "invoice", "cmr")
INTRASTAT_DEFAULT_SOURCES = ("packing", "invoice")
#: Куката за VIES (група PARTIES): callable(con, vat) → текст за колона VIES
#: („валиден“, „невалиден“, „—“). Без нея колоната остава празна.
VIES_STATUS_HOOK = None
#: Заглавията на файла — фиксирано на български (файлът е за НСИ).
INTRASTAT_COLUMNS = (
    ("no", "№"), ("month", "Месец"), ("cn8", "CN8"), ("description", "Описание на стоката"),
    ("country", "Държава на партньора"), ("vat", "ДДС № на партньора"), ("vies", "VIES"),
    ("nature", "Характер на сделката"), ("transport", "Вид транспорт"),
    ("delivery_terms", "Условия на доставка"), ("net_kg", "Нето маса, кг"),
    ("supplementary", "Допълнителни единици"), ("value_eur", "Фактурна стойност, EUR"),
    ("origin", "Държава на произход"), ("docs", "Документи"), ("issues", "Бележки"),
)
#: Датата на документа по тип (иначе — датата на издаване).
_DATE_KEYS = {"packing": ("doc_date",), "cmr": ("date_loading", "established_date"),
              "invoice_br": ("doc_date",), "invoice_no": ("doc_date",),
              "invoice_dubai": ("doc_date",)}


def parse_month(text):
    m = _MONTH_RE.match(str(text or "").strip())
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    return date(year, month, 1)


def _next_month(d):
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _doc_date(doc_type, data, created_at):
    for key in _DATE_KEYS.get(doc_type, ()):
        value = str(data.get(key) or "").strip()
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
    try:
        return datetime.strptime(str(created_at)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _dec(value):
    n = _num(value)
    if n is None:
        return None
    try:
        return Decimal(str(value).strip().replace(" ", "").replace(",", ".")) if isinstance(value, str) \
            else Decimal(repr(n))
    except InvalidOperation:
        return Decimal(repr(n))


def _client_vat(con, name, cache):
    key = _fold(name)
    if not key:
        return ""
    if key not in cache:
        row = con.execute("SELECT vat FROM clients WHERE ci_lower(name) = ? OR ci_lower(alias) = ?"
                          " ORDER BY id LIMIT 1", (str(name).strip().lower(),) * 2).fetchone()
        cache[key] = (row["vat"] if row else "") or ""
    return cache[key]


def _vies(con, vat):
    hook = VIES_STATUS_HOOK
    if hook is None or not vat:
        return ""
    try:
        return str(hook(con, vat) or "")
    except Exception:  # pylint: disable=broad-except
        applog.log_exception("customs._vies: куката за VIES гръмна")
        return ""


def _new_line(month, doc_label, cn, desc, country, vat, nature, transport, terms, net, value,
              origin):
    issues = []
    cn_text = normalize_cn(cn)
    if not cn_valid(cn_text):
        issues.append("CN код липсва/невалиден")
    cn8 = cn_text[:8] if cn_valid(cn_text) else cn_text
    vat_norm = normalize_id(vat)
    if not vat_norm:
        issues.append("липсва ДДС № на партньора")
    elif not vat_norm.startswith(VAT_PREFIX.get(country, country)):
        issues.append("ДДС № не започва с %s" % VAT_PREFIX.get(country, country))
    if net is None:
        issues.append("липсва нето маса")
    if value is None:
        issues.append("липсва фактурна стойност")
    return {"month": month, "cn8": cn8, "description": str(desc or "").strip(),
            "country": country, "vat": vat_norm, "vies": "", "nature": nature,
            "transport": transport, "delivery_terms": str(terms or "").strip().upper(),
            "net_kg": net, "supplementary": "", "value_eur": value, "origin": origin,
            "docs": [doc_label], "issues": issues}


def intrastat_lines(con, month, sources=INTRASTAT_DEFAULT_SOURCES, aggregate=True, settings=None):
    """Редовете на Intrastat (изпращания) за месеца: документите с дата в
    месеца и местоназначение в ЕС (без България).

    * фактура — по ред: CN = HS code, нето = нето тегло × брой (ако има
      колона), стойност = сумата на реда, произход = „Държава на произход“;
    * опаковъчен лист — един ред: HS кодът, „Общо нето“ (иначе сборът на
      редовете), стойност — от фактурата по „Фактура №“ (ако е издадена тук);
      пропуска се, ако тази фактура вече е в справката;
    * ЧМР (по избор) — кл. 10, без нето и стойност (за ръчно допълване).
    ДДС № на партньора — от ЧМР, иначе от адресната книга по името."""
    cs = customs_settings(settings if settings is not None else db.get_settings(con))
    nature = cs["customs_intrastat_nature"].strip() or "11"
    transport = cs["customs_intrastat_transport"].strip() or "3"
    default_origin = country_code(cs["customs_intrastat_origin"]) or "BG"
    month_end = _next_month(month)
    lo = (month - timedelta(days=62)).isoformat()
    hi = (month_end + timedelta(days=62)).isoformat()
    types = []
    if "packing" in sources:
        types.append("packing")
    if "invoice" in sources:
        types.extend(db.INVOICE_DOC_TYPES)
    if "cmr" in sources:
        types.append("cmr")
    if not types:
        return []
    placeholders = ",".join("?" for _ in types)
    rows = con.execute(
        "SELECT id, doc_type, number, created_at, data FROM documents"
        " WHERE doc_type IN (%s) AND created_at >= ? AND created_at < ? ORDER BY id" % placeholders,  # nosec B608 -- само „?“ по брой
        list(types) + [lo, hi]).fetchall()
    ym = month.strftime("%Y-%m")
    vat_cache = {}
    lines = []
    included_invoices = set()
    docs = []
    for r in rows:
        data = safe_json_data(r["data"])
        d = _doc_date(r["doc_type"], data, r["created_at"])
        if d is None or not (month <= d < month_end):
            continue
        country = destination_country(r["doc_type"], data)
        if country not in EU_CODES or country == "BG":
            continue
        docs.append((r, data, country))
        if r["doc_type"] in db.INVOICE_DOC_TYPES:
            included_invoices.add(normalize_id(r["number"]))
    for r, data, country in docs:
        doc_type = r["doc_type"]
        label = "%s № %s" % (db.DOC_TYPES[doc_type]["prefix"], r["number"])
        if doc_type in db.INVOICE_DOC_TYPES:
            vat = _client_vat(con, data.get("consignee_name"), vat_cache)
            origin = country_code(data.get("country_origin")) or default_origin
            for it in data.get("items") or []:
                if not isinstance(it, dict):
                    continue
                weight = invoice_row_weight(it) if it.get("net_weight") not in (None, "") else ""
                lines.append(_new_line(
                    ym, label, it.get("hs_code"), it.get("description") or data.get("description"),
                    country, vat, nature, transport, data.get("terms_delivery"),
                    _dec(weight) if weight else None, _dec(invoice_row_total(it) or None), origin))
        elif doc_type == "packing":
            inv_no = normalize_id(data.get("invoice_no"))
            if inv_no and inv_no in included_invoices:
                continue
            net = _dec(data.get("total_net"))
            if net is None:
                s = _sum_rows(data.get("items"), "net")
                net = _dec(s) if s is not None else None
            value, origin = None, default_origin
            if inv_no:
                inv = con.execute(
                    "SELECT data FROM documents WHERE doc_type IN (?, ?, ?) AND UPPER(number) = ?"
                    " ORDER BY id DESC LIMIT 1", tuple(db.INVOICE_DOC_TYPES) + (inv_no,)).fetchone()
                if inv is not None:
                    inv_data = safe_json_data(inv["data"])
                    value = _dec(invoice_totals(inv_data.get("items") or []).get("price") or None)
                    origin = country_code(inv_data.get("country_origin")) or default_origin
            desc = next((it.get("description") for it in data.get("items") or []
                         if isinstance(it, dict) and it.get("description")), "")
            lines.append(_new_line(ym, label, data.get("hs_code"), desc, country,
                                   _client_vat(con, data.get("receiver_name"), vat_cache),
                                   nature, transport, data.get("terms_delivery"), net, value,
                                   origin))
        else:  # cmr
            vat = data.get("consignee_vat") or _client_vat(con, data.get("consignee_name"), vat_cache)
            lines.append(_new_line(ym, label, data.get("stat_no"), data.get("goods"), country, vat,
                                   nature, transport, "", None, None, default_origin))
    for line in lines:
        line["vies"] = _vies(con, line["vat"])
    if aggregate:
        lines = _aggregate(lines)
    for i, line in enumerate(lines, start=1):
        line["no"] = i
    return lines


def _aggregate(lines):
    keys = ("cn8", "country", "vat", "nature", "transport", "delivery_terms", "origin")
    out = {}
    for line in lines:
        key = tuple(line[k] for k in keys)
        cur = out.get(key)
        if cur is None:
            out[key] = dict(line, docs=list(line["docs"]), issues=list(line["issues"]))
            continue
        for field in ("net_kg", "value_eur"):
            a, b = cur[field], line[field]
            cur[field] = b if a is None else a if b is None else a + b
        for doc in line["docs"]:
            if doc not in cur["docs"]:
                cur["docs"].append(doc)
        for issue in line["issues"]:
            if issue not in cur["issues"]:
                cur["issues"].append(issue)
        if not cur["description"]:
            cur["description"] = line["description"]
    return list(out.values())


def _mass_kg(value):
    """Нето маса в цели кг (под 1 кг → 1)."""
    if value is None:
        return None
    kg = int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return max(kg, 1) if value > 0 else 0


def _money(value):
    return None if value is None else Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def intrastat_table(lines):
    """Редовете като списъци за файла (числата — числа, а не текст)."""
    table = []
    for line in lines:
        row = []
        for key, _label in INTRASTAT_COLUMNS:
            if key == "net_kg":
                row.append(_mass_kg(line["net_kg"]))
            elif key == "value_eur":
                money = _money(line["value_eur"])
                row.append(None if money is None else float(money))
            elif key in ("docs", "issues"):
                row.append("; ".join(line[key]))
            else:
                row.append(line.get(key, ""))
        table.append(row)
    return table


INTRASTAT_NOTE = ("ВНИМАНИЕ: файлът е помощна таблица. Форматът за електронно подаване в НСИ "
                  "(Intrastat) не е проверен — сверете колоните, кодовете и закръглянето с "
                  "актуалните указания на НСИ, преди да въведете или импортирате данните.")

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def intrastat_csv(lines):
    """CSV с „;“ и десетична запетая (UTF-8 с BOM — отваря се направо в Excel)."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow([label for _k, label in INTRASTAT_COLUMNS])
    for row in intrastat_table(lines):
        out = []
        for value in row:
            if value is None:
                out.append("")
            elif isinstance(value, float):
                out.append(("%.2f" % value).replace(".", ","))
            else:
                text = str(value)
                if text.startswith(_CSV_FORMULA_PREFIXES):
                    text = "'" + text
                out.append(text)
        writer.writerow(out)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def intrastat_xlsx(lines, month):
    from openpyxl import Workbook

    from routes_documents import _xlsx_append, _xlsx_styles
    st = _xlsx_styles()
    wb = Workbook()
    ws = wb.active
    ws.title = "Intrastat %s" % month.strftime("%Y-%m")
    _xlsx_append(ws, [label for _k, label in INTRASTAT_COLUMNS])
    for cell in ws[1]:
        cell.font = st["bold"]
        cell.fill = st["head_fill"]
        cell.border = st["border"]
        cell.alignment = st["wrap"]
    for row in intrastat_table(lines):
        r = _xlsx_append(ws, row)
        ws.cell(row=r, column=11).number_format = "0"
        ws.cell(row=r, column=13).number_format = "#,##0.00"
    widths = (5, 9, 11, 36, 10, 16, 10, 10, 9, 10, 12, 12, 14, 10, 30, 40)
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    ws.freeze_panes = "A2"
    note = wb.create_sheet("Бележка")
    _xlsx_append(note, [INTRASTAT_NOTE])
    _xlsx_append(note, ["Характер на сделката 11 — покупко-продажба; вид транспорт 3 — автомобилен. "
                        "Нето масата е закръглена до цели кг; стойността е от фактурите (EUR)."])
    note.column_dimensions["A"].width = 120
    note["A1"].alignment = st["wrap"]
    note["A1"].font = st["bold"]
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------- маршрути

def register(app):
    # Заявка (06.11.2026, интеграция): колоната „VIES“ в Intrastat чете кеша на PARTIES.
    global VIES_STATUS_HOOK  # pylint: disable=global-statement
    if VIES_STATUS_HOOK is None:
        import vies
        VIES_STATUS_HOOK = vies.cached_status_text
    app.add_url_rule("/customs/ldm", "customs_ldm", customs_ldm)
    app.add_url_rule("/customs/settings", "customs_settings", customs_settings_page,
                     methods=["GET", "POST"])
    app.add_url_rule("/customs/intrastat", "customs_intrastat", customs_intrastat)
    app.add_url_rule("/customs/intrastat/export.<fmt>", "customs_intrastat_export",
                     customs_intrastat_export)
    app.add_url_rule("/doc/<int:doc_id>/customs-check", "customs_check", customs_check)
    app.add_url_rule("/doc/<int:doc_id>/efti.<fmt>", "customs_efti_export", customs_efti_export)
    app.add_template_global(panel_data, name="customs_panel_data")
    app.add_template_global(form_context, name="customs_cfg")
    app.add_template_global(cn_state, name="cn_state")
    app.add_template_global(taric_url, name="taric_url")
    app.add_template_global(fmt_ldm, name="fmt_ldm")
    _extend_copy_skip()


def _extend_copy_skip():
    """„Копирай като нов“ не пренася MRN и норвежкия референтен № (те са на
    КОНКРЕТНАТА пратка) — допълва routes_documents._COPY_SHIPMENT_KEYS."""
    import routes_documents
    keys = routes_documents._COPY_SHIPMENT_KEYS
    for doc_type in ("cmr", "packing") + tuple(db.INVOICE_DOC_TYPES):
        current = tuple(keys.get(doc_type, ()))
        extra = tuple(k for k in ("mrn", "no_declaration_ref") if k not in current)
        keys[doc_type] = current + extra


@login_required
def customs_ldm():
    settings = db.get_settings(get_db())
    return render_template("customs_ldm.html", cfg=form_context(settings))


def _settings_form_values(current):
    return {k: request.form.get(k, current.get(k, "")) for k in SETTING_KEYS}


@admin_required
def customs_settings_page():
    con = get_db()
    current = customs_settings(db.get_settings(con))
    if request.method == "POST":
        values = {k: (request.form.get(k) or "").strip() for k in SETTING_KEYS if k in request.form}
        errors = []
        if "customs_ldm_factor" in values:
            number = _num(values["customs_ldm_factor"])
            if number is None or not (LDM_FACTOR_MIN <= number <= LDM_FACTOR_MAX):
                errors.append(_("Коефициентът кг/LDM трябва да е число от %(min)d до %(max)d "
                                "(напр. 1850).") % {"min": LDM_FACTOR_MIN, "max": LDM_FACTOR_MAX})
            else:
                values["customs_ldm_factor"] = ("%g" % number)
        if values.get("customs_sender_eori"):
            values["customs_sender_eori"] = normalize_id(values["customs_sender_eori"])
            if not eori_valid(values["customs_sender_eori"]):
                errors.append(_("EORI номерът трябва да започва с код на държава и до 15 букви/цифри "
                                "(напр. BG123456789)."))
        bad = bad_control_code_lines(values.get("customs_dualuse_control_codes", ""))
        if bad:
            errors.append(_("Невалидни контролни позиции (очаква се „3A001 — описание“): "
                            "%(lines)s") % {"lines": ", ".join(bad[:5])})
        if values.get("customs_intrastat_nature") and not re.match(r"^\d{1,2}$", values["customs_intrastat_nature"]):
            errors.append(_("Характерът на сделката е код от 1–2 цифри (напр. 11)."))
        if values.get("customs_intrastat_transport") and not re.match(r"^\d$", values["customs_intrastat_transport"]):
            errors.append(_("Видът транспорт е една цифра (3 — автомобилен)."))
        if values.get("customs_intrastat_origin"):
            code = country_code(values["customs_intrastat_origin"])
            if not code:
                errors.append(_("Държавата на произход по подразбиране не е разпозната (напр. BG)."))
            else:
                values["customs_intrastat_origin"] = code
        if errors:
            for msg in errors:
                flash(msg, "error")
            return render_template("customs_settings.html", s=_settings_form_values(current),
                                   presets=LDM_FACTOR_PRESETS)
        changed = {k: v for k, v in values.items() if v != current.get(k, "")}
        if changed:
            db.save_settings(con, changed)
            con.commit()
            applog.log_audit("митнически настройки", "ключове=%s" % ",".join(sorted(changed)))
        flash(_("Митническите настройки са запазени."), "success")
        return redirect(url_for("customs_settings"))
    return render_template("customs_settings.html", s=current, presets=LDM_FACTOR_PRESETS)


def _intrastat_filter():
    month = parse_month(request.args.get("month"))
    today = date.today()
    if month is None:
        first = date(today.year, today.month, 1)
        month = (first - timedelta(days=1)).replace(day=1)  # миналият месец
    sources = tuple(s for s in request.args.getlist("src") if s in INTRASTAT_SOURCES)
    if not request.args.get("month"):
        sources = INTRASTAT_DEFAULT_SOURCES
    aggregate = request.args.get("detail") != "1"
    return month, sources, aggregate


@login_required
@read_snapshot
def customs_intrastat():
    con = get_db()
    month, sources, aggregate = _intrastat_filter()
    lines = intrastat_lines(con, month, sources, aggregate)
    return render_template("customs_intrastat.html", lines=lines, month=month.strftime("%Y-%m"),
                           sources=sources, aggregate=aggregate, columns=INTRASTAT_COLUMNS,
                           table=intrastat_table(lines), note=INTRASTAT_NOTE)


@login_required
def customs_intrastat_export(fmt):
    if fmt not in ("xlsx", "csv"):
        abort(404)
    con = get_db()
    month, sources, aggregate = _intrastat_filter()
    lines = intrastat_lines(con, month, sources, aggregate)
    applog.log_audit("износ Intrastat", "месец=%s, формат=%s, редове=%d"
                     % (month.strftime("%Y-%m"), fmt, len(lines)))
    name = "intrastat_dispatches_%s.%s" % (month.strftime("%Y-%m"), fmt)
    if fmt == "csv":
        return send_file(io.BytesIO(intrastat_csv(lines)), as_attachment=True, download_name=name,
                         mimetype="text/csv")
    return send_file(intrastat_xlsx(lines, month), as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@login_required
@read_snapshot
def customs_check(doc_id):
    con = get_db()
    row, data = fetch_document(con, doc_id)
    if row["doc_type"] not in _PANEL_TYPES:
        abort(404)
    check = norway_checklist(con, row, data)
    return render_template("customs_check.html", doc=row, d=data, check=check,
                           title=db.DOC_TYPES[row["doc_type"]]["title"],
                           doc_titles={k: v["title"] for k, v in db.DOC_TYPES.items()})


@login_required
def customs_efti_export(doc_id, fmt):
    if fmt not in ("json", "xml"):
        abort(404)
    con = get_db()
    row, data = fetch_document(con, doc_id)
    if row["doc_type"] != "cmr":
        abort(404)
    dataset = efti_dataset(row, data, db.get_settings(con))
    applog.log_audit("износ на ЧМР (eFTI %s)" % fmt.upper(), "документ=%s" % row["number"])
    safe_no = re.sub(r"[^0-9A-Za-z_-]+", "-", str(row["number"])).strip("-") or str(row["id"])
    name = "CMR_%s_eFTI.%s" % (safe_no, fmt)
    if fmt == "json":
        body = json.dumps(dataset, ensure_ascii=False, indent=2).encode("utf-8")
        return send_file(io.BytesIO(body), as_attachment=True, download_name=name,
                         mimetype="application/json")
    return send_file(io.BytesIO(efti_xml(dataset)), as_attachment=True, download_name=name,
                     mimetype="application/xml")

