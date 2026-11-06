# -*- coding: utf-8 -*-
"""Палетна сметка (размяна на EUR/EPAL палети) по клиент и по превозвач.

Заявка (06.11.2026, PARTIES): „Палетна сметка по клиент и превозвач (EUR
палети: дадени, върнати, салдо)“ — стандарт при европейските SMB TMS
(Logistiqo, PTV Axylog): всяка пратка с размяна на палети осчетоводява
салдото на партньора.

Модел (таблица pallet_ledger, db._m016):

* всяко движение е ред с вид палет (EUR / EPAL / друг) и три количества —
  **дадени** (излезли от нас със стоката), **върнати** (партньорът ни ги е
  върнал по-късно) и **разменени** (на място срещу празни 1:1 — дългът
  се погасява веднага);
* **салдо = дадени − върнати − разменени**; положително = партньорът ни
  дължи палети, отрицателно = ние му дължим;
* редовете от ЧМР (source='cmr') се ПРЕЗАПИСВАТ при всеки запис на
  документа (издаване/редакция — sync_document) и се трият с него (ON
  DELETE CASCADE); ръчните корекции (source='manual') ги прави само
  администратор.

Партньорът е ключ `party_key`: „c<id>“/„r<id>“ за клиент/превозвач от
адресната книга/регистъра (салдото оцелява при преименуване), иначе
„n:<име с малки букви>“."""
import re
from datetime import date

#: Видовете палети: ключ в базата → етикет (двуезичен, както бланките).
PALLET_TYPES = ("EUR", "EPAL", "OTHER")
PALLET_TYPE_LABELS = {"EUR": "EUR", "EPAL": "EPAL", "OTHER": "Друг / Other"}

#: Количествата на едно движение.
QTY_FIELDS = ("given", "returned", "exchanged")

#: Видовете партньори.
PARTY_KINDS = ("client", "carrier")

#: Горна граница на едно количество (защита от грешен вход „1000000“).
MAX_QTY = 100000

PAGE_SIZE = 100
#: Таван на редовете в Excel износа на движенията.
EXPORT_LIMIT = 50000

_INT_RE = re.compile(r"\d{1,6}\Z")


def data_key(pallet_type, qty_field):
    """Името на полето в данните на ЧМР: pallet_eur_given и т.н."""
    return "pallet_%s_%s" % (pallet_type.lower(), qty_field)


#: Всички полета на палетната размяна в данните на ЧМР — „Копирай като нов“
#: не ги пренася (палетите са на конкретната пратка).
CMR_DATA_KEYS = tuple(data_key(t, q) for t in PALLET_TYPES for q in QTY_FIELDS) + (
    "pallet_party", "pallet_note")


def parse_qty(value):
    """Количество от формата: "" → 0; цяло число 0…MAX_QTY → int; иначе None
    (невалидно — „-3“, „2,5“, „abc“)."""
    text = re.sub(r"[\s  ]+", "", str(value if value is not None else ""))
    if not text:
        return 0
    if not _INT_RE.match(text):
        return None
    number = int(text)
    return number if number <= MAX_QTY else None


def _norm_name(name):
    return " ".join(str(name or "").split())


def name_key(name):
    return "n:" + _norm_name(name).lower()


def resolve_client(con, name):
    """(party_key, client_id, име) за получателя на ЧМР."""
    name = _norm_name(name)
    if not name:
        return None
    row = con.execute("SELECT id, name FROM clients WHERE ci_lower(TRIM(name)) = ci_lower(?)"
                      " ORDER BY id LIMIT 1", (name,)).fetchone()
    if row is not None:
        return "c%d" % row["id"], row["id"], name
    return name_key(name), None, name


def resolve_carrier(con, carrier_id, carrier_text):
    """(party_key, carrier_id, име) за превозвача на ЧМР: по `carrier_id` от
    регистъра, иначе по името (първата част на кутия 16 до запетаята)."""
    import carriers
    record = None
    raw = str(carrier_id or "").strip()
    if raw.isdigit():
        record = con.execute("SELECT id, name FROM carriers WHERE id = ?", (int(raw),)).fetchone()
    first = _norm_name(str(carrier_text or "").split("\n")[0].split(",")[0])
    if record is None and first:
        found = carriers.find_by_name(con, first)
        if found:
            record = con.execute("SELECT id, name FROM carriers WHERE id = ?", (found,)).fetchone()
    if record is not None:
        return "r%d" % record["id"], record["id"], record["name"]
    if not first:
        return None
    return name_key(first), None, first


def cmr_movements(data):
    """[(вид палет, дадени, върнати, разменени)] от данните на ЧМР (само
    ненулевите) и списък с невалидните полета."""
    out, invalid = [], []
    for ptype in PALLET_TYPES:
        qty = []
        for q in QTY_FIELDS:
            v = parse_qty(data.get(data_key(ptype, q)))
            if v is None:
                invalid.append(data_key(ptype, q))
                v = 0
            qty.append(v)
        if any(qty):
            out.append((ptype,) + tuple(qty))
    return out, invalid


def sync_document(con, doc_type, doc_id, data, user_id=None):
    """Презаписва движенията от ЧМР `doc_id` според данните му (без commit).
    Връща (брой записани редове, невалидни полета, липсващ партньор?)."""
    if doc_type != "cmr" or not doc_id:
        return 0, [], False
    con.execute("DELETE FROM pallet_ledger WHERE doc_id = ? AND source = 'cmr'", (doc_id,))
    movements, invalid = cmr_movements(data or {})
    if not movements:
        return 0, invalid, False
    kind = "carrier" if (data.get("pallet_party") or "").strip() == "carrier" else "client"
    if kind == "carrier":
        party = resolve_carrier(con, data.get("carrier_id"), data.get("carrier"))
    else:
        party = resolve_client(con, data.get("consignee_name"))
    if party is None:
        return 0, invalid, True
    key, pid, name = party
    entry_date = _entry_date(data)
    note = _norm_name(data.get("pallet_note"))[:300]
    for ptype, given, returned, exchanged in movements:
        con.execute(
            "INSERT INTO pallet_ledger (entry_date, party_kind, party_key, party_name, client_id,"
            " carrier_id, pallet_type, given, returned, exchanged, doc_id, doc_number, source,"
            " note, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cmr', ?, ?)",
            (entry_date, kind, key, name, pid if kind == "client" else None,
             pid if kind == "carrier" else None, ptype, given, returned, exchanged, doc_id,
             str(data.get("number") or ""), note, user_id))
    return len(movements), invalid, False


def _entry_date(data):
    for key in ("date_loading", "established_date"):
        value = str(data.get(key) or "").strip()
        if re.match(r"\d{4}-\d{2}-\d{2}\Z", value):
            return value
    return date.today().isoformat()


def add_manual(con, party_kind, party_key, party_name, pallet_type, given, returned,
               exchanged, entry_date, note, user_id, client_id=None, carrier_id=None):
    """Ръчна корекция (администратор). Без commit. Връща id-то на реда."""
    cur = con.execute(
        "INSERT INTO pallet_ledger (entry_date, party_kind, party_key, party_name, client_id,"
        " carrier_id, pallet_type, given, returned, exchanged, source, note, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)",
        (entry_date, party_kind, party_key, party_name, client_id, carrier_id, pallet_type,
         given, returned, exchanged, note, user_id))
    return cur.lastrowid


def resolve_manual_party(con, kind, name):
    """Партньор за ръчна корекция по въведено име (клиент от адресната книга /
    превозвач от регистъра, иначе само по име)."""
    if kind == "carrier":
        return resolve_carrier(con, None, name)
    return resolve_client(con, name)


_BALANCE_COLUMNS = (
    "SUM(CASE WHEN pallet_type = 'EUR' THEN given - returned - exchanged ELSE 0 END) AS bal_eur,"
    " SUM(CASE WHEN pallet_type = 'EPAL' THEN given - returned - exchanged ELSE 0 END) AS bal_epal,"
    " SUM(CASE WHEN pallet_type NOT IN ('EUR', 'EPAL') THEN given - returned - exchanged ELSE 0 END)"
    " AS bal_other,"
    " SUM(given - returned - exchanged) AS bal_total,"
    " SUM(given) AS sum_given, SUM(returned) AS sum_returned, SUM(exchanged) AS sum_exchanged,"
    " COUNT(*) AS movements, MAX(entry_date) AS last_date")


#: Подредба на салдата: клиенти, после превозвачи; по име.
_ORDER = " ORDER BY party_kind, MAX(party_name) COLLATE NOCASE, party_key"


def _balances_where(kind, query):
    where, params = [], []
    if kind in PARTY_KINDS:
        where.append("party_kind = ?")
        params.append(kind)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def balances(con, kind="", query="", nonzero=False, page=1, page_size=PAGE_SIZE, limit=None):
    """Салдата по партньор: (редове, page, total_pages, total_count).
    Името е последното записано за ключа (за клиент/превозвач от
    книгата/регистъра — текущото име там)."""
    where, params = _balances_where(kind, query)
    having = []
    hparams = []
    if query.strip():
        having.append("ci_contains(MAX(party_name), ?)")
        hparams.append(query.strip())
    if nonzero:
        having.append("(SUM(CASE WHEN pallet_type = 'EUR' THEN given - returned - exchanged ELSE 0 END) <> 0"
                      " OR SUM(CASE WHEN pallet_type = 'EPAL' THEN given - returned - exchanged ELSE 0 END) <> 0"
                      " OR SUM(CASE WHEN pallet_type NOT IN ('EUR', 'EPAL') THEN given - returned - exchanged"
                      " ELSE 0 END) <> 0)")
    having_sql = (" HAVING " + " AND ".join(having)) if having else ""
    grouped = ("SELECT party_kind, party_key, MAX(id) AS last_id, MAX(client_id) AS client_id,"  # nosec B608 -- where/having са само от константи и „?“
               " MAX(carrier_id) AS carrier_id, " + _BALANCE_COLUMNS +
               " FROM pallet_ledger" + where + " GROUP BY party_kind, party_key" + having_sql)
    all_params = params + hparams
    total = con.execute("SELECT COUNT(*) FROM (%s)" % grouped, all_params).fetchone()[0]  # nosec B608 -- виж по-горе
    if limit is not None:
        page, total_pages = 1, 1
        rows = con.execute(grouped + _ORDER + " LIMIT ?", all_params + [limit]).fetchall()
    else:
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        rows = con.execute(grouped + _ORDER + " LIMIT ? OFFSET ?",
                           all_params + [page_size, (page - 1) * page_size]).fetchall()
    out = [dict(r) for r in rows]
    _attach_names(con, out)
    return out, page, total_pages, total


def _attach_names(con, rows):
    """display_name: текущото име от адресната книга/регистъра (ако е
    свързан), иначе последното записано име в сметката."""
    if not rows:
        return rows
    last_ids = [r["last_id"] for r in rows]
    names = {r["id"]: r["party_name"] for r in con.execute(
        "SELECT id, party_name FROM pallet_ledger WHERE id IN (%s)" % ",".join("?" * len(last_ids)),  # nosec B608 -- само „?“
        last_ids)}
    client_ids = [int(r["party_key"][1:]) for r in rows if re.match(r"c\d+\Z", r["party_key"])]
    carrier_ids = [int(r["party_key"][1:]) for r in rows if re.match(r"r\d+\Z", r["party_key"])]
    cnames = _names(con, "clients", client_ids)
    rnames = _names(con, "carriers", carrier_ids)
    for r in rows:
        key = r["party_key"]
        current = None
        if re.match(r"c\d+\Z", key):
            current = cnames.get(int(key[1:]))
            r["link_client_id"] = int(key[1:]) if current is not None else None
        elif re.match(r"r\d+\Z", key):
            current = rnames.get(int(key[1:]))
            r["link_carrier_id"] = int(key[1:]) if current is not None else None
        r.setdefault("link_client_id", None)
        r.setdefault("link_carrier_id", None)
        r["display_name"] = current or names.get(r["last_id"]) or "—"
    return rows


def _names(con, table, ids):
    if not ids:
        return {}
    return {r["id"]: r["name"] for r in con.execute(
        "SELECT id, name FROM %s WHERE id IN (%s)" % (table, ",".join("?" * len(ids))), ids)}  # nosec B608 -- таблицата е само от извикващия код (константа), „?“ плейсхолдъри


def party_balance(con, kind, keys):
    """Салдото (по вид палет) на един партньор — сбор по един или няколко
    ключа (напр. „c5“ и „n:име“ за клиент, записан и преди адресната книга).
    Връща речник bal_eur/bal_epal/bal_other/bal_total/movements."""
    keys = [k for k in keys if k]
    empty = {"bal_eur": 0, "bal_epal": 0, "bal_other": 0, "bal_total": 0, "movements": 0,
             "sum_given": 0, "sum_returned": 0, "sum_exchanged": 0, "last_date": None}
    if not keys:
        return empty
    try:
        row = con.execute(
            "SELECT " + _BALANCE_COLUMNS + " FROM pallet_ledger WHERE party_kind = ?"  # nosec B608 -- само константи и „?“
            " AND party_key IN (%s)" % ",".join("?" * len(keys)), [kind] + keys).fetchone()
    except Exception:  # база отпреди миграцията (само за четене) — без карта
        return empty
    if row is None or not row["movements"]:
        return empty
    return {k: (row[k] or 0) if k != "last_date" else row[k] for k in empty}


def client_keys(client_id, client_name):
    return ["c%d" % client_id if client_id else "", name_key(client_name) if client_name else ""]


def carrier_keys(carrier_id, carrier_name):
    return ["r%d" % carrier_id if carrier_id else "", name_key(carrier_name) if carrier_name else ""]


def movements(con, kind, key, page=1, page_size=PAGE_SIZE, limit=None):
    """Движенията на партньор (най-новите първи): (редове, page, total_pages, total)."""
    where = " WHERE l.party_kind = ? AND l.party_key = ?"
    params = [kind, key]
    total = con.execute("SELECT COUNT(*) FROM pallet_ledger l" + where, params).fetchone()[0]  # nosec B608 -- само „?“
    sql = ("SELECT l.*, u.full_name AS author FROM pallet_ledger l"  # nosec B608 -- само „?“
           " LEFT JOIN users u ON u.id = l.created_by" + where +
           " ORDER BY l.entry_date DESC, l.id DESC LIMIT ? OFFSET ?")
    if limit is not None:
        rows = con.execute(sql, params + [limit, 0]).fetchall()
        return [dict(r) for r in rows], 1, 1, total
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    rows = con.execute(sql, params + [page_size, (page - 1) * page_size]).fetchall()
    return [dict(r) for r in rows], page, total_pages, total


def all_movements(con, kind="", limit=EXPORT_LIMIT):
    """Всички движения за Excel износа (най-новите първи), по избор само за вид партньор."""
    where, params = _balances_where(kind, "")
    return [dict(r) for r in con.execute(
        "SELECT l.*, u.full_name AS author FROM pallet_ledger l"  # nosec B608 -- само константи и „?“
        " LEFT JOIN users u ON u.id = l.created_by" + where.replace("party_kind", "l.party_kind") +
        " ORDER BY l.entry_date DESC, l.id DESC LIMIT ?", params + [limit]).fetchall()]


def party_display_name(con, kind, key):
    """Името на партньора за заглавието на страницата с движенията."""
    rows = [{"party_kind": kind, "party_key": key,
             "last_id": (con.execute("SELECT MAX(id) FROM pallet_ledger WHERE party_kind = ?"
                                     " AND party_key = ?", (kind, key)).fetchone()[0])}]
    if rows[0]["last_id"] is None:
        return None
    _attach_names(con, rows)
    return rows[0]


def after_document_saved(con, doc_type, doc_id, data):
    """Закачка от routes_documents (издаване, издаване от прегледа, редакция):
    презаписва палетните движения на ЧМР-то в отделна кратка транзакция.
    Никога не проваля самото издаване — грешката се логва, а потребителят
    вижда предупреждение (документът е записан)."""
    if doc_type != "cmr" or not doc_id:
        return
    try:
        from flask import flash, session
        from flask_babel import gettext as _
    except Exception:  # pragma: no cover - извън Flask
        flash = session = _ = None
    try:
        if not con.in_transaction:
            con.execute("BEGIN IMMEDIATE")
        count, invalid, no_party = sync_document(con, doc_type, doc_id, data,
                                                 session.get("user_id") if session else None)
        con.commit()
    except Exception:
        import contextlib
        with contextlib.suppress(Exception):
            con.rollback()
        import applog
        applog.log_exception("pallet_ledger.after_document_saved: ЧМР id=%s" % doc_id)
        if flash:
            flash(_("ЧМР-то е записано, но палетната сметка НЕ е обновена (грешка при запис). "
                    "Отворете „Редактирай“ и запазете ЧМР-то отново."), "warning")
        return
    if flash is None:
        return
    if invalid:
        flash(_("Палети: невалидно количество в %(count)d поле(та) — броят се като 0. "
                "Въведете цели числа.") % {"count": len(invalid)}, "warning")
    if no_party:
        flash(_("Палетите не са записани в палетна сметка: няма получател/превозвач, на "
                "когото да се отнесат."), "warning")
