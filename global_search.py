# -*- coding: utf-8 -*-
"""Глобално търсене — едно поле за цялата програма.

Заявка (06.11.2026, BOOK): „Глобално търсене“ от анализа на логистичните
програми. Едно поле (странична лента; Ctrl+K или „/“ от всяка страница)
търси наведнъж в:

* документите (без фактурите) и фактурите — през същия триграмен индекс
  като списъците (appcore.document_text_search: сгъване на регистъра и
  без диакритика, няколко думи = всички);
* общата адресна книга, адресната книга за фактури, справочника материали
  и (ако ги има — група PARTIES) превозвачите — с ci_contains (db), т.е.
  същото сгъване като търсачките на тези екрани.

Резултатите са групирани по вид, с „къде е намерено“ (поле: PO, номер,
материал, адрес…) и пряк линк. Всяка група е ограничена (LIVE_LIMIT за
падащия списък, PAGE_LIMIT за страницата /search) — „Виж всички“ води към
списъка на съответния раздел с филтъра.

Права: служителят вижда всичко, което вижда и днес — документите,
фактурите, двете адресни книги и материалите са достъпни за всеки влязъл
потребител (login_required); административните настройки не се търсят.
"""
import json
import time

from flask import render_template, request, url_for
from flask_babel import gettext as _
from flask_babel import gettext as _dyn  # превод на готов msgid (не се извлича от pybabel)
from werkzeug.routing import BuildError

import db
import search_index
from appcore import (N_, document_text_search, get_db, login_required,
                     paginate_documents, read_snapshot)

#: Колко резултата на група в падащия списък / на страницата /search.
LIVE_LIMIT = 5
PAGE_LIMIT = 30
#: По-къса заявка не се търси (всяка буква би върнала половината база).
MIN_QUERY = 2
#: Най-много толкова „къде е намерено“ на резултат.
MAX_WHERE = 3
#: Най-много толкова думи в заявката.
MAX_TERMS = 6

#: Етикети на полетата на документите за „къде е намерено“ (ключ → етикет).
_DOC_FIELD_LABELS = {
    "number": N_("номер"), "barcode": N_("баркод"),
    "po_no": N_("PO / поръчка"), "order_no": N_("PO / поръчка"),
    "pos": N_("позиция"),
    "material_code": N_("материал"), "reference": N_("материал"),
    "reference_desc": N_("описание"), "description": N_("описание"), "goods": N_("описание"),
    "hs_code": N_("HS код"),
    "invoice_number": N_("№ фактура"), "invoice_no": N_("№ фактура"),
    "invoice_numbers": N_("№ фактура"),
    "ref_cmr": N_("ЧМР"), "route_sheet_no": N_("пътен лист"),
    "consignee_name": N_("получател"), "receiver_name": N_("получател"),
    "dest_name": N_("получател"), "billto_name": N_("Bill To"),
    "client_name": N_("клиент"), "sender_name": N_("изпращач"),
    "carrier_name": N_("превозвач"), "carrier": N_("превозвач"),
    "successive_carriers": N_("превозвач"),
    "vehicle_reg": N_("рег. №"), "truck_reg": N_("рег. №"), "trailer_reg": N_("рег. №"),
    "driver": N_("шофьор"), "escort_name": N_("придружител"),
    "pallet_no": N_("палет №"), "marks": N_("маркировка"), "packing": N_("опаковка"),
    "notes": N_("бележка"), "special_agreements": N_("бележка"),
    "reservations": N_("бележка"), "sender_instructions": N_("бележка"),
}
#: По окончание на ключа (consignee_address, sender_city…).
_DOC_SUFFIX_LABELS = (
    ("_address", N_("адрес")), ("place_loading", N_("адрес")), ("place_delivery", N_("адрес")),
    ("_city", N_("град")), ("_country", N_("държава")), ("_postcode", N_("пощ. код")),
    ("_vat", N_("ДДС / ЕИК")), ("_eik", N_("ДДС / ЕИК")),
    ("_phone", N_("контакт")), ("_email", N_("контакт")), ("_contact", N_("контакт")),
    ("_name", N_("име")), ("_from", N_("адрес")), ("_to", N_("адрес")),
)

_CLIENT_FIELDS = (
    ("name", N_("фирма")), ("alias", N_("псевдоним")), ("address", N_("адрес")),
    ("city", N_("град")), ("postcode", N_("пощ. код")), ("country", N_("държава")),
    ("eik", N_("ЕИК")), ("vat", N_("ДДС №")), ("phone", N_("телефон")),
    ("email", N_("имейл")), ("contact", N_("лице за контакт")),
)
_INVOICE_CLIENT_FIELDS = (
    ("name", N_("наименование")), ("delivery_name", N_("доставка — фирма")),
    ("delivery_address", N_("адрес за доставка")), ("delivery_phone", N_("телефон")),
    ("billing_name", N_("Bill To — фирма")), ("billing_address", N_("адрес за фактуриране")),
    ("billing_phone", N_("телефон")), ("notes", N_("забележка")),
)
_MATERIAL_FIELDS = (("code", N_("код")), ("description", N_("описание")))
#: Превозвачите (група PARTIES): търсят се само колоните, които съществуват.
_CARRIER_FIELDS = (
    ("name", N_("фирма")), ("alias", N_("псевдоним")), ("address", N_("адрес")),
    ("city", N_("град")), ("country", N_("държава")), ("eik", N_("ЕИК")),
    ("vat", N_("ДДС №")), ("eori", N_("EORI")), ("phone", N_("телефон")), ("email", N_("имейл")),
    ("contact", N_("лице за контакт")), ("truck_reg", N_("рег. №")),
    ("vehicle_reg", N_("рег. №")), ("trailer_reg", N_("рег. №")), ("notes", N_("забележка")),
)

#: Групите в реда на показване: (ключ, заглавие).
GROUPS = (
    ("documents", N_("Документи")),
    ("invoices", N_("Фактури")),
    ("clients", N_("Адресна книга")),
    ("invoice_clients", N_("Адресна книга (фактури)")),
    ("materials", N_("Материали")),
    ("carriers", N_("Превозвачи")),
)


def register(app):
    app.add_url_rule("/search", "global_search", search_page)
    app.add_url_rule("/search/live", "global_search_live", search_live)


# ---------------------------------------------------------------- помощни

def query_terms(query):
    terms = []
    for term in (query or "").split():
        if term not in terms:
            terms.append(term)
    return terms[:MAX_TERMS]


def _fold(text):
    return db.search_fold(text) or ""


def _snippet(value, folded_term, width=70):
    value = " ".join(str(value).split())
    if len(value) <= width:
        return value
    pos = max(0, _fold(value).find(folded_term))
    start = max(0, min(pos - width // 3, len(value) - width))
    out = value[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(value) else "")


def _doc_field_label(key):
    if key in _DOC_FIELD_LABELS:
        return _DOC_FIELD_LABELS[key]
    for suffix, label in _DOC_SUFFIX_LABELS:
        if key.endswith(suffix):
            return label
    return key


def _walk(value, key=None, row=None):
    """(последен ключ, номер на ред или None, стойност) за всяка текстова/
    числова стойност в JSON-а на документа."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, k if isinstance(k, str) else key, row)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, key, row if row is not None else i + 1)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        yield key, row, value


def _where_document(row, data, folded_terms):
    """„Къде е намерено“ за документ: [{field, row, value}] (до MAX_WHERE)."""
    terms = _where_terms(folded_terms)
    found, seen = [], set()
    sources = [("number", None, row["number"]), ("barcode", None, row["barcode"])]
    for key, line, value in sources + list(_walk(data)):
        if value in (None, "") or key is None:
            continue
        folded = _fold(str(value))
        for rank, term in enumerate(terms):
            if term in folded:
                label = _doc_field_label(key)
                mark = (label, line)
                if mark not in seen:
                    seen.add(mark)
                    found.append((rank, len(found), {"field": _(label) if label != key else key,
                                                     "row": line, "value": _snippet(value, term)}))
                break
        if sum(1 for f in found if f[0] == 0) >= MAX_WHERE:
            break  # вече има MAX_WHERE съвпадения на най-дългата дума
    return [w for _r, _n, w in sorted(found, key=lambda x: (x[0], x[1]))[:MAX_WHERE]]


def _where_terms(folded_terms):
    """Думите за „къде е намерено“ — най-дългите първи; еднобуквените само
    ако няма други (иначе „в“ от „в 1234“ би посочило всяко поле с „в“)."""
    terms = sorted({t for t in folded_terms if t}, key=lambda t: (-len(t), t))
    longer = [t for t in terms if len(t) > 1]
    return longer or terms


def _where_fields(row, fields, folded_terms):
    terms = _where_terms(folded_terms)
    found = []
    for col, label in fields:
        value = row[col] if col in row.keys() else None
        if not value:
            continue
        folded = _fold(str(value))
        for rank, term in enumerate(terms):
            if term in folded:
                found.append((rank, len(found), {"field": _(label), "row": None,
                                                 "value": _snippet(value, term)}))
                break
    return [w for _r, _n, w in sorted(found, key=lambda x: (x[0], x[1]))[:MAX_WHERE]]


def _all_terms(text, needles):
    """SQL функция gs_all_terms: всички (вече сгънати) думи са в сгънатия текст.
    Едно извикване на ред за всички полета и думи — при 10 000 материала
    ~3 пъти по-бързо от ci_contains по поле и дума (същото сгъване)."""
    if not text:
        return False
    folded = _FOLD_CACHE.get(text)
    if folded is None:
        if len(_FOLD_CACHE) >= _FOLD_CACHE_MAX:
            _FOLD_CACHE.clear()
        folded = _FOLD_CACHE[text] = db.search_fold(text)
    return all(n in folded for n in needles.split(_SEP))


_SEP = "\x1f"
#: Сгънатият текст по САМИЯ текст (ключът е съдържанието — промяна в базата
#: дава нов ключ, нищо не остарява). Сгъването на кирилица/диакритика е
#: ~3 µs на ред; от кеша — ~0.5 µs (10 000 материала: 33 → ~8 ms).
_FOLD_CACHE = {}
_FOLD_CACHE_MAX = 60000


def _terms_where(con, fields, terms):
    """Условие „всяка дума е в някое от полетата“ (сгъване като db.search_fold).
    Полетата се сливат с разделител \x1f — дума не може да „прескочи“ от едно
    поле в друго (str.split() го брои за интервал, т.е. думите не го съдържат)."""
    con.create_function("gs_all_terms", 2, _all_terms, deterministic=True)
    text = " || char(31) || ".join("COALESCE(%s, '')" % f for f in fields)
    return "gs_all_terms(%s, ?)" % text, [_SEP.join(_fold(t) for t in terms)]


def _url(endpoint, **values):
    try:
        return url_for(endpoint, **values)
    except BuildError:
        return None


def _date(value):
    text = str(value or "")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return "%s.%s.%s" % (text[8:10], text[5:7], text[0:4])
    return text[:10]


# ---------------------------------------------------------------- групи

def _search_documents(con, query, terms, limit, invoices):
    if max(len(t) for t in terms) < search_index.FTS_MIN_CHARS:
        # 2 знака: триграмният индекс не помага, а обхождането на всички тела
        # е ~50 ms при 20 000 документа — само номер/баркод (ASCII, LIKE е native).
        sql = " AND ".join("(d.number LIKE ? ESCAPE '\\' OR d.barcode LIKE ? ESCAPE '\\')" for _t in terms)
        params = []
        for t in terms:
            like = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
    else:
        sql, params = document_text_search(query, ("d.number", "d.barcode"))
    types = list(db.INVOICE_DOC_TYPES)
    where = "WHERE %s AND d.doc_type %s (%s)" % (
        sql, "IN" if invoices else "NOT IN", ",".join("?" * len(types)))
    rows, _page, _pages, total = paginate_documents(
        con, where, list(params) + types, 1, page_size=limit,
        columns="d.id, d.doc_type, d.number, d.barcode, d.client_name, d.created_at, d.data",
        count_cap=limit)
    folded = [_fold(t) for t in terms]
    items = []
    for r in rows:
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            data = {}
        title = _dyn(db.DOC_TYPES.get(r["doc_type"], {}).get("title", r["doc_type"]))
        sub = " · ".join(x for x in ((r["client_name"] or "").strip(), _date(r["created_at"])) if x)
        items.append({"title": "%s № %s" % (title, r["number"]), "subtitle": sub,
                      "where": _where_document(r, data if isinstance(data, dict) else {}, folded),
                      "url": url_for("view_document", doc_id=r["id"])})
    more = isinstance(total, int) and getattr(total, "at_least", False)
    more_url = url_for("invoices_list" if invoices else "documents", q=query)
    return items, more, more_url


def _search_table(con, table, fields, terms, limit, order):
    cols = [f for f, _l in fields]
    where, params = _terms_where(con, cols, terms)
    rows = con.execute("SELECT * FROM %s WHERE %s ORDER BY %s LIMIT ?"  # nosec B608 -- таблица/колони/подредба — само константи от модула
                       % (table, where, order), params + [limit + 1]).fetchall()
    return rows[:limit], len(rows) > limit


def _search_clients(con, query, terms, limit):
    rows, more = _search_table(con, "clients", _CLIENT_FIELDS, terms, limit, "name COLLATE NOCASE")
    folded = [_fold(t) for t in terms]
    items = [{"title": r["name"] + (" (%s)" % r["alias"] if r["alias"] else ""),
              "subtitle": ", ".join(x for x in (r["city"], r["country"]) if x),
              "where": _where_fields(r, _CLIENT_FIELDS, folded),
              "url": url_for("client_edit", client_id=r["id"])} for r in rows]
    return items, more, url_for("clients_list", q=query)


def _search_invoice_clients(con, query, terms, limit):
    rows, more = _search_table(con, "invoice_clients", _INVOICE_CLIENT_FIELDS, terms, limit, "name")
    folded = [_fold(t) for t in terms]
    items = [{"title": r["name"],
              "subtitle": (r["billing_name"] or r["delivery_name"] or ""),
              "where": _where_fields(r, _INVOICE_CLIENT_FIELDS, folded),
              "url": url_for("invoice_client_edit", entry_id=r["id"])} for r in rows]
    return items, more, url_for("invoice_clients_list", q=query)


def _search_materials(con, query, terms, limit):
    rows, more = _search_table(con, "materials", _MATERIAL_FIELDS, terms, limit, "code")
    folded = [_fold(t) for t in terms]
    items = [{"title": r["code"],
              "subtitle": " · ".join(x for x in (r["description"],
                                                 ("%s kg" % r["net_weight"]) if r["net_weight"] else "") if x),
              "where": _where_fields(r, _MATERIAL_FIELDS, folded),
              "url": url_for("materials_list", q=r["code"])} for r in rows]
    return items, more, url_for("materials_list", q=query)


def _table_columns(con, table):
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is None:
        return None
    return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}  # nosec B608 -- име на таблица от модула


def _carrier_fields(con):
    """Полетата на превозвачите, които ги има в базата (група PARTIES, db._m016),
    + рег. номерата на камионите/ремаркетата и шофьорите, ако ги има."""
    cols = _table_columns(con, "carriers")
    if not cols or "id" not in cols or "name" not in cols:
        return None
    fields = [(c, c, label) for c, label in _CARRIER_FIELDS if c in cols]
    vehicles = _table_columns(con, "carrier_vehicles") or set()
    if {"carrier_id", "plate"} <= vehicles:
        fields.append(("(SELECT group_concat(v.plate, ', ') FROM carrier_vehicles v"
                       " WHERE v.carrier_id = c.id)", "gs_plates", N_("рег. №")))
    drivers = _table_columns(con, "carrier_drivers") or set()
    if {"carrier_id", "name"} <= drivers:
        fields.append(("(SELECT group_concat(dr.name, ', ') FROM carrier_drivers dr"
                       " WHERE dr.carrier_id = c.id)", "gs_drivers", N_("шофьор")))
    return fields


def _search_carriers(con, query, terms, limit):
    fields = _carrier_fields(con)
    if fields is None:
        return None
    where, params = _terms_where(con, [expr if expr != col else "c." + col for expr, col, _l in fields], terms)
    extra = "".join(", %s AS %s" % (expr, col) for expr, col, _l in fields if expr != col)
    rows = con.execute("SELECT c.*%s FROM carriers c WHERE %s ORDER BY c.name COLLATE NOCASE LIMIT ?"  # nosec B608 -- изразите са константи от модула
                       % (extra, where), params + [limit + 1]).fetchall()
    more = len(rows) > limit
    folded = [_fold(t) for t in terms]
    labels = [(col, label) for _e, col, label in fields]
    items = []
    for r in rows[:limit]:
        name = str(r["name"] or "")
        items.append({"title": name,
                      "subtitle": ", ".join(str(r[c]) for c in ("city", "country") if c in r.keys() and r[c]),
                      "where": _where_fields(r, labels, folded),
                      "url": _url("carrier_edit", carrier_id=r["id"]) or _url("carriers_list", q=name)})
    return items, more, _url("carriers_list", q=query)


def search(con, query, limit=LIVE_LIMIT):
    """{"query", "groups": [{key, label, items, more, more_url}], "total", "ms"}."""
    started = time.perf_counter()
    query = " ".join((query or "").split())[:200]
    terms = query_terms(query)
    groups = []
    if len(query) >= MIN_QUERY and terms:
        query = " ".join(terms)
        runners = {
            "documents": lambda: _search_documents(con, query, terms, limit, False),
            "invoices": lambda: _search_documents(con, query, terms, limit, True),
            "clients": lambda: _search_clients(con, query, terms, limit),
            "invoice_clients": lambda: _search_invoice_clients(con, query, terms, limit),
            "materials": lambda: _search_materials(con, query, terms, limit),
            "carriers": lambda: _search_carriers(con, query, terms, limit),
        }
        for key, label in GROUPS:
            result = runners[key]()
            if not result or not result[0]:
                continue
            items, more, more_url = result
            groups.append({"key": key, "label": _(label), "items": items,
                           "more": bool(more), "more_url": more_url})
    return {"query": query, "groups": groups,
            "total": sum(len(g["items"]) for g in groups),
            "ms": round((time.perf_counter() - started) * 1000, 1)}


# ---------------------------------------------------------------- маршрути

@login_required
@read_snapshot
def search_page():
    result = search(get_db(), request.args.get("q", ""), limit=PAGE_LIMIT)
    return render_template("search.html", q=request.args.get("q", "").strip(),
                           result=result, min_query=MIN_QUERY)


@login_required
@read_snapshot
def search_live():
    result = search(get_db(), request.args.get("q", ""), limit=LIVE_LIMIT)
    result["ok"] = True
    result["page_url"] = url_for("global_search", q=result["query"])
    return result
