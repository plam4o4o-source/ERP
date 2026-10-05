# -*- coding: utf-8 -*-
"""„Печат на цялата пратка“ — всички документи на една пратка (ЧМР,
опаковъчен лист, фактури, декларации, товарителница, палетни карти) като
ЕДНО задание за печат / един PDF.

Одит (05.10.2026): три части —
  * related_documents(): намира свързаните документи по данните им (номер
    на поръчка, номера на фактури, „Свързано ЧМР №“ на палетните карти…);
  * shipment_candidates(): шаблонна функция за бутона „Печат на пратката“
    в лентата на документа (_macros.doc_toolbar) и диалога към него;
  * /shipment/print: една страница с бланките една след друга — всяка
    рендирана от СОБСТВЕНИЯ си печатен шаблон (само блок `content`, с
    `bundle=True`), тоест точно както при самостоятелен печат;
    /shipment/lookup: „+ добави документ по номер или баркод“.

Права: view_document пуска ВСЕКИ влязъл потребител до всеки тип документ
(фактурите също са в менюто на служителите), затова тук няма допълнително
филтриране по роля — _may_view е единствената точка, ако това се промени."""
import re
from datetime import date, datetime, timedelta

from flask import abort, current_app, render_template, request
from flask_babel import gettext as _
from markupsafe import Markup

import applog
import db
from appcore import (PRINT_TEMPLATES, fmt_num, format_bg_date, get_db, login_required,
                     safe_json_data)

#: Ред на типовете в диалога и в печата: ЧМР, опаковъчен лист, фактури,
#: декларации, товарителница, палетни карти.
TYPE_ORDER = {"cmr": 0, "packing": 1, "invoice_br": 2, "invoice_no": 2, "invoice_dubai": 2,
              "dualuse": 3, "export_it": 3, "waybill": 4, "pallet": 5}
#: Търси се само сред документите, създадени ±толкова дни около началния.
WINDOW_DAYS = 30
#: Кръгове на транзитивното търсене (ЧМР → фактура → декларация…).
MAX_ROUNDS = 3
#: Предпазен таван на кандидатите от една заявка към базата.
POOL_QUERY_LIMIT = 400
#: Най-много толкова слаби („може би“) предложения в диалога.
MAX_WEAK_ROWS = 8
#: Най-много документи в един печат (20 палетни карти + бланките е ~25).
MAX_PRINT_DOCS = 120
#: Брой екземпляри: ЧМР 1/3/4/5 (по подразбиране 3 — изпращач, получател,
#: превозвач), останалите 1–3; палетните карти — само A4, по една.
CMR_COPIES = (1, 3, 4, 5)
OTHER_COPIES = (1, 2, 3)

_TOKEN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z/\-]*")
_NUMBER_RE = re.compile(r"0*(\d+)(/\d{2,4})?")


def _may_view(row):
    """Огледално на view_document (само @login_required) — виж докстринга."""
    return row is not None


def norm_no(value):
    """Номер за сравнение: без интервали, главни букви, без водещи нули
    („0000012955“ == „12955“, „0765/2026“ == „765/2026“)."""
    s = re.sub(r"\s+", "", str(value or "")).upper().strip(".,;:()")
    m = _NUMBER_RE.fullmatch(s)
    if m:
        return m.group(1) + (m.group(2) or "")
    return s


def _is_key(value):
    """Годен ключ за свързване: поне 3 знака и поне една цифра — „1“ от
    „Палет 1 от 20“ или „10“ от позиция не бива да свързват документи."""
    return len(value) >= 3 and any(ch.isdigit() for ch in value)


def _tokens(text):
    return {t for t in (norm_no(x) for x in _TOKEN_RE.findall(str(text or ""))) if _is_key(t)}


def _items(data):
    return [it for it in (data.get("items") or []) if isinstance(it, dict)]


def _facts(doc_type, data):
    """Какво „сочи“ документът: номера на поръчки (po), споменати номера на
    фактури/документи (refs) и „Свързано ЧМР №“ (cmr_ref) на палетна карта."""
    po, refs, cmr_ref = set(), set(), ""
    if doc_type == "packing":
        po |= _tokens(data.get("order_no"))
        refs |= _tokens(data.get("invoice_no"))
        # Редовете, издърпани от палетни карти („Палет 2747/2026 — …“).
        for it in _items(data):
            refs |= _tokens(it.get("description"))
    elif doc_type in db.INVOICE_DOC_TYPES:
        for it in _items(data):
            po |= _tokens(it.get("po_no"))
    elif doc_type == "pallet":
        for it in _items(data):
            po |= _tokens(it.get("order_no"))
        cmr_ref = norm_no(data.get("ref_cmr"))
    elif doc_type == "cmr":
        # Кл. 5 „Приложени документи“ — свободен текст („Invoice 0000012955,
        # Packing list 0498/2026“).
        refs |= _tokens(data.get("attached_docs"))
    elif doc_type == "dualuse":
        refs |= _tokens(data.get("invoice_numbers"))
    elif doc_type in ("export_it", "waybill"):
        refs |= _tokens(data.get("invoice_no"))
    return {"po": po, "refs": refs, "cmr_ref": cmr_ref}


class _Doc:
    __slots__ = ("id", "doc_type", "number", "seq", "year", "client_name", "created_at",
                 "facts", "key")

    def __init__(self, row, data):
        self.id = row["id"]
        self.doc_type = row["doc_type"]
        self.number = row["number"]
        self.seq = row["seq"]
        self.year = row["year"]
        self.client_name = row["client_name"] or ""
        self.created_at = row["created_at"] or ""
        self.facts = _facts(self.doc_type, data)
        self.key = norm_no(self.number)

    def label(self):
        title = db.DOC_TYPES.get(self.doc_type, {}).get("title", self.doc_type)
        return "%s № %s" % (_(title), self.number)


def _window(created_at):
    try:
        start = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        start = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    return ((start - timedelta(days=WINDOW_DAYS)).strftime(fmt),
            (start + timedelta(days=WINDOW_DAYS)).strftime(fmt))


_POOL_COLUMNS = "id, doc_type, number, seq, year, client_name, created_at, data"

#: Кои типове документи могат да съдържат в ДАННИТЕ си номера на документ
#: от даден тип (за да не се сканират големите фактури за всеки ключ):
#: номер на ЧМР — „Свързано ЧМР №“ на палетните карти; опаковъчен лист и
#: декларациите — кл. 5 на ЧМР; фактура — ЧМР, опаковъчен лист, декларации,
#: товарителница; палетна карта — редовете на опаковъчния лист.
_MENTIONED_IN = {
    "cmr": ("pallet",),
    "packing": ("cmr",),
    "dualuse": ("cmr",),
    "export_it": ("cmr",),
    "waybill": ("cmr",),
    "pallet": ("packing",),
    "invoice_br": ("cmr", "packing", "dualuse", "export_it", "waybill"),
    "invoice_no": ("cmr", "packing", "dualuse", "export_it", "waybill"),
    "invoice_dubai": ("cmr", "packing", "dualuse", "export_it", "waybill"),
}
#: Номер на поръчка (PO) има в опаковъчния лист, палетните карти и фактурите.
_PO_TYPES = ("packing", "pallet") + db.INVOICE_DOC_TYPES


def _query_keys(doc, is_start):
    """(ключове за търсене по номер, {тип: ключове за търсене в данните})."""
    by_number = {k for k in doc.facts["refs"] if _is_key(k)}
    if _is_key(doc.facts["cmr_ref"]):
        by_number.add(doc.facts["cmr_ref"])
    in_data = {}
    if doc.facts["po"]:
        for t in _PO_TYPES:
            in_data.setdefault(t, set()).update(doc.facts["po"])
    # Номерата на 20-те палетни карти не се търсят поотделно в опаковъчните
    # листове (20 сканирания) — само този на началната карта.
    if _is_key(doc.key) and (doc.doc_type != "pallet" or is_start):
        for t in _MENTIONED_IN.get(doc.doc_type, ()):
            in_data.setdefault(t, set()).add(doc.key)
    return by_number, in_data


def _fetch_pool(con, by_number, in_data, lo, hi, exclude):
    """Кандидатите от прозореца ±WINDOW_DAYS (по индекса на created_at):
    документите, чийто НОМЕР съвпада със споменат номер, и тези, в чиито
    данни се среща ключ, възможен за типа им. instr() е евтин предварителен
    филтър; точните правила се проверяват после в Python (_evaluate)."""
    found = set()
    if by_number:
        keys = sorted(by_number)
        cond = " OR ".join("instr(number, ?) > 0" for _k in keys)
        for r in con.execute(
                "SELECT id, number FROM documents WHERE created_at >= ? AND created_at <= ?"  # nosec B608 -- само „?“ плейсхолдъри
                " AND (%s) LIMIT ?" % cond, [lo, hi] + keys + [POOL_QUERY_LIMIT]).fetchall():
            if norm_no(r["number"]) in by_number:
                found.add(r["id"])
    for doc_type, keys in sorted(in_data.items()):
        keys = sorted(keys)
        cond = " OR ".join("instr(data, ?) > 0" for _k in keys)
        # „+doc_type“: без индекса по тип — иначе SQLite обхожда ВСИЧКИ
        # документи от типа в базата (и чете големия `data`, за да стигне до
        # created_at), вместо само прозореца по индекса (created_at, doc_type, …).
        # Измерено при 20 000 документа: 26 → 15 ms за палетните карти.
        for r in con.execute(
                "SELECT id FROM documents WHERE created_at >= ? AND created_at <= ?"  # nosec B608 -- само „?“ плейсхолдъри
                " AND +doc_type = ? AND (%s) LIMIT ?" % cond,
                [lo, hi, doc_type] + keys + [POOL_QUERY_LIMIT]).fetchall():
            found.add(r["id"])
    wanted = sorted(found - set(exclude))
    rows = []
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        rows.extend(con.execute(
            "SELECT %s FROM documents WHERE id IN (%s)"  # nosec B608 -- колоните са литерал; иначе само „?“ плейсхолдъри
            % (_POOL_COLUMNS, ",".join("?" for _i in chunk)), chunk).fetchall())
    return rows


def _evaluate(start, pool):
    """Силните връзки (отметнати по подразбиране) и слабите предложения.

    Връща (strong, weak): {id: [причини]} — strong включва и началния.
    Първо се изчерпват ИЗРИЧНИТЕ връзки (посочен номер, „Свързано ЧМР“),
    едва после „само същата поръчка“ — иначе палетна карта от друга пратка
    със същото PO би влязла, преди да е намерено ЧМР-то на пратката."""
    strong = {start.id: [_("този документ")]}
    weak = {}
    cmr_numbers = {start.key} if start.doc_type == "cmr" else set()

    def doc_of(i):
        return start if i == start.id else pool[i]

    def add(target, doc, reason):
        reasons = target.setdefault(doc.id, [])
        if reason not in reasons:
            reasons.append(reason)

    def explicit_reasons(doc, members):
        reasons = []
        # 1) Номерът на документа е посочен в силен документ (кл. 5 на ЧМР,
        #    „Фактура №“ на опаковъчния лист/декларацията, редове от палети).
        for m in members:
            if _is_key(doc.key) and doc.key in m.facts["refs"]:
                if doc.doc_type == "packing":
                    reasons.append(_("посочен в %(doc)s") % {"doc": m.label()})
                else:
                    reasons.append(_("посочена в %(doc)s") % {"doc": m.label()})
        # 2) Документът сочи силна фактура („по фактура …“) или силен
        #    документ (ЧМР, посочващо опаковъчния лист).
        for m in members:
            if m.key in doc.facts["refs"] and _is_key(m.key):
                if m.doc_type in db.INVOICE_DOC_TYPES:
                    reasons.append(_("по фактура %(number)s") % {"number": m.number})
                elif doc.doc_type == "cmr":
                    reasons.append(_("посочва %(doc)s") % {"doc": m.label()})
        # 3) „Свързано ЧМР №“ на палетна карта / ЧМР-то на началната карта.
        for m in members:
            if doc.doc_type == "pallet" and m.doc_type == "cmr" and doc.facts["cmr_ref"] == m.key:
                reasons.append(_("свързано ЧМР № %(number)s") % {"number": m.number})
            if doc.doc_type == "cmr" and m.doc_type == "pallet" and m.facts["cmr_ref"] == doc.key:
                reasons.append(_("посочено в %(doc)s") % {"doc": m.label()})
        return list(dict.fromkeys(reasons))

    def explicit_fixpoint():
        changed = True
        while changed:
            changed = False
            members = [doc_of(i) for i in strong]
            cmr_candidates = []
            for doc in pool.values():
                if doc.id in strong:
                    continue
                reasons = explicit_reasons(doc, members)
                if not reasons:
                    continue
                if doc.doc_type == "pallet" and doc.facts["cmr_ref"] \
                        and doc.facts["cmr_ref"] not in cmr_numbers:
                    # Картата е към ДРУГО ЧМР — друга пратка.
                    continue
                if doc.doc_type == "cmr":
                    cmr_candidates.append((doc, reasons))
                    continue
                strong[doc.id] = reasons
                changed = True
            # Друго ЧМР влиза само ако пратката още няма ЧМР и едно от тях
            # посочва ЯВНО повече от документите ѝ от всички останали.
            if not cmr_numbers and cmr_candidates:
                cmr_candidates.sort(key=lambda c: -len(c[1]))
                best = cmr_candidates[0]
                if len(cmr_candidates) == 1 or len(best[1]) > len(cmr_candidates[1][1]):
                    strong[best[0].id] = best[1]
                    cmr_numbers.add(best[0].key)
                    changed = True
                    continue
            if not changed:
                for doc, reasons in cmr_candidates:
                    add(weak, doc, reasons[0] + " — " + _("може би е отделна пратка"))

    explicit_fixpoint()
    # 4) Само същата поръчка (PO): опаковъчният лист влиза, ако не сочи
    #    ДРУГА фактура; фактурата — ако пратката няма изрично посочена;
    #    палетната карта без „Свързано ЧМР“ — само ако пратката няма ЧМР.
    for _pass in range(2):
        strong_po = set()
        for i in strong:
            strong_po |= doc_of(i).facts["po"]
        invoice_keys = {doc_of(i).key for i in strong if doc_of(i).doc_type in db.INVOICE_DOC_TYPES}
        added = False
        for doc in pool.values():
            if doc.id in strong:
                continue
            shared_po = sorted(doc.facts["po"] & strong_po)
            if not shared_po:
                continue
            po_text = ", ".join(shared_po[:2])
            po_reason = _("същата поръчка PO %(po)s") % {"po": po_text}
            take = False
            if doc.doc_type == "packing":
                own = doc.facts["refs"]
                take = not (own and invoice_keys and not (own & invoice_keys))
            elif doc.doc_type in db.INVOICE_DOC_TYPES:
                take = not invoice_keys
            elif doc.doc_type == "pallet":
                if doc.facts["cmr_ref"] and doc.facts["cmr_ref"] not in cmr_numbers:
                    continue
                if cmr_numbers:
                    add(weak, doc, _("същата поръчка PO %(po)s, без свързано ЧМР") % {"po": po_text})
                    continue
                take = True
            elif doc.doc_type == "cmr":
                add(weak, doc, po_reason + " — " + _("може би е отделна пратка"))
                continue
            if take:
                strong[doc.id] = [po_reason]
                added = True
            else:
                add(weak, doc, po_reason)
        if not added:
            break
        explicit_fixpoint()
    for i in strong:
        weak.pop(i, None)
    return strong, weak


def _pallet_range(docs):
    """„2747–2766/2026“ за поредни номера от една година, иначе изброяване."""
    docs = sorted(docs, key=lambda d: (d.year or 0, d.seq or 0))
    first, last = docs[0], docs[-1]
    contiguous = (len({d.year for d in docs}) == 1 and all(
        (b.seq or 0) - (a.seq or 0) == 1 for a, b in zip(docs, docs[1:])))
    if contiguous and "/" in first.number and "/" in last.number:
        return "%s–%s" % (first.number.split("/", 1)[0], last.number)
    numbers = [d.number for d in docs]
    if len(numbers) > 4:
        return "%s … %s" % (", ".join(numbers[:3]), numbers[-1])
    return ", ".join(numbers)


def _copies_for(doc_type):
    if doc_type == "pallet":
        return None, 1
    if doc_type == "cmr":
        return CMR_COPIES, 3
    return OTHER_COPIES, 1


def _row(docs, reasons, checked, is_start=False):
    """Един ред на диалога (палетните карти — групирани в един ред)."""
    first = docs[0]
    options, default = _copies_for(first.doc_type)
    if first.doc_type == "pallet" and len(docs) > 1:
        title = _("Палетни карти № %(range)s (%(count)d бр.)") % {
            "range": _pallet_range(docs), "count": len(docs)}
    else:
        title = first.label()
    ids = [d.id for d in sorted(docs, key=lambda d: (d.year or 0, d.seq or 0, d.id))]
    return {"id": first.id, "ids": ids,
            "doc_type": first.doc_type, "number": first.number, "title": title,
            "reason": "; ".join(dict.fromkeys(reasons)), "checked": checked,
            "copies_options": options, "default_copies": default, "is_start": is_start}


def related_documents(con, doc_row, data):
    """Подреден списък с кандидатите за „Печат на цялата пратка“ — първо
    началният документ, после силно свързаните (отметнати) по реда ЧМР,
    опаковъчен лист, фактури, декларации, товарителница, палетни карти, а
    накрая слабите предложения (неотметнати). Палетните карти са групирани
    в един ред, но пазят id-тата си (`ids`)."""
    start = _Doc(doc_row, data)
    lo, hi = _window(start.created_at)
    pool = {}
    queried = set()
    strong = {start.id: []}
    for _round in range(MAX_ROUNDS):
        by_number, in_data = set(), {}
        for i in strong:
            n, d = _query_keys(pool.get(i, start), i == start.id)
            by_number |= n
            for t, keys in d.items():
                in_data.setdefault(t, set()).update(keys)
        # Всеки ключ се търси само веднъж (кръговете добавят само новите).
        by_number = {k for k in by_number if ("#", k) not in queried}
        in_data = {t: {k for k in keys if (t, k) not in queried} for t, keys in in_data.items()}
        in_data = {t: keys for t, keys in in_data.items() if keys}
        if not by_number and not in_data:
            break
        queried |= {("#", k) for k in by_number}
        queried |= {(t, k) for t, keys in in_data.items() for k in keys}
        for r in _fetch_pool(con, by_number, in_data, lo, hi, exclude={start.id} | set(pool)):
            pool[r["id"]] = _Doc(r, safe_json_data(r["data"]))
        strong, weak = _evaluate(start, pool)
    strong, weak = _evaluate(start, pool)

    # Слабо правило: същия клиент, същия ден, без връзка по данните.
    if start.client_name.strip():
        day = str(start.created_at)[:10]
        try:
            next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            next_day = None
        if next_day:
            same_day = con.execute(
                "SELECT id, doc_type, number, seq, year, client_name, created_at, '{}' AS data"
                " FROM documents WHERE client_name = ? AND created_at >= ? AND created_at < ?"
                " AND doc_type <> 'pallet' ORDER BY id LIMIT 30",
                (start.client_name, day, next_day)).fetchall()
            for r in same_day:
                if r["id"] in strong or r["id"] == start.id:
                    continue
                if r["id"] not in pool:
                    pool[r["id"]] = _Doc(r, {})
                # Друго ЧМР — винаги с тази причина (както в одобрения макет).
                if r["doc_type"] == "cmr" or r["id"] not in weak:
                    weak[r["id"]] = [_("същия клиент, същия ден — може би е отделна пратка")]

    def doc_of(i):
        return start if i == start.id else pool[i]

    def sort_key(d):
        return (TYPE_ORDER.get(d.doc_type, 9), d.year or 0, d.seq or 0, d.id)

    rows = []
    strong_docs = sorted((doc_of(i) for i in strong if i != start.id), key=sort_key)
    if start.doc_type == "pallet":
        group = [start] + [d for d in strong_docs if d.doc_type == "pallet"]
        rows.append(_row(group, strong[start.id], True, is_start=True))
        strong_docs = [d for d in strong_docs if d.doc_type != "pallet"]
    else:
        rows.append(_row([start], strong[start.id], True, is_start=True))
    pallets = [d for d in strong_docs if d.doc_type == "pallet"]
    for d in strong_docs:
        if d.doc_type != "pallet":
            rows.append(_row([d], strong[d.id], True))
    if pallets:
        reasons = []
        for d in pallets:
            reasons.extend(strong[d.id])
        rows.append(_row(pallets, reasons, True))

    weak_docs = sorted((doc_of(i) for i in weak), key=sort_key)
    weak_pallets = [d for d in weak_docs if d.doc_type == "pallet"]
    weak_rows = [_row([d], weak[d.id], False) for d in weak_docs if d.doc_type != "pallet"]
    if weak_pallets:
        reasons = []
        for d in weak_pallets:
            reasons.extend(weak[d.id])
        weak_rows.append(_row(weak_pallets, reasons, False))
    rows.extend(weak_rows[:MAX_WEAK_ROWS])
    return rows


def shipment_candidates(doc):
    """Шаблонна функция за лентата на документа: {"rows", "checked_count"}
    или None, ако документът не е издаден/не е на екрана за преглед."""
    try:
        if not doc or not doc["id"] or request.endpoint != "view_document":
            return None
    except (KeyError, IndexError, TypeError):
        return None
    # Помощна функция на ВСЕКИ изглед на документ — грешка тук не бива да
    # събаря самия документ: без бутона, с запис в дневника.
    try:
        rows = related_documents(get_db(), doc, safe_json_data(doc["data"]))
    except Exception:  # pylint: disable=broad-except
        applog.log_exception("shipment.shipment_candidates: неуспешно търсене на свързани документи")
        return None
    return {"rows": rows, "checked_count": sum(1 for r in rows if r["checked"])}


# ---------------------------------------------------------------- маршрути

def register(app):
    app.add_url_rule("/shipment/print", "shipment_print", shipment_print)
    app.add_url_rule("/shipment/lookup", "shipment_lookup", shipment_lookup)
    app.add_template_global(shipment_candidates, name="shipment_candidates")


def _parse_ids(raw):
    """„5049:3,5045,5025:1“ → [(5049, 3), (5045, 1), (5025, 1)] — без
    повторения, най-много MAX_PRINT_DOCS; невалидните части се пропускат."""
    out, seen = [], set()
    for part in str(raw or "").split(","):
        doc_id, _sep, copies = part.strip().partition(":")
        try:
            doc_id = int(doc_id)
            copies = int(copies) if copies.strip() else 1
        except ValueError:
            continue
        if doc_id <= 0 or doc_id in seen:
            continue
        seen.add(doc_id)
        out.append((doc_id, copies))
        if len(out) >= MAX_PRINT_DOCS:
            break
    return out


def _clamp_copies(doc_type, copies):
    if doc_type == "pallet":
        return 1
    if doc_type == "cmr":
        return min(max(copies, 1), 5)
    return min(max(copies, 1), OTHER_COPIES[-1])


def _render_content(base_vars, template_name, ctx):
    """Само блокът `content` на печатния шаблон (без base.html) — същият
    шаблон и същите променливи като при самостоятелен печат."""
    tmpl = current_app.jinja_env.get_template(template_name)
    variables = dict(base_vars)
    variables.update(ctx)
    return Markup("".join(tmpl.blocks["content"](tmpl.new_context(variables))))  # nosec B704 -- изход от автоекраниран шаблон


#: Двуезичните наименования на типовете на заглавния лист (печатен текст —
#: винаги БГ/EN като останалите бланки, не се превежда).
_COVER_TYPE_TITLES = {
    "cmr": "ЧМР / CMR",
    "packing": "Опаковъчен лист / Packing list",
    "pallet": "Палетна карта / Pallet card",
    "waybill": "Товарителница / Waybill",
    "dualuse": "Декларация за двойна употреба / Dual-use declaration",
    "export_it": "Декларация за износ (Италия) / Export declaration (Italy)",
    "invoice_br": "Фактура / Invoice",
    "invoice_no": "Фактура / Commercial invoice",
    "invoice_dubai": "Фактура / Commercial invoice",
}
_CMR_COPY_NOTES = {
    3: "изпращач, получател, превозвач / sender, consignee, carrier",
    4: "изпращач, получател, превозвач + 1 / sender, consignee, carrier + 1",
    5: "изпращач, получател, превозвач, архив / sender, consignee, carrier, file",
}


def _join_lines(*parts):
    return "\n".join(p.strip() for p in parts if p and str(p).strip())


def _cover(parts, from_id):
    """Данните за заглавния лист: получател, поръчка, превозвач, товар,
    натоварване (от ЧМР-то, иначе от опаковъчния лист/фактурата) и
    съдържанието (палетните карти — групирани в един ред)."""
    by_type = {}
    for p in parts:
        by_type.setdefault(p["row"]["doc_type"], []).append(p)
    cmr = next((p for p in by_type.get("cmr", []) if p["row"]["id"] == from_id), None) \
        or (by_type.get("cmr") or [None])[0]
    packing = (by_type.get("packing") or [None])[0]
    invoice = next((p for t in db.INVOICE_DOC_TYPES for p in by_type.get(t, [])), None)
    info = {"consignee": "", "po": "", "carrier": "", "cargo": "", "loading": ""}
    if cmr:
        d = cmr["data"]
        info["consignee"] = _join_lines(d.get("consignee_name"), d.get("consignee_address"),
                                        " ".join(x for x in (d.get("consignee_city"),
                                                             d.get("consignee_country")) if x))
        info["carrier"] = " · ".join(x for x in (
            d.get("carrier"), " / ".join(x for x in (d.get("truck_reg"), d.get("trailer_reg")) if x),
            d.get("driver")) if x)
        cargo = []
        if d.get("packages"):
            cargo.append(" ".join(x for x in (str(d.get("packages")), d.get("packing")) if x))
        if d.get("weight"):
            cargo.append("бруто / gross %s кг / kg" % fmt_num(d.get("weight")))
        if d.get("volume"):
            cargo.append("%s м³ / m³" % fmt_num(d.get("volume")))
        info["cargo"] = " · ".join(cargo)
        info["loading"] = " — ".join(x for x in (d.get("place_loading"),
                                                format_bg_date(d.get("date_loading"))) if x)
    if packing:
        d = packing["data"]
        info["po"] = d.get("order_no") or ""
        if not info["consignee"]:
            info["consignee"] = _join_lines(d.get("receiver_name"), d.get("receiver_address"),
                                            " ".join(x for x in (d.get("receiver_city"),
                                                                 d.get("receiver_country")) if x))
        if not info["cargo"]:
            cargo = []
            if d.get("total_packages"):
                cargo.append("%s колета / packages" % d.get("total_packages"))
            if d.get("total_gross"):
                cargo.append("бруто / gross %s кг / kg" % fmt_num(d.get("total_gross")))
            if d.get("total_volume"):
                cargo.append("%s м³ / m³" % fmt_num(d.get("total_volume")))
            info["cargo"] = " · ".join(cargo)
    if invoice:
        d = invoice["data"]
        if not info["po"]:
            pos = list(dict.fromkeys(str(it.get("po_no")).strip() for it in _items(d)
                                     if str(it.get("po_no") or "").strip()))
            info["po"] = ", ".join(pos[:3]) + (" …" if len(pos) > 3 else "")
        if not info["consignee"]:
            info["consignee"] = _join_lines(d.get("consignee_name"), d.get("consignee_address"))

    contents = []
    for p in parts:
        row = p["row"]
        if row["doc_type"] == "pallet" and contents and contents[-1]["doc_type"] == "pallet":
            contents[-1]["rows"].append(row)
            continue
        contents.append({"doc_type": row["doc_type"], "rows": [row], "copies": p["copies"]})
    toc = []
    for c in contents:
        rows = c["rows"]
        if c["doc_type"] == "pallet":
            docs = [_Doc(r, {}) for r in rows]
            if len(docs) > 1:
                title = "Палетни карти / Pallet cards № %s" % _pallet_range(docs)
            else:
                title = "%s № %s" % (_COVER_TYPE_TITLES["pallet"], rows[0]["number"])
            note = "%d карти / cards" % len(rows) if len(rows) > 1 else "1 карта / card"
            count = len(rows)
        else:
            title = "%s № %s" % (_COVER_TYPE_TITLES.get(c["doc_type"], c["doc_type"]),
                                 rows[0]["number"])
            n = c["copies"]
            note = "%d %s" % (n, "екземпляр / copy" if n == 1 else "екземпляра / copies")
            if c["doc_type"] == "cmr" and n in _CMR_COPY_NOTES:
                note += " — " + _CMR_COPY_NOTES[n]
            count = n
        toc.append({"title": title, "note": note, "count": count, "doc_type": c["doc_type"]})
    # Името на получателя е първият ред (удебелен на листа), адресът — останалите.
    name, _sep, rest = info["consignee"].partition("\n")
    info["consignee_name"], info["consignee_rest"] = name, rest
    info["toc"] = toc
    info["total"] = sum(t["count"] for t in toc)
    return info


@login_required
def shipment_print():
    """GET /shipment/print?ids=<id>:<екз.>,…&cover=1&from=<id>[&autoprint=1]"""
    con = get_db()
    wanted = _parse_ids(request.args.get("ids"))
    if not wanted:
        abort(404)
    ids = [i for i, _c in wanted]
    rows = {r["id"]: r for r in con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by"
        " WHERE d.id IN (%s)" % ",".join("?" for _i in ids), ids).fetchall()}  # nosec B608 -- само „?“ плейсхолдъри по брой
    from routes_documents import _print_context
    base_vars = {}
    current_app.update_template_context(base_vars)
    parts = []
    for doc_id, copies in wanted:
        row = rows.get(doc_id)
        if row is None or not _may_view(row) or row["doc_type"] not in PRINT_TEMPLATES:
            continue
        data = safe_json_data(row["data"])
        copies = _clamp_copies(row["doc_type"], copies)
        template = PRINT_TEMPLATES[row["doc_type"]]
        if row["doc_type"] == "cmr":
            # ЧМР печата екземплярите си само (надписите „Екземпляр за…“).
            sections = [_render_content(base_vars, template,
                                        _print_context(con, row, data, copies, bundle=True))]
        else:
            ctx = _print_context(con, row, data, 1, bundle=True)
            html = _render_content(base_vars, template, ctx)
            sections = [html] * copies
        parts.append({"row": row, "data": data, "copies": copies, "sections": sections})
    if not parts:
        abort(404)
    from_id = request.args.get("from", type=int)
    if from_id not in rows:
        from_id = parts[0]["row"]["id"]
    cover = _cover(parts, from_id) if request.args.get("cover", "1") != "0" else None
    included = []
    for p in parts:
        row = p["row"]
        if row["doc_type"] == "pallet" and included and included[-1]["doc_type"] == "pallet":
            included[-1]["docs"].append(_Doc(row, {}))
            continue
        included.append({"doc_type": row["doc_type"], "docs": [_Doc(row, {})],
                         "copies": p["copies"]})
    for item in included:
        docs = item["docs"]
        if item["doc_type"] == "pallet" and len(docs) > 1:
            item["title"] = _("Палетни карти № %(range)s (%(count)d бр.)") % {
                "range": _pallet_range(docs), "count": len(docs)}
        else:
            item["title"] = docs[0].label()
    return render_template("shipment_print.html", parts=parts, cover=cover,
                           included=included, from_id=from_id,
                           printed_on=format_bg_date(date.today().isoformat()),
                           autoprint=request.args.get("autoprint") == "1")


@login_required
def shipment_lookup():
    """GET /shipment/lookup?code=… → {"ok": true, "rows": [...]} — редове за
    диалога, намерени със СЪЩОТО търсене като сканирането на баркод."""
    from routes_dashboard import find_documents_for_code
    code = (request.args.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": _("Въведете номер или баркод на документ.")}
    con = get_db()
    found = find_documents_for_code(con, code)
    rows = []
    for r in found[:10]:
        full = con.execute("SELECT id, doc_type, number, seq, year, client_name, created_at"
                           " FROM documents WHERE id = ?", (r["id"],)).fetchone()
        if full is None or not _may_view(full):
            continue
        rows.append(_row([_Doc(full, {})], [_("добавен ръчно")], True))
    if not rows:
        return {"ok": False, "error": _("Няма документ с номер или баркод „%(code)s“.")
                % {"code": code}}
    return {"ok": True, "rows": rows}
