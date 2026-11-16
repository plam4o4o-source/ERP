# -*- coding: utf-8 -*-
"""Заявка (16.11.2026): „Като се зареди файл от Excel във фактури да остава
зареден и само да се избира съответната поръчка, не като сега всеки път да се
зарежда файла“.

Последният качен Excel файл във фактурите се помни за потребителя — пазят се
вече прочетените редове (поръчка, позиция, код, описание, количество, цена),
не самият .xlsx. Следващата фактура (и след затваряне на програмата) само
избира поръчка от списъка; нето теглото и описанието от справочника материали
се допълват в момента на зареждане, тоест винаги по текущия справочник.

Списъкът показва и кои поръчки вече имат издадена фактура (по P.O NO в
редовете на фактурите) — за да се вижда коя е следващата.

Файлът е по потребител (всеки оператор работи със своя справка) и остава,
докато не се качи друг или не се натисне „Забрави файла“."""
import json

from flask import session

import applog
import db

#: Колко издадени фактури да се изброят до една поръчка.
MAX_INVOICES_PER_PO = 5


def save(con, user_id, filename, rows, warnings):
    """Запомня прочетените редове на файла (замества предишния). Не прави commit."""
    con.execute(
        "INSERT INTO invoice_import_cache (user_id, filename, uploaded_at, rows_json, warnings_json)"
        " VALUES (?, ?, datetime('now','localtime'), ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET filename = excluded.filename,"
        " uploaded_at = excluded.uploaded_at, rows_json = excluded.rows_json,"
        " warnings_json = excluded.warnings_json",
        (user_id, filename or "", json.dumps(rows, ensure_ascii=False),
         json.dumps(warnings or [], ensure_ascii=False)))


def now_text():
    from datetime import datetime  # pylint: disable=import-outside-toplevel
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load(con, user_id):
    """{"filename", "uploaded_at", "rows", "warnings"} или None."""
    if not user_id:
        return None
    try:
        row = con.execute("SELECT filename, uploaded_at, rows_json, warnings_json"
                          " FROM invoice_import_cache WHERE user_id = ?", (user_id,)).fetchone()
    except Exception:  # pylint: disable=broad-except
        # База отпреди миграция _m020 (не би трябвало — init_db я прилага).
        applog.log_exception("invoice_import_cache.load: таблицата не се прочете")
        return None
    if row is None:
        return None
    try:
        rows = json.loads(row["rows_json"] or "[]")
        warnings = json.loads(row["warnings_json"] or "[]")
    except ValueError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    return {"filename": row["filename"], "uploaded_at": row["uploaded_at"],
            "rows": [r for r in rows if isinstance(r, dict)],
            "warnings": warnings if isinstance(warnings, list) else []}


def clear(con, user_id):
    con.execute("DELETE FROM invoice_import_cache WHERE user_id = ?", (user_id,))


def po_summary(rows):
    """Поръчките във файла по реда на появяване: [{"po_no", "count"}]."""
    order, counts = [], {}
    for r in rows:
        po = str(r.get("po_no") or "").strip()
        if po not in counts:
            counts[po] = 0
            order.append(po)
        counts[po] += 1
    return [{"po_no": po, "count": counts[po]} for po in order]


def invoiced_pos(con, pos):
    """{po: [номера на издадените фактури с тази поръчка в редовете]}.
    Предфилтър по текста на данните (instr), после точна проверка на P.O NO."""
    found = {}
    types = db.INVOICE_DOC_TYPES
    marks = ",".join("?" * len(types))
    for po in pos:
        if not po:
            continue
        rows = con.execute(
            "SELECT number, data FROM documents WHERE doc_type IN (%s)"  # nosec B608 -- само „?“ плейсхолдъри
            " AND instr(data, ?) > 0 ORDER BY year, seq" % marks,
            list(types) + [po]).fetchall()
        numbers = []
        for row in rows:
            try:
                data = json.loads(row["data"] or "{}")
            except ValueError:
                continue
            items = data.get("items") if isinstance(data, dict) else None
            if any(isinstance(it, dict) and str(it.get("po_no") or "").strip() == po
                   for it in items or []):
                numbers.append(row["number"])
        if numbers:
            found[po] = numbers[:MAX_INVOICES_PER_PO]
    return found


def info(con, user_id, cache=None):
    """Данните за панела „Запомнен файл“ (JSON за static/app.js) или None."""
    cache = cache if cache is not None else load(con, user_id)
    if not cache:
        return None
    pos = po_summary(cache["rows"])
    done = invoiced_pos(con, [p["po_no"] for p in pos])
    for p in pos:
        p["invoiced"] = done.get(p["po_no"], [])
    stamp = str(cache["uploaded_at"] or "")
    if len(stamp) >= 16 and stamp[4] == "-":
        stamp = "%s.%s.%s %s" % (stamp[8:10], stamp[5:7], stamp[0:4], stamp[11:16])
    return {"filename": cache["filename"], "uploaded_at": stamp, "pos": pos,
            "rows": len(cache["rows"])}


def info_for_session():
    """Шаблонна функция за формите на фактурите (текущият потребител)."""
    from appcore import get_db  # pylint: disable=import-outside-toplevel
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        return info(get_db(), user_id)
    except Exception:  # pylint: disable=broad-except
        applog.log_exception("invoice_import_cache.info_for_session")
        return None
