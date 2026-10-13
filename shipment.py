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
import unicodedata
from datetime import date, datetime, timedelta

from flask import abort, current_app, render_template, request
from flask_babel import gettext as _
from markupsafe import Markup

import applog
import db
import search_index
from appcore import (PRINT_TEMPLATES, _parse_decimal, fmt_num, format_bg_date, get_db,
                     login_required, safe_json_data)

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
#: превозвач), останалите 1–3; палетните карти — A4, 1–3 от всяка карта.
CMR_COPIES = (1, 3, 4, 5)
OTHER_COPIES = (1, 2, 3)

_TOKEN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z/\-]*")
_NUMBER_RE = re.compile(r"0*(\d+)(/\d{2,4})?")
#: Думите на свободния текст (и на кирилица) + разделителите „;“ и нов ред.
_WORD_RE = re.compile(r"[^\W_][\w/\-]*|[;\n]")
#: Дати („01.10.2026“, „2026-10-04“) не са номера на документи.
_DATE_RE = re.compile(r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})\b")

#: Одит (13.10.2026, SHIP-1): вид документ по ключовата дума пред номера в
#: свободен текст (кл. 5 на ЧМР): (видове, префикси, точни думи) — casefold.
_KIND_WORDS = (
    (db.INVOICE_DOC_TYPES, ("invoice", "фактур", "factur", "fattur", "fatura", "rechnung", "faktur"),
     ("inv", "ф-ра")),
    (("packing",), ("packing", "packlist", "опаковъч"), ("pl", "p/l")),
    (("pallet",), ("pallet", "палет", "palet"), ()),
    (("cmr",), (), ("cmr", "e-cmr", "чмр", "цмр")),
    (("waybill",), ("waybill", "товарител"), ()),
    (("dualuse",), ("dual", "двойн"), ()),
    (("export_it",), ("export", "износ", "esport"), ()),
)
#: „Декларация“ без уточнение — която и да е от двете.
_GENERIC_KIND = (("dualuse", "export_it"), ("declar", "деклар", "dichiar"))
#: Думи, които не започват ново споменаване („Invoice No. 1, 2 and 3“).
_FILLER_WORDS = frozenset(("no", "nr", "nos", "number", "num", "list", "лист", "листове", "card",
                           "cards", "карта", "карти", "and", "und", "ve", "et", "и", "of", "use",
                           "за", "for", "goods", "стоки"))
#: Правни форми — не различават клиенти („WEG S.A.“ == „WEG“).
_LEGAL_FORMS = frozenset((
    "sa", "ltda", "ltd", "llc", "inc", "gmbh", "ag", "kg", "eood", "ood", "ead", "ad", "et", "srl",
    "spa", "sas", "sarl", "bv", "nv", "oy", "ab", "as", "co", "corp", "plc", "sti", "fze", "fzco",
    "еоод", "оод", "еад", "ад", "ет", "limited", "company"))


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


def _client_key(name):
    """Името на клиента за сравнение: без диакритика/регистър/пунктуация и
    без правната форма — „Ünal Lojistik A.Ş.“ → {unal, lojistik}."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return frozenset(w for w in re.findall(r"\w+", s) if len(w) > 1 and w not in _LEGAL_FORMS)


def _same_client(a, b):
    """Непознат клиент (празно) не е „друг“; „WEG“ и „WEG Equipamentos“ — един."""
    return not a or not b or a <= b or b <= a


def _word_kinds(word):
    """(видове, общо ли е) за ключова дума, иначе (None, False)."""
    w = word.casefold()
    for kinds, prefixes, exact in _KIND_WORDS:
        if w in exact or w.startswith(prefixes):
            return kinds, False
    if w.startswith(_GENERIC_KIND[1]):
        return _GENERIC_KIND[0], True
    return None, False


def _mentions(text):
    """Одит (13.10.2026, SHIP-1): номерата в свободен текст с вида документ,
    който им съответства — ({ключ: {видове}}, {ключове без вид}).

    Ключовата дума важи за следващите номера („Invoices 1, 2“), докато не
    дойде друга дума („Certificate 3“ — номерът е без вид), „;“ или нов ред.
    Номер без ключова дума е двусмислен: само предложение без отметка."""
    refs, amb = {}, set()
    specific, generic, after_number = set(), set(), False
    for word in _WORD_RE.findall(_DATE_RE.sub(" ", str(text or ""))):
        if word in (";", "\n"):
            specific, generic, after_number = set(), set(), False
            continue
        key = norm_no(word) if _TOKEN_RE.fullmatch(word) else ""
        if _is_key(key):
            kinds = specific or generic
            if "cmr" in kinds:          # „ЧМР товарителница“ е ЧМР
                kinds = kinds - {"waybill"}
            if kinds:
                refs.setdefault(key, set()).update(kinds)
            else:
                amb.add(key)
            after_number = True
            continue
        kinds, is_generic = _word_kinds(word)
        if kinds is None and word.casefold() in _FILLER_WORDS:
            continue
        if after_number:
            specific, generic, after_number = set(), set(), False
        if kinds:
            (generic if is_generic else specific).update(kinds)
    return refs, amb


def _typed(refs, text, kinds):
    """Поле с известен вид („Фактура №“) — всички номера в него са от вида."""
    for key in _tokens(text):
        refs.setdefault(key, set()).update(kinds)


def _facts(doc_type, data):
    """Какво „сочи“ документът: номера на поръчки (po), споменати номера с
    вида им (refs: {ключ: {видове}}), споменати номера без вид (amb) и
    „Свързано ЧМР №“ (cmr_ref) на палетна карта."""
    po, refs, amb, cmr_ref = set(), {}, set(), ""
    if doc_type == "packing":
        po |= _tokens(data.get("order_no"))
        _typed(refs, data.get("invoice_no"), db.INVOICE_DOC_TYPES)
        # Редовете, издърпани от палетни карти („Палет 2747/2026 — …“).
        for it in _items(data):
            _typed(refs, it.get("description"), ("pallet",))
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
        refs, amb = _mentions(data.get("attached_docs"))
    elif doc_type == "dualuse":
        _typed(refs, data.get("invoice_numbers"), db.INVOICE_DOC_TYPES)
    elif doc_type in ("export_it", "waybill"):
        _typed(refs, data.get("invoice_no"), db.INVOICE_DOC_TYPES)
        # Одит (13.10.2026, SHIP-3): „Ref. ЧМР №“ на декларацията за износ.
        _typed(refs, data.get("ref_cmr"), ("cmr",))
    return {"po": po, "refs": refs, "amb": amb, "cmr_ref": cmr_ref,
            "cmr_ref_text": str(data.get("ref_cmr") or "").strip()}


class _Doc:
    __slots__ = ("id", "doc_type", "number", "seq", "year", "client_name", "created_at",
                 "facts", "key", "client")

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
        self.client = _client_key(self.client_name)

    def label(self):
        title = db.DOC_TYPES.get(self.doc_type, {}).get("title", self.doc_type)
        return "%s № %s" % (_(title), self.number)

    def mentions(self, other):
        """Този документ посочва `other` с вид, съвместим с вида му."""
        return _is_key(other.key) and other.doc_type in self.facts["refs"].get(other.key, ())


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
#: номер на ЧМР — „Свързано ЧМР №“ на палетните карти и „Ref. ЧМР №“ на
#: декларацията за износ; опаковъчен лист и декларациите — кл. 5 на ЧМР;
#: фактура — ЧМР, опаковъчен лист, декларации, товарителница; палетна
#: карта — редовете на опаковъчния лист.
_MENTIONED_IN = {
    "cmr": ("pallet", "export_it"),
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
    by_number = {k for k in set(doc.facts["refs"]) | doc.facts["amb"] if _is_key(k)}
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
    # Перф (13.10.2026): ключовете се търсят в сгънатото тяло за търсене
    # (document_search.body — само стойностите, ~40% от размера на `data`),
    # а прозорецът по created_at/doc_type идва изцяло от покриващия индекс.
    # Измерено (20 000 документа, студен кеш): прочетено при преглед на
    # документ 2.9 → 0.9 MB, при най-тежкия 18.4 → 2.9 MB. Ключовете са
    # ASCII (_TOKEN_RE), затова lower() съвпада със сгъването на тялото;
    # предварителният филтър става само по-широк (без регистър) — точните
    # правила остават в _evaluate.
    use_body = search_index.is_ready()
    for doc_type, keys in sorted(in_data.items()):
        if use_body:
            keys = sorted(k.lower() for k in keys)
            cond = " OR ".join("instr(s.body, ?) > 0" for _k in keys)
            sql = ("SELECT d.id FROM documents d JOIN document_search s ON s.id = d.id"  # nosec B608 -- само „?“ плейсхолдъри
                   " WHERE d.created_at >= ? AND d.created_at <= ?"
                   " AND +d.doc_type = ? AND (%s) LIMIT ?" % cond)
        else:
            keys = sorted(keys)
            cond = " OR ".join("instr(data, ?) > 0" for _k in keys)
            # „+doc_type“: без индекса по тип — иначе SQLite обхожда ВСИЧКИ
            # документи от типа в базата, вместо само прозореца по created_at.
            sql = ("SELECT id FROM documents WHERE created_at >= ? AND created_at <= ?"  # nosec B608 -- само „?“ плейсхолдъри
                   " AND +doc_type = ? AND (%s) LIMIT ?" % cond)
        for r in con.execute(sql, [lo, hi, doc_type] + keys + [POOL_QUERY_LIMIT]).fetchall():
            found.add(r["id"])
    wanted = sorted(found - set(exclude))
    rows = []
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        rows.extend(con.execute(
            "SELECT %s FROM documents WHERE id IN (%s)"  # nosec B608 -- колоните са литерал; иначе само „?“ плейсхолдъри
            % (_POOL_COLUMNS, ",".join("?" for _i in chunk)), chunk).fetchall())
    return rows


class _Linker:
    """Силните връзки (отметнати по подразбиране) и слабите предложения.

    run() връща (strong, weak): {id: [причини]} — strong включва и началния.
    Първо се изчерпват ИЗРИЧНИТЕ връзки (посочен номер със съвместим вид,
    „Свързано ЧМР“, „Ref. ЧМР“), едва после „само същата поръчка“ — иначе
    палетна карта от друга пратка със същото PO би влязла, преди да е
    намерено ЧМР-то на пратката.

    Одит (13.10.2026, SHIP-1): документ на ДРУГ клиент (виж _same_client)
    никога не се отмята — само предложение с причината „друг клиент: …“;
    номер без вид документ — само предложение."""

    def __init__(self, start, pool):
        self.start, self.pool = start, pool
        self.strong = {start.id: [_("този документ")]}
        self.weak = {}
        self.client = start.client
        self.cmr_numbers = {start.key} if start.doc_type == "cmr" else set()
        # Номерата на ЧМР на пратката — и когато самото ЧМР не е издадено
        # („Свързано ЧМР №“ на началната карта).
        self.linked_cmr = set(self.cmr_numbers)
        if start.doc_type == "pallet" and start.facts["cmr_ref"]:
            self.linked_cmr.add(start.facts["cmr_ref"])

    def doc_of(self, i):
        return self.start if i == self.start.id else self.pool[i]

    def members(self):
        return [self.doc_of(i) for i in self.strong]

    def foreign(self, doc):
        return not _same_client(self.client, doc.client)

    def accept(self, doc, reasons):
        self.strong[doc.id] = reasons
        if not self.client:
            self.client = doc.client
        if doc.doc_type == "cmr":
            self.cmr_numbers.add(doc.key)
            self.linked_cmr.add(doc.key)

    def suggest(self, doc, reason):
        if self.foreign(doc):
            reason += " — " + _("друг клиент: %(client)s") % {"client": doc.client_name}
        reasons = self.weak.setdefault(doc.id, [])
        if reason not in reasons:
            reasons.append(reason)

    def other_shipment_card(self, doc):
        """Палетна карта към ДРУГО ЧМР — друга пратка."""
        ref = doc.facts["cmr_ref"]
        return doc.doc_type == "pallet" and bool(ref) and ref not in self.linked_cmr

    @staticmethod
    def explicit_reasons(doc, members):
        reasons = []
        for m in members:
            # 1) Номерът на документа е посочен в силен документ със съвместим
            #    вид (кл. 5 на ЧМР, „Фактура №“, „Ref. ЧМР №“, редове от палети).
            if m.mentions(doc):
                if doc.doc_type == "packing":
                    reasons.append(_("посочен в %(doc)s") % {"doc": m.label()})
                elif doc.doc_type == "cmr":
                    reasons.append(_("посочено в %(doc)s") % {"doc": m.label()})
                else:
                    reasons.append(_("посочена в %(doc)s") % {"doc": m.label()})
            # 2) Документът сочи силен документ („по фактура …“, „посочва …“).
            if doc.mentions(m):
                if m.doc_type in db.INVOICE_DOC_TYPES:
                    reasons.append(_("по фактура %(number)s") % {"number": m.number})
                else:
                    reasons.append(_("посочва %(doc)s") % {"doc": m.label()})
            reasons.extend(_Linker.cmr_link_reasons(doc, m))
        return list(dict.fromkeys(reasons))

    @staticmethod
    def cmr_link_reasons(doc, m):
        """3) „Свързано ЧМР №“ на палетна карта / ЧМР-то на началната карта /
        друга карта със СЪЩОТО „Свързано ЧМР №“ (ЧМР-то може да не е издадено)."""
        ref = doc.facts["cmr_ref"]
        if doc.doc_type == "pallet" and m.doc_type == "cmr" and ref == m.key:
            return [_("свързано ЧМР № %(number)s") % {"number": m.number}]
        if doc.doc_type == "cmr" and m.doc_type == "pallet" and m.facts["cmr_ref"] == doc.key:
            return [_("посочено в %(doc)s") % {"doc": m.label()}]
        if doc.doc_type == "pallet" and m.doc_type == "pallet" and _is_key(ref) \
                and ref == m.facts["cmr_ref"]:
            return [_("същото свързано ЧМР № %(number)s") % {"number": doc.facts["cmr_ref_text"]}]
        return []

    def settle_cmr(self, candidates, changed):
        """Друго ЧМР влиза само ако пратката още няма ЧМР и едно от тях
        посочва ЯВНО повече от документите ѝ от всички останали."""
        if not self.cmr_numbers and candidates:
            candidates.sort(key=lambda c: -len(c[1]))
            best = candidates[0]
            if len(candidates) == 1 or len(best[1]) > len(candidates[1][1]):
                self.accept(*best)
                return True
        if not changed:
            for doc, reasons in candidates:
                self.suggest(doc, reasons[0] + " — " + _("може би е отделна пратка"))
        return changed

    def explicit_fixpoint(self):
        changed = True
        while changed:
            changed = False
            members = self.members()
            cmr_candidates = []
            for doc in self.pool.values():
                if doc.id in self.strong:
                    continue
                reasons = self.explicit_reasons(doc, members)
                if not reasons or self.other_shipment_card(doc):
                    continue
                if self.foreign(doc):
                    self.suggest(doc, reasons[0])
                elif doc.doc_type == "cmr":
                    cmr_candidates.append((doc, reasons))
                else:
                    self.accept(doc, reasons)
                    changed = True
            changed = self.settle_cmr(cmr_candidates, changed)

    def po_candidate(self, doc, strong_po, invoice_keys):
        """4) Само същата поръчка (PO): опаковъчният лист влиза, ако не сочи
        ДРУГА фактура; фактурата — ако пратката няма изрично посочена;
        палетната карта без „Свързано ЧМР“ — никога (само предложение, без
        значение от кой документ е започнато — SHIP-2). True = приет."""
        shared_po = sorted(doc.facts["po"] & strong_po)
        if not shared_po:
            return False
        po_text = ", ".join(shared_po[:2])
        po_reason = _("същата поръчка PO %(po)s") % {"po": po_text}
        if doc.doc_type == "pallet":
            if self.other_shipment_card(doc):
                return False
            if not doc.facts["cmr_ref"]:
                self.suggest(doc, _("същата поръчка PO %(po)s, без свързано ЧМР") % {"po": po_text})
                return False
            take = True
        elif doc.doc_type == "packing":
            own = {k for k, kinds in doc.facts["refs"].items() if kinds & set(db.INVOICE_DOC_TYPES)}
            take = not (own and invoice_keys and not (own & invoice_keys))
        elif doc.doc_type in db.INVOICE_DOC_TYPES:
            take = not invoice_keys
        else:
            self.suggest(doc, po_reason + " — " + _("може би е отделна пратка"))
            return False
        if take and not self.foreign(doc):
            self.accept(doc, [po_reason])
            return True
        self.suggest(doc, po_reason)
        return False

    def po_pass(self):
        members = self.members()
        strong_po = set().union(*(m.facts["po"] for m in members))
        invoice_keys = {m.key for m in members if m.doc_type in db.INVOICE_DOC_TYPES}
        added = False
        for doc in self.pool.values():
            if doc.id not in self.strong and self.po_candidate(doc, strong_po, invoice_keys):
                added = True
        return added

    def ambiguous_pass(self):
        """5) Номер без вид документ („0007/2026“ в кл. 5) — само предложение."""
        members = self.members()
        for doc in self.pool.values():
            if doc.id in self.strong:
                continue
            for m in members:
                if _is_key(doc.key) and doc.key in m.facts["amb"]:
                    self.suggest(doc, _("номер %(number)s е посочен в %(doc)s без вид на документа")
                                 % {"number": doc.number, "doc": m.label()})
                if _is_key(m.key) and m.key in doc.facts["amb"]:
                    self.suggest(doc, _("посочва номер %(number)s без вид на документа")
                                 % {"number": m.number})

    def run(self):
        self.explicit_fixpoint()
        for _pass in range(2):
            if not self.po_pass():
                break
            self.explicit_fixpoint()
        self.ambiguous_pass()
        for i in self.strong:
            self.weak.pop(i, None)
        return self.strong, self.weak


def _evaluate(start, pool):
    """(strong, weak) — виж _Linker."""
    return _Linker(start, pool).run()


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
    # Одит (13.10.2026, SHIP-UX): и палетните карти — 1–3 екз. от всяка
    # (напр. по 2 етикета на палет), по подразбиране 1.
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
            "copies_options": options, "default_copies": default, "is_start": is_start,
            "client_name": first.client_name,
            "date": format_bg_date(str(first.created_at)[:10]) if first.created_at else ""}


def _round_keys(start, pool, strong, queried):
    """Новите ключове за следващия кръг — всеки ключ се търси само веднъж
    (кръговете добавят само новите); `queried` се допълва."""
    by_number, in_data = set(), {}
    for i in strong:
        n, d = _query_keys(pool.get(i, start), i == start.id)
        by_number |= n
        for t, keys in d.items():
            in_data.setdefault(t, set()).update(keys)
    by_number = {k for k in by_number if ("#", k) not in queried}
    in_data = {t: {k for k in keys if (t, k) not in queried} for t, keys in in_data.items()}
    in_data = {t: keys for t, keys in in_data.items() if keys}
    queried |= {("#", k) for k in by_number}
    queried |= {(t, k) for t, keys in in_data.items() for k in keys}
    return by_number, in_data


def _collect(con, start):
    """Транзитивното търсене (до MAX_ROUNDS кръга): (pool, strong, weak)."""
    lo, hi = _window(start.created_at)
    pool, queried = {}, set()
    strong, weak = _evaluate(start, pool)
    for _round in range(MAX_ROUNDS):
        by_number, in_data = _round_keys(start, pool, strong, queried)
        if not by_number and not in_data:
            break
        for r in _fetch_pool(con, by_number, in_data, lo, hi, exclude={start.id} | set(pool)):
            pool[r["id"]] = _Doc(r, safe_json_data(r["data"]))
        strong, weak = _evaluate(start, pool)
    return pool, strong, weak


def _add_same_day(con, start, pool, strong, weak):
    """Слабо правило: същия клиент, същия ден, без връзка по данните."""
    if not start.client_name.strip():
        return
    day = str(start.created_at)[:10]
    try:
        next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return
    same_day = con.execute(
        "SELECT id, doc_type, number, seq, year, client_name, created_at, '{}' AS data"
        " FROM documents WHERE +client_name = ? AND created_at >= ? AND created_at < ?"
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


def _sort_key(d):
    return (TYPE_ORDER.get(d.doc_type, 9), d.year or 0, d.seq or 0, d.id)


def _grouped_rows(docs, reasons_of, checked):
    """Редовете на документите; палетните карти — накрая, в един ред."""
    rows = [_row([d], reasons_of[d.id], checked) for d in docs if d.doc_type != "pallet"]
    pallets = [d for d in docs if d.doc_type == "pallet"]
    if pallets:
        rows.append(_row(pallets, [r for d in pallets for r in reasons_of[d.id]], checked))
    return rows


def _start_row(start, strong_docs, strong):
    """Редът на началния документ — от палетна карта: с всички отметнати
    карти; „този документ“ е само за самата начална карта (SHIP-UX)."""
    if start.doc_type != "pallet":
        return _row([start], strong[start.id], True, is_start=True), strong_docs
    group = [d for d in strong_docs if d.doc_type == "pallet"]
    reasons = strong[start.id]
    if group:
        reasons = [_("№ %(number)s — този документ") % {"number": start.number}]
        reasons += [r for d in group for r in strong[d.id]]
    row = _row([start] + group, reasons, True, is_start=True)
    return row, [d for d in strong_docs if d.doc_type != "pallet"]


def related_documents(con, doc_row, data):
    """Подреден списък с кандидатите за „Печат на цялата пратка“ — първо
    началният документ, после силно свързаните (отметнати) по реда ЧМР,
    опаковъчен лист, фактури, декларации, товарителница, палетни карти, а
    накрая слабите предложения (неотметнати). Палетните карти са групирани
    в един ред, но пазят id-тата си (`ids`)."""
    start = _Doc(doc_row, data)
    pool, strong, weak = _collect(con, start)
    _add_same_day(con, start, pool, strong, weak)
    strong_docs = sorted((pool[i] for i in strong if i != start.id), key=_sort_key)
    first, strong_docs = _start_row(start, strong_docs, strong)
    # Предложенията на други клиенти — след тези на същия (таван MAX_WEAK_ROWS).
    client = start.client or next((pool[i].client for i in strong if i != start.id and pool[i].client),
                                  frozenset())
    weak_docs = sorted((pool[i] for i in weak),
                       key=lambda d: (not _same_client(client, d.client), _sort_key(d)))
    return ([first] + _grouped_rows(strong_docs, strong, True)
            + _grouped_rows(weak_docs, weak, False)[:MAX_WEAK_ROWS])


def shipment_candidates(doc):
    """Шаблонна функция за лентата на документа: {"rows", "checked_count"}
    или None, ако документът не е издаден/не е на екрана за преглед.
    Одит (13.10.2026, SHIP-UX): checked_count е броят ДОКУМЕНТИ (5 палетни
    карти в един ред са 5), а бутонът е и при един документ — пратката
    може да се сглоби ръчно с „добави по номер“."""
    try:
        if not doc or not doc["id"] or request.endpoint != "view_document":
            return None
    except (KeyError, IndexError, TypeError):
        return None
    # Помощна функция на ВСЕКИ изглед на документ — грешка тук не бива да
    # събаря самия документ: без бутона, с запис в дневника.
    try:
        con = get_db()
        # Перф (13.10.2026): всички заявки на търсенето — под ЕДИН споделен
        # катинар (една четяща транзакция): при база на мрежов диск всяка
        # самостоятелна заявка иначе плаща заключване/отключване и проверка
        # за горещ журнал (fcntl + stat = обмени по мрежата).
        own_txn = not con.in_transaction
        if own_txn:
            con.execute("BEGIN")
        try:
            rows = related_documents(con, doc, safe_json_data(doc["data"]))
        finally:
            if own_txn and con.in_transaction:
                con.rollback()
    except Exception:  # pylint: disable=broad-except
        applog.log_exception("shipment.shipment_candidates: неуспешно търсене на свързани документи")
        return None
    return {"rows": rows, "checked_count": sum(len(r["ids"]) for r in rows if r["checked"])}


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


def _place(d, prefix):
    """Име, адрес и „град държава“ на страна (consignee_/receiver_/client_)."""
    return _join_lines(d.get(prefix + "_name"), d.get(prefix + "_address"),
                       " ".join(x for x in (d.get(prefix + "_city"), d.get(prefix + "_country")) if x))


def _cargo(packages, gross, volume):
    cargo = []
    if packages:
        cargo.append(packages)
    if gross:
        cargo.append("бруто / gross %s кг / kg" % fmt_num(gross))
    if volume:
        cargo.append("%s м³ / m³" % fmt_num(volume))
    return " · ".join(cargo)


def _total(values):
    """Сбор на числата (десетична запетая/точка); None, ако няма нито едно."""
    nums = [n for n in (_parse_decimal(v) for v in values) if n is not None]
    if not nums:
        return None
    total = sum(nums)
    return int(total) if total == int(total) else round(total, 3)


def _cover_cmr(d):
    return {"consignee": _place(d, "consignee"),
            "carrier": " · ".join(x for x in (
                d.get("carrier"), " / ".join(x for x in (d.get("truck_reg"), d.get("trailer_reg")) if x),
                d.get("driver")) if x),
            "cargo": _cargo(" ".join(x for x in (str(d.get("packages") or ""), d.get("packing")) if x),
                            d.get("weight"), d.get("volume")),
            "loading": " — ".join(x for x in (d.get("place_loading"),
                                             format_bg_date(d.get("date_loading"))) if x)}


def _cover_packing(d):
    packages = d.get("total_packages")
    return {"po": d.get("order_no") or "", "consignee": _place(d, "receiver"),
            "cargo": _cargo("%s колета / packages" % packages if packages else "",
                            d.get("total_gross"), d.get("total_volume"))}


def _cover_invoice(d):
    pos = list(dict.fromkeys(str(it.get("po_no")).strip() for it in _items(d)
                             if str(it.get("po_no") or "").strip()))
    return {"po": ", ".join(pos[:3]) + (" …" if len(pos) > 3 else ""),
            "consignee": _join_lines(d.get("consignee_name"), d.get("consignee_address"))}


def _cover_waybill(d):
    items = _items(d)
    qty = _total(it.get("qty") for it in items)
    kinds = {str(it.get("packing") or "").strip() for it in items} - {""}
    packages = ""
    if qty:
        packages = "%s %s" % (fmt_num(qty), kinds.pop() if len(kinds) == 1 else "колета / packages")
    vehicle = " ".join(x for x in (d.get("vehicle_make"), d.get("vehicle_model")) if x)
    return {"consignee": _place(d, "consignee"),
            "carrier": " · ".join(x for x in (d.get("carrier_name"), d.get("vehicle_reg"), vehicle) if x),
            "cargo": _cargo(packages, _total(it.get("weight") for it in items), None),
            "loading": " — ".join(x for x in (
                d.get("place_loading"),
                format_bg_date(d.get("date_loading") or d.get("loading_date"))) if x)}


def _cover_pallets(datas):
    """Палетните карти заедно: клиентът, поръчките, брой палети и бруто."""
    first = next((d for d in datas if d.get("client_name")), datas[0])
    orders = list(dict.fromkeys(str(it.get("order_no")).strip() for d in datas for it in _items(d)
                                if str(it.get("order_no") or "").strip()))
    count = len(datas)
    return {"consignee": _place(first, "client"),
            "po": ", ".join(orders[:3]) + (" …" if len(orders) > 3 else ""),
            "cargo": _cargo("%d %s" % (count, "палет / pallet" if count == 1 else "палета / pallets"),
                            _total(d.get("gross") for d in datas), None)}


def _cover_export(d):
    return {"consignee": d.get("receiver_name") or ""}


def _cover_dualuse(d):
    return {"consignee": _join_lines(d.get("dest_name"), d.get("destination_country"))}


_COVER_SOURCES = (("packing", _cover_packing), ("invoice", _cover_invoice),
                  ("waybill", _cover_waybill), ("export_it", _cover_export),
                  ("dualuse", _cover_dualuse))


def _cover_info(parts, from_id):
    """Получател, поръчка, превозвач, товар, натоварване — всяко поле от
    първия документ, който го има: ЧМР (началното, ако е ЧМР), опаковъчен
    лист, фактура, товарителница, палетните карти, декларациите.
    Одит (13.10.2026, SHIP-9): досега само ЧМР/опаковъчен лист/фактура —
    пратка само с палетни карти или товарителница беше изцяло „—“."""
    by_type = {}
    for p in parts:
        key = "invoice" if p["row"]["doc_type"] in db.INVOICE_DOC_TYPES else p["row"]["doc_type"]
        by_type.setdefault(key, []).append(p)
    cmrs = sorted(by_type.get("cmr", []), key=lambda p: p["row"]["id"] != from_id)
    sources = [_cover_cmr(cmrs[0]["data"])] if cmrs else []
    for key, extract in _COVER_SOURCES[:3]:
        if by_type.get(key):
            sources.append(extract(by_type[key][0]["data"]))
    if by_type.get("pallet"):
        sources.append(_cover_pallets([p["data"] for p in by_type["pallet"]]))
    for key, extract in _COVER_SOURCES[3:]:
        if by_type.get(key):
            sources.append(extract(by_type[key][0]["data"]))
    info = dict.fromkeys(("consignee", "po", "carrier", "cargo", "loading"), "")
    for src in sources:
        for field, value in src.items():
            if not info[field] and value:
                info[field] = value
    return info


def _cover_toc(parts):
    """Съдържанието: палетните карти поред (с еднакви екземпляри) — един ред."""
    contents = []
    for p in parts:
        row = p["row"]
        last = contents[-1] if contents else None
        if row["doc_type"] == "pallet" and last and last["doc_type"] == "pallet" \
                and last["copies"] == p["copies"]:
            last["rows"].append(row)
            continue
        contents.append({"doc_type": row["doc_type"], "rows": [row], "copies": p["copies"]})
    return [_toc_entry(c) for c in contents]


def _copies_note(n):
    return "%d %s" % (n, "екземпляр / copy" if n == 1 else "екземпляра / copies")


def _toc_entry(c):
    rows, n = c["rows"], c["copies"]
    if c["doc_type"] == "pallet":
        if len(rows) > 1:
            title = "Палетни карти / Pallet cards № %s" % _pallet_range([_Doc(r, {}) for r in rows])
            note = "%d карти / cards" % len(rows)
        else:
            title = "%s № %s" % (_COVER_TYPE_TITLES["pallet"], rows[0]["number"])
            note = "1 карта / card"
        if n > 1:
            note += " × %s" % _copies_note(n)
        return {"title": title, "note": note, "count": len(rows) * n, "doc_type": "pallet"}
    title = "%s № %s" % (_COVER_TYPE_TITLES.get(c["doc_type"], c["doc_type"]), rows[0]["number"])
    note = _copies_note(n)
    if c["doc_type"] == "cmr" and n in _CMR_COPY_NOTES:
        note += " — " + _CMR_COPY_NOTES[n]
    return {"title": title, "note": note, "count": n, "doc_type": c["doc_type"]}


def _cover(parts, from_id):
    """Данните за заглавния лист (виж _cover_info) и съдържанието."""
    info = _cover_info(parts, from_id)
    toc = _cover_toc(parts)
    # Името на получателя е първият ред (удебелен на листа), адресът — останалите.
    name, _sep, rest = info["consignee"].partition("\n")
    info["consignee_name"], info["consignee_rest"] = name, rest
    info["toc"] = toc
    info["total"] = sum(t["count"] for t in toc)
    return info


def _included(parts):
    """Лентата на екрана: палетните карти поред — един елемент."""
    included = []
    for p in parts:
        row = p["row"]
        last = included[-1] if included else None
        if row["doc_type"] == "pallet" and last and last["doc_type"] == "pallet" \
                and last["copies"] == p["copies"]:
            last["docs"].append(_Doc(row, {}))
            continue
        included.append({"doc_type": row["doc_type"], "docs": [_Doc(row, {})], "copies": p["copies"]})
    for item in included:
        docs = item["docs"]
        if item["doc_type"] == "pallet" and len(docs) > 1:
            item["title"] = _("Палетни карти № %(range)s (%(count)d бр.)") % {
                "range": _pallet_range(docs), "count": len(docs)}
        else:
            item["title"] = docs[0].label()
    return included


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
    # Одит (13.10.2026, SHIP-UX): „документи: N“ — броят документи, не редовете.
    return render_template("shipment_print.html", parts=parts, cover=cover,
                           included=_included(parts), doc_count=len(parts), from_id=from_id,
                           printed_on=format_bg_date(date.today().isoformat()),
                           autoprint=request.args.get("autoprint") == "1")


_LOOKUP_COLUMNS = "id, doc_type, number, seq, year, client_name, created_at"


def _lookup_tolerant(con, code):
    """Одит (13.10.2026, SHIP-UX): номер без оглед на регистъра и водещите
    нули — същото сравнение като при свързването (norm_no): „12955“ намира
    „0000012955“, „req-br“ — „REQ-BR“. Допълва find_documents_for_code
    (баркод/точен номер/кирилска подредба)."""
    key = norm_no(code)
    if not key:
        return []
    rows = con.execute(
        "SELECT %s FROM documents WHERE upper(number) = ? OR upper(ltrim(number, '0')) = ?"  # nosec B608 -- колоните са литерал
        " ORDER BY id DESC LIMIT 50" % _LOOKUP_COLUMNS,
        (code.strip().upper(), key)).fetchall()
    return [r for r in rows if norm_no(r["number"]) == key][:10]


@login_required
def shipment_lookup():
    """GET /shipment/lookup?code=… → {"ok": true, "rows": [...]} — редове за
    диалога, намерени със СЪЩОТО търсене като сканирането на баркод, а след
    тях — и равните по номер без регистър/водещи нули (_lookup_tolerant)."""
    from routes_dashboard import find_documents_for_code
    code = (request.args.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": _("Въведете номер или баркод на документ.")}
    con = get_db()
    found = [r["id"] for r in find_documents_for_code(con, code)[:10]]
    full = [con.execute("SELECT %s FROM documents WHERE id = ?" % _LOOKUP_COLUMNS,  # nosec B608 -- колоните са литерал
                        (i,)).fetchone() for i in found]
    # И равните по norm_no („12955“ и „0000012955“) — както при свързването.
    full += [r for r in _lookup_tolerant(con, code) if r["id"] not in found]
    rows = []
    for r in full[:10]:
        if r is None or not _may_view(r):
            continue
        rows.append(_row([_Doc(r, {})], [_("добавен ръчно")], True))
    if not rows:
        return {"ok": False, "error": _("Няма документ с номер или баркод „%(code)s“.")
                % {"code": code}}
    return {"ok": True, "rows": rows}
