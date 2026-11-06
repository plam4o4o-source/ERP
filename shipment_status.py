# -*- coding: utf-8 -*-
"""Статус на пратката, POD, разход/марж, CO2 и „Повтори пратка“.

Заявка (06.11.2026, SHIP): одобрените предложения от анализа на
логистичните програми — „пратката“ минава през етапите чернова →
натоварена → доставена → POD получен → фактурирана, с дата и кой ги е
отбелязал; сканираният подписан ЧМР/POD е прикачен файл с вид „pod“;
разход и марж по пратка (приходът = сборът на свързаните фактури, EUR);
CO2e по данни на превозвача; шаблон по клиент и „Повтори пратка“.

Модел на данните (db._m015_shipment_status):
  * Пратката няма собствен ред — КОТВАТА е ЧМР-то; ако в пратката няма ЧМР
    — опаковъчният лист. Свързаните документи се намират както за „Печат на
    пратката“ (shipment.related_documents — отметнатите редове).
  * shipment_status — един ред на котва (създава се при първото действие);
    `status` е най-далечният отбелязан етап (за индекса и филтъра в /docs).
  * shipment_members — кои документи са в пратката (котвата — също). Пише
    се при действие от лентата и при преглед на котвата, ако нещо се е
    променило — така /docs показва значката без търсене на ред (N+1).
  * document_attachments.kind = 'pod' — POD файлът (attachments.py).

Права: етапите, POD и CO2 — всеки влязъл потребител (служителите и досега
прикачват файлове); разходът и маржът са търговска информация — само за
администратора, освен ако в Настройки е разрешено и за служителите
(MONEY_FOR_EMPLOYEES_KEY)."""
import decimal
import re
import sqlite3
from datetime import date, datetime

from flask import (abort, after_this_request, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_babel import gettext as _
from flask_babel import gettext as _dyn  # превод на готов msgid (не се извлича от pybabel)

import applog
import attachments
import db
from appcore import admin_required, fetch_document, get_db, login_required, safe_json_data

N_ = db.N_

#: Етапите по ред; „чернова“ е началото (без дата).
STEPS = ("draft", "loaded", "delivered", "pod", "invoiced")
MILESTONES = STEPS[1:]
STEP_LABELS = {
    "draft": N_("Чернова"),
    "loaded": N_("Натоварена"),
    "delivered": N_("Доставена"),
    "pod": N_("POD получен"),
    "invoiced": N_("Фактурирана"),
}
#: Видовете документи, които могат да са котва на пратката.
ANCHOR_TYPES = ("cmr", "packing")
#: Филтрите на /docs (?ship=…): етапите + двата готови.
LIST_FILTERS = STEPS + ("uninvoiced", "wait_pod")
FILTER_LABELS = dict(STEP_LABELS, uninvoiced=N_("Доставена, но нефактурирана"),
                     wait_pod=N_("Чака POD"))
#: Настройка (таблица settings): „1“ — служителите също виждат разхода/маржа.
MONEY_FOR_EMPLOYEES_KEY = "shipment_money_for_employees"
#: Горна граница на сумите (EUR) и на CO2e (кг) — защита от грешно въведено.
MAX_AMOUNT = decimal.Decimal("100000000")
MAX_CO2E_KG = 10000000.0
#: „Повтори пратка“: най-много толкова документа в опашката.
REPEAT_MAX_DOCS = 60
_SESSION_REPEAT = "ship_repeat"
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")

#: Шаблон по клиент: полетата, които се предлагат от последния документ за
#: клиента (адресът — само ако адресната книга не го е попълнила; номерата
#: на конкретната пратка — никога). Превозвачът/рег. номерата/шофьорът по
#: клиент се предлагат от група PARTIES (carriers.last_for_client,
#: static/parties.js) — тук не, за да не пишат две места в едни полета.
TEMPLATE_FIELDS = {
    "cmr": ("consignee_address", "consignee_city", "consignee_country", "consignee_vat",
            "place_loading", "place_delivery", "goods", "packing", "stat_no",
            "sender_instructions", "payment_instructions", "special_agreements", "cod"),
    "packing": ("receiver_address", "receiver_city", "receiver_country",
                "terms_delivery", "transport_type", "hs_code", "receiver_contact",
                "receiver_phone", "receiver_email", "notes"),
}
#: Полетата на изпращача (за езика BG/EN — виж routes_documents._apply_sender_lang).
_SENDER_LANG_FIELDS = ("name", "address", "postcode", "city", "country")


def register(app):
    app.add_url_rule("/shipment/<int:anchor_id>/step", "shipment_step", shipment_step,
                     methods=["POST"])
    app.add_url_rule("/shipment/<int:anchor_id>/details", "shipment_details", shipment_details,
                     methods=["POST"])
    app.add_url_rule("/shipment/<int:anchor_id>/pod/<int:attachment_id>", "shipment_pod_mark",
                     shipment_pod_mark, methods=["POST"])
    app.add_url_rule("/shipment/<int:anchor_id>/repeat", "shipment_repeat", shipment_repeat,
                     methods=["GET", "POST"])
    app.add_url_rule("/shipment/repeat/next", "shipment_repeat_next", shipment_repeat_next)
    app.add_url_rule("/shipment/repeat/cancel", "shipment_repeat_cancel", shipment_repeat_cancel,
                     methods=["POST"])
    app.add_url_rule("/shipment/client-template", "shipment_client_template",
                     shipment_client_template)
    app.add_url_rule("/settings/shipment", "shipment_settings", shipment_settings,
                     methods=["POST"])
    app.add_template_global(status_bar, name="shipment_status_bar")
    app.add_template_global(list_badges, name="shipment_list_badges")
    app.add_template_global(waiting_counts, name="shipment_waiting_counts")
    app.add_template_global(money_visible, name="shipment_money_visible")
    app.add_template_global(money_for_employees, name="shipment_money_for_employees")
    app.add_template_global(filter_options, name="shipment_filter_options")
    app.add_template_global(any_status, name="shipment_any_status")
    app.add_template_global(STEP_LABELS, name="shipment_step_labels")


# ---------------------------------------------------------------- помощни

def _today():
    return date.today().isoformat()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def derive_status(row):
    """Най-далечният отбелязан етап (речник/ред с колоните *_at)."""
    status = "draft"
    for step in MILESTONES:
        if row.get(step + "_at"):
            status = step
    return status


def parse_amount_cents(text):
    """„1 200,50“ / „1200.5“ → 120050 (евроцентове); "" → None;
    ValueError при невалидно, отрицателно или прекалено голямо число."""
    raw = re.sub(r"[\s €]|EUR", "", str(text or ""), flags=re.I)
    if not raw:
        return None
    if raw.count(",") == 1 and raw.count(".") >= 1:
        raw = raw.replace(".", "")  # „1.200,50“
    raw = raw.replace(",", ".")
    try:
        value = decimal.Decimal(raw)
    except decimal.InvalidOperation:
        raise ValueError(raw)
    if not value.is_finite() or value < 0 or value > MAX_AMOUNT:
        raise ValueError(raw)
    return int((value * 100).quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))


def parse_co2e(text):
    raw = re.sub(r"\s|kg|кг", "", str(text or ""), flags=re.I).replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(raw)
    if value != value or value < 0 or value > MAX_CO2E_KG:
        raise ValueError(raw)
    return round(value, 3)


def fmt_cents(cents):
    if cents is None:
        return "—"
    sign = "−" if cents < 0 else ""
    cents = abs(int(cents))
    return sign + "{:,}.{:02d} €".format(cents // 100, cents % 100).replace(",", " ")


def fmt_kg(value):
    if value is None:
        return "—"
    text = ("%.3f" % value).rstrip("0").rstrip(".")
    whole, _sep, frac = text.partition(".")
    # Като Справки (routes_reports._fmt_kg): хиляди с интервал, десетична точка.
    return "{:,}".format(int(whole)).replace(",", " ") + ("." + frac if frac else "")


def cents_input(cents):
    """Стойност за полето на формата (без хилядни интервали)."""
    if cents is None:
        return ""
    return "%d,%02d" % (cents // 100, cents % 100)


def _rows(con, sql, params=()):
    """Заявка към таблиците на модула; база без миграция _m015 (стара или само
    за четене) — празен резултат вместо грешка на целия екран."""
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such" in str(exc):
            return []
        raise


def get_status(con, anchor_id):
    rows = _rows(con, "SELECT * FROM shipment_status WHERE document_id = ?", (anchor_id,))
    return dict(rows[0]) if rows else None


def money_for_employees():
    """Настройката „служителите виждат разхода/маржа“ (Настройки)."""
    if "_ship_money_for_employees" not in g:
        rows = _rows(get_db(), "SELECT value FROM settings WHERE key = ?", (MONEY_FOR_EMPLOYEES_KEY,))
        g._ship_money_for_employees = bool(rows) and rows[0]["value"] == "1"
    return g._ship_money_for_employees


def money_visible():
    """Разходът/маржът (търговска информация): администраторът винаги,
    служителят — само ако е разрешено в Настройки."""
    if session.get("role") == "admin":
        return True
    if "user_id" not in session:
        return False
    return money_for_employees()


def any_status():
    """Има ли изобщо отбелязана пратка — филтърът „Статус на пратката“ в /docs
    се показва само тогава (иначе лентата с филтри остава на един ред)."""
    return bool(_rows(get_db(), "SELECT 1 FROM shipment_status LIMIT 1"))


def filter_options():
    return [(key, _(FILTER_LABELS[key])) for key in LIST_FILTERS]


def list_filter_sql(ship):
    """(SQL условие, параметри) за ?ship=… в /docs, или None при непознат
    филтър. Условията са върху малката таблица shipment_status (един ред на
    пратка) — „d.id IN (…)“ минава по първичния ключ на documents."""
    if ship not in LIST_FILTERS:
        return None
    if ship == "uninvoiced":
        return ("d.id IN (SELECT document_id FROM shipment_status WHERE invoiced_at IS NULL"
                " AND (delivered_at IS NOT NULL OR pod_at IS NOT NULL))", [])
    if ship == "wait_pod":
        return ("d.id IN (SELECT document_id FROM shipment_status WHERE delivered_at IS NOT NULL"
                " AND pod_at IS NULL)", [])
    if ship == "draft":
        # Котвите без отбелязан етап: ЧМР/опаковъчни листове без ред (или с
        # ред „чернова“), които не са част от пратка с друга котва.
        return ("d.doc_type IN ('cmr', 'packing')"
                " AND d.id NOT IN (SELECT document_id FROM shipment_status WHERE status <> 'draft')"
                " AND d.id NOT IN (SELECT member_id FROM shipment_members WHERE anchor_id <> member_id)",
                [])
    return ("d.id IN (SELECT document_id FROM shipment_status WHERE status = ?)", [ship])


def list_badges(docs):
    """{id на документ: значка} за редовете на страницата в /docs — две
    заявки за цялата страница (без N+1): статусът на пратката, в която е
    документът (котвата и членовете ѝ), и дали котвата има POD файл."""
    ids = [d["id"] for d in docs]
    if not ids:
        return {}
    con = get_db()
    marks = ",".join("?" for _i in ids)
    out = {}
    anchors = set()
    sql = ("SELECT m.member_id AS doc_id, m.anchor_id, s.status FROM shipment_members m"  # nosec B608 -- само „?“ плейсхолдъри
           " JOIN shipment_status s ON s.document_id = m.anchor_id WHERE m.member_id IN (%s)"
           " UNION SELECT s.document_id, s.document_id, s.status FROM shipment_status s"
           " WHERE s.document_id IN (%s)" % (marks, marks))
    for r in _rows(con, sql, ids + ids):
        out[r["doc_id"]] = {"status": r["status"], "label": _(STEP_LABELS.get(r["status"], r["status"])),
                            "anchor_id": r["anchor_id"], "is_anchor": r["anchor_id"] == r["doc_id"],
                            "pod": False}
        anchors.add(r["anchor_id"])
    if anchors:
        a_ids = sorted(anchors)
        # POD файлът е на котвата или (пратка, започнала от опаковъчен лист)
        # на член на пратката — котвата е член на себе си.
        sql = ("SELECT DISTINCT m.anchor_id FROM shipment_members m"  # nosec B608 -- само „?“ плейсхолдъри
               " JOIN document_attachments a ON a.document_id = m.member_id AND a.kind = 'pod'"
               " WHERE m.anchor_id IN (%s)" % ",".join("?" for _i in a_ids))
        with_pod = {r["anchor_id"] for r in _rows(con, sql, a_ids)}
        for badge in out.values():
            badge["pod"] = badge["is_anchor"] and badge["anchor_id"] in with_pod
    return out


def waiting_counts():
    """Плочката на таблото „Чакат POD / фактура“ (пратки, не фактури)."""
    rows = _rows(get_db(),
                 "SELECT SUM(delivered_at IS NOT NULL AND pod_at IS NULL) AS wait_pod,"
                 " SUM(invoiced_at IS NULL AND (delivered_at IS NOT NULL OR pod_at IS NOT NULL))"
                 " AS uninvoiced FROM shipment_status")
    if not rows:
        return {"wait_pod": 0, "uninvoiced": 0}
    return {"wait_pod": rows[0]["wait_pod"] or 0, "uninvoiced": rows[0]["uninvoiced"] or 0}


# ---------------------------------------------------------------- пратката

def _strong_rows(rows):
    return [r for r in (rows or []) if r.get("checked")]


def resolve_anchor(doc_type, doc_id, rows):
    """(id, вид, номер) на котвата на пратката на документа или None: ЧМР-то
    на пратката; без ЧМР — опаковъчният лист (самият той, ако е такъв)."""
    strong = [r for r in _strong_rows(rows) if not r.get("is_start")]
    cmr = next((r for r in strong if r["doc_type"] == "cmr"), None)
    if doc_type == "cmr":
        return doc_id
    if cmr:
        return cmr["ids"][0]
    if doc_type == "packing":
        return doc_id
    packing = next((r for r in strong if r["doc_type"] == "packing"), None)
    return packing["ids"][0] if packing else None


def contract_price_cents(data):
    """Договорената цена към превозвача от ЧМР-то (поле to_price_eur на група
    PARTIES, „Поръчка за превоз“) в центове или None — разходът за превоз по
    подразбиране, докато не е въведен друг в лентата на пратката."""
    try:
        return parse_amount_cents((data or {}).get("to_price_eur"))
    except ValueError:
        return None


def _members_and_invoices(anchor_id, rows):
    members, invoices = {anchor_id}, []
    for r in _strong_rows(rows):
        members.update(r["ids"])
        if r["doc_type"] in db.INVOICE_DOC_TYPES:
            invoices.extend(r["ids"])
    return members, sorted(set(invoices))


def invoice_amounts(con, invoice_ids):
    """{id: (номер, сума в центове или None, дата на издаване)} — сумата е
    ТОЧНО тази под таблицата на фактурата (appcore.invoice_totals; виж
    routes_reports.invoice_cents)."""
    if not invoice_ids:
        return {}
    from routes_reports import invoice_cents_cached
    # Едно четене на фактурите на заявка (лентата и проверката за запис).
    memo = g.setdefault("_ship_invoice_amounts", {})
    key = tuple(sorted(invoice_ids))
    if key in memo:
        return memo[key]
    out = memo[key] = {}
    sql = ("SELECT id, number, created_at, data FROM documents WHERE id IN (%s)"  # nosec B608 -- само „?“ плейсхолдъри
           % ",".join("?" for _i in invoice_ids))
    for r in con.execute(sql, list(invoice_ids)).fetchall():
        out[r["id"]] = (r["number"], invoice_cents_cached(r["data"]), str(r["created_at"] or "")[:10])
    return out


def _revenue(amounts):
    priced = [c for _n, c, _d in amounts.values() if c is not None]
    return sum(priced) if priced else None


def _stored_members(con, anchor_id):
    return {r["member_id"] for r in _rows(con, "SELECT member_id FROM shipment_members"
                                               " WHERE anchor_id = ?", (anchor_id,))}


def _write_members(con, anchor_id, members):
    con.execute("DELETE FROM shipment_members WHERE anchor_id = ? AND member_id NOT IN (%s)"  # nosec B608 -- само „?“ плейсхолдъри
                % ",".join("?" for _m in members), [anchor_id] + sorted(members))
    con.executemany("INSERT OR REPLACE INTO shipment_members (member_id, anchor_id) VALUES (?, ?)",
                    [(m, anchor_id) for m in sorted(members)])


def _ensure_row(con, anchor_id):
    con.execute("INSERT OR IGNORE INTO shipment_status (document_id, status) VALUES (?, 'draft')",
                (anchor_id,))


def _auto_invoice(st, invoice_ids, amounts, changed_invoices):
    """Автоматично „фактурирана“: има свързана фактура, пратката е доставена
    (или POD е получен) и етапът още не е отбелязан. Ръчно отменен етап
    (invoiced_auto = -1) не се връща сам, докато фактурите не се сменят."""
    if not invoice_ids or st.get("invoiced_at"):
        return None
    if not (st.get("delivered_at") or st.get("pod_at")):
        return None
    if st.get("invoiced_auto") == -1 and not changed_invoices:
        return None
    dates = [d for _n, _c, d in amounts.values() if d]
    return max(dates) if dates else _today()


def refresh(con, anchor_id, rows):
    """Членовете, приходът и автоматичното „фактурирана“ на пратката —
    в текущата писателска транзакция. Връща речника на реда след записа."""
    members, invoice_ids = _members_and_invoices(anchor_id, rows)
    amounts = invoice_amounts(con, invoice_ids)
    _ensure_row(con, anchor_id)
    st = get_status(con, anchor_id)
    ids_text = ",".join(str(i) for i in invoice_ids)
    changed_invoices = ids_text != (st.get("invoice_ids") or "")
    _write_members(con, anchor_id, members)
    fields = {"revenue_cents": _revenue(amounts), "invoice_ids": ids_text}
    if changed_invoices and st.get("invoiced_auto") == -1:
        fields["invoiced_auto"] = 0
    auto_date = _auto_invoice(st, invoice_ids, amounts, changed_invoices)
    if auto_date:
        fields.update(invoiced_at=auto_date, invoiced_by=None, invoiced_auto=1)
    _update(con, anchor_id, fields, touch=False)
    return get_status(con, anchor_id)


def _update(con, anchor_id, fields, touch=True):
    if touch:
        fields = dict(fields, updated_at=_now(), updated_by=session.get("user_id"))
    if fields:
        cols = sorted(fields)
        con.execute("UPDATE shipment_status SET %s WHERE document_id = ?"  # nosec B608 -- имената на колоните са от кода
                    % ", ".join("%s = ?" % c for c in cols), [fields[c] for c in cols] + [anchor_id])
    st = get_status(con, anchor_id)
    status = derive_status(st)
    if status != st["status"]:
        con.execute("UPDATE shipment_status SET status = ? WHERE document_id = ?", (status, anchor_id))
        st["status"] = status
    return st


def _needs_refresh(con, anchor_id, st, rows):
    members, invoice_ids = _members_and_invoices(anchor_id, rows)
    if members != _stored_members(con, anchor_id):
        return True
    if ",".join(str(i) for i in invoice_ids) != (st.get("invoice_ids") or ""):
        return True
    amounts = invoice_amounts(con, invoice_ids)
    if _revenue(amounts) != st.get("revenue_cents"):
        return True
    return bool(_auto_invoice(st, invoice_ids, amounts, False))


def _adoptable(con, anchor_id, rows):
    """Котвата (ЧМР) още няма ред, а опаковъчен лист от пратката има —
    пратката е започнала без ЧМР. Връща id-то на листа или None."""
    for r in _strong_rows(rows):
        if r["doc_type"] == "packing":
            for pid in r["ids"]:
                if pid != anchor_id and get_status(con, pid):
                    return pid
    return None


def _adopt(con, from_id, to_id):
    """Редът на пратката минава от опаковъчния лист към ЧМР-то."""
    con.execute("UPDATE shipment_status SET document_id = ? WHERE document_id = ?"
                " AND NOT EXISTS (SELECT 1 FROM shipment_status WHERE document_id = ?)",
                (to_id, from_id, to_id))
    con.execute("UPDATE shipment_members SET anchor_id = ? WHERE anchor_id = ?", (to_id, from_id))


def _write_later(anchor_id, rows, adopt_from=None):
    """Записът при преглед — СЛЕД изгледа (read_snapshot е само за четене,
    виж appcore.read_snapshot): кратка писателска транзакция в after_request."""
    @after_this_request
    def _apply(response):
        con = get_db()
        try:
            if con.in_transaction:
                con.rollback()
            con.execute("BEGIN IMMEDIATE")
            if adopt_from:
                _adopt(con, adopt_from, anchor_id)
            if get_status(con, anchor_id):
                refresh(con, anchor_id, rows)
            con.commit()
        except Exception:  # pylint: disable=broad-except
            if con.in_transaction:
                con.rollback()
            applog.log_exception("shipment_status: неуспешно обновяване на пратката %s" % anchor_id)
        return response


def _user_names(con, ids):
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    sql = ("SELECT id, full_name, username FROM users WHERE id IN (%s)"  # nosec B608 -- само „?“ плейсхолдъри
           % ",".join("?" for _i in ids))
    return {r["id"]: r["full_name"] or r["username"] for r in con.execute(sql, ids).fetchall()}


def _steps_view(st, names):
    status = st["status"] if st else "draft"
    reached = STEPS.index(status)
    out = []
    for i, step in enumerate(STEPS):
        item = {"key": step, "label": _(STEP_LABELS[step]), "done": i <= reached,
                "current": step == status, "date": "", "by": "", "auto": False}
        if step != "draft" and st:
            item["date"] = st.get(step + "_at") or ""
            item["by"] = names.get(st.get(step + "_by"), "")
            item["auto"] = step == "invoiced" and st.get("invoiced_auto") == 1 and bool(item["date"])
            item["skipped"] = item["done"] and not item["date"]
        out.append(item)
    return out


def _repeat_banner():
    state = session.get(_SESSION_REPEAT)
    if not state or not state.get("queue"):
        return None
    return {"left": len(state["queue"]), "total": state.get("total", len(state["queue"])),
            "next_title": state.get("titles", {}).get(str(state["queue"][0]), "")}


def status_bar(doc):
    """Шаблонна функция за лентата „Статус на пратката“ на изгледа на
    документ (_macros.doc_toolbar). None — без лента (предварителен преглед,
    друг екран, документ без пратка)."""
    try:
        if not doc or not doc["id"] or request.endpoint != "view_document":
            return None
    except (KeyError, IndexError, TypeError):
        return None
    banner = _repeat_banner()
    try:
        return _status_bar(doc, banner)
    except Exception:  # pylint: disable=broad-except
        applog.log_exception("shipment_status.status_bar: неуспешно показване на пратката")
        return {"banner": banner} if banner else None


def _status_bar(doc, banner):
    import shipment
    doc_type = doc["doc_type"]
    if doc_type not in ANCHOR_TYPES + ("pallet",) + tuple(db.INVOICE_DOC_TYPES):
        return {"banner": banner} if banner else None
    rows = shipment.related_rows_for_view(doc) or []
    anchor_id = resolve_anchor(doc_type, doc["id"], rows)
    if anchor_id is None:
        return {"banner": banner} if banner else None
    con = get_db()
    st = get_status(con, anchor_id)
    is_anchor = anchor_id == doc["id"]
    adopt_from = None
    if st is None and is_anchor and doc_type == "cmr":
        adopt_from = _adoptable(con, anchor_id, rows)
        if adopt_from:
            st = get_status(con, adopt_from)
            st["document_id"] = anchor_id
    elif st is None and not is_anchor and doc_type == "packing" and get_status(con, doc["id"]):
        # Пратката е започнала от този лист, после е издадено ЧМР — редът
        # минава към ЧМР-то; лентата тук вече сочи ЧМР-то.
        adopt_from = doc["id"]
        st = get_status(con, doc["id"])
    if not is_anchor and doc_type not in ANCHOR_TYPES and st is None:
        return {"banner": banner} if banner else None
    if adopt_from:
        if is_anchor:
            _write_later(anchor_id, rows, adopt_from=adopt_from)
        else:
            _write_later_adopt_only(adopt_from, anchor_id)
    elif is_anchor and st is not None and _needs_refresh(con, anchor_id, st, rows):
        _write_later(anchor_id, rows)
    anchor_row = doc if is_anchor else con.execute(
        "SELECT id, doc_type, number FROM documents WHERE id = ?", (anchor_id,)).fetchone()
    if anchor_row is None:
        return {"banner": banner} if banner else None
    names = _user_names(con, [st.get(s + "_by") for s in MILESTONES] if st else [])
    view = {
        "banner": banner, "anchor_id": anchor_id, "is_anchor": is_anchor,
        "anchor_type": anchor_row["doc_type"], "anchor_number": anchor_row["number"],
        "anchor_title": _dyn(db.DOC_TYPES.get(anchor_row["doc_type"], {}).get("title", "")),
        "status": st["status"] if st else "draft",
        "status_label": _dyn(STEP_LABELS[st["status"] if st else "draft"]),
        "steps": _steps_view(st, names), "today": _today(),
        "co2e": st.get("co2e_kg") if st else None,
        "co2e_text": fmt_kg(st.get("co2e_kg")) if st else "—",
        "money": money_visible(),
    }
    if not is_anchor:
        return view
    members, invoice_ids = _members_and_invoices(anchor_id, rows)
    amounts = invoice_amounts(con, invoice_ids)
    revenue = _revenue(amounts)
    transport = st.get("transport_cost_cents") if st else None
    contract = contract_price_cents(safe_json_data(doc["data"])) if doc_type == "cmr" else None
    from_contract = transport is None and contract is not None
    if from_contract:
        transport = contract
    other = st.get("other_cost_cents") if st else None
    cost = None if transport is None and other is None else (transport or 0) + (other or 0)
    margin = revenue - cost if (cost is not None and revenue is not None) else None
    member_ids = sorted(members)
    atts = _rows(con, "SELECT id, document_id, filename, kind, uploaded_at FROM document_attachments"  # nosec B608 -- само „?“ плейсхолдъри
                      " WHERE document_id IN (%s) ORDER BY id DESC" % ",".join("?" for _m in member_ids),
                 member_ids)
    view.update({
        "invoices": [{"id": i, "number": amounts[i][0], "amount": fmt_cents(amounts[i][1])}
                     for i in invoice_ids if i in amounts],
        "revenue_text": fmt_cents(revenue), "cost_text": fmt_cents(cost),
        "margin_text": fmt_cents(margin),
        "margin_pct": ("%.0f %%" % (100.0 * margin / revenue)) if (margin is not None and revenue) else "",
        "margin_negative": margin is not None and margin < 0,
        "transport_input": "" if from_contract else cents_input(transport),
        "other_input": cents_input(other),
        "contract_text": fmt_cents(contract) if contract is not None else "",
        "cost_from_contract": from_contract,
        "cost_note": (st.get("cost_note") or "") if st else "",
        "co2e_input": ("%s" % fmt_kg(st.get("co2e_kg"))).replace(" ", "") if st and st.get("co2e_kg") is not None else "",
        "pods": [a for a in atts if a["kind"] == "pod"],
        "other_atts": [a for a in atts if a["kind"] != "pod" and a["document_id"] == anchor_id],
        "member_count": len(members),
    })
    return view


def _write_later_adopt_only(from_id, to_id):
    @after_this_request
    def _apply(response):
        con = get_db()
        try:
            if con.in_transaction:
                con.rollback()
            con.execute("BEGIN IMMEDIATE")
            _adopt(con, from_id, to_id)
            con.commit()
        except Exception:  # pylint: disable=broad-except
            if con.in_transaction:
                con.rollback()
            applog.log_exception("shipment_status: неуспешно преместване на пратката %s" % from_id)
        return response


# ---------------------------------------------------------------- действия

def _anchor_or_404(con, anchor_id):
    row, data = fetch_document(con, anchor_id)
    if row["doc_type"] not in ANCHOR_TYPES:
        abort(404)
    return row, data


def _related_for(con, row, data):
    import shipment
    return shipment.related_documents(con, row, data)


def _back(anchor_id):
    return redirect(url_for("view_document", doc_id=anchor_id))


def _label_of(row):
    return "%s № %s" % (db.DOC_TYPES.get(row["doc_type"], {}).get("title", row["doc_type"]),
                        row["number"])


@login_required
def shipment_step(anchor_id):
    """Отбелязва/отменя етап (дата + кой). „POD получен“ — с качване на
    сканирания подписан документ или с отбелязване на вече прикачен файл."""
    con = get_db()
    row, data = _anchor_or_404(con, anchor_id)
    step = request.form.get("step", "")
    action = request.form.get("action", "set")
    if step not in MILESTONES or action not in ("set", "clear"):
        abort(400)
    day = (request.form.get("date") or "").strip() or _today()
    if action == "set":
        if not _DATE_RE.match(day):
            flash(_("Невалидна дата — изберете дата от календара."), "error")
            return _back(anchor_id)
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            flash(_("Невалидна дата — изберете дата от календара."), "error")
            return _back(anchor_id)
    pod_file = request.files.get("pod_file") if step == "pod" and action == "set" else None
    if pod_file is not None and not pod_file.filename:
        pod_file = None
    new_attachment = None
    if pod_file is not None:
        try:
            new_attachment = attachments.save_attachment(con, anchor_id, pod_file,
                                                         uploaded_by=session.get("user_id"))
        except ValueError as exc:
            flash("%s" % exc, "error")
            return _back(anchor_id)
    rows = _related_for(con, row, data)
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure_row(con, anchor_id)
        if new_attachment:
            con.execute("UPDATE document_attachments SET kind = 'pod' WHERE id = ?", (new_attachment,))
        mark_id = request.form.get("pod_attachment_id", type=int) if step == "pod" else None
        if mark_id:
            con.execute("UPDATE document_attachments SET kind = 'pod' WHERE id = ? AND document_id = ?",
                        (mark_id, anchor_id))
        if action == "set":
            fields = {step + "_at": day, step + "_by": session.get("user_id")}
            if step == "invoiced":
                fields["invoiced_auto"] = 0
        else:
            fields = {step + "_at": None, step + "_by": None}
            if step == "invoiced":
                fields["invoiced_auto"] = -1
        _update(con, anchor_id, fields)
        st = refresh(con, anchor_id, rows)
        has_pod = con.execute("SELECT 1 FROM shipment_members m JOIN document_attachments a"
                              " ON a.document_id = m.member_id AND a.kind = 'pod'"
                              " WHERE m.anchor_id = ? LIMIT 1", (anchor_id,)).fetchone() is not None
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    label = STEP_LABELS[step]
    applog.log_audit("статус на пратката: %s „%s“" % ("отбелязан" if action == "set" else "отменен", label),
                     "%s, дата %s%s" % (_label_of(row), day if action == "set" else "—",
                                        ", POD файл #%s" % new_attachment if new_attachment else ""))
    if action == "set":
        flash(_("Пратката е отбелязана: „%(step)s“ (%(date)s).")
              % {"step": _(label), "date": day[8:10] + "." + day[5:7] + "." + day[:4]}, "success")
        if step == "pod" and not has_pod:
            flash(_("POD е отбелязан без прикачен файл — прикачете сканирания подписан ЧМР/POD от "
                    "лентата на пратката, когато го получите."), "warning")
        if st.get("invoiced_auto") == 1 and step != "invoiced" and st.get("invoiced_at"):
            flash(_("Пратката има свързана фактура — отбелязана е и като „Фактурирана“ (авто)."), "info")
    else:
        flash(_("Етапът „%(step)s“ е отменен.") % {"step": _(label)}, "info")
    return _back(anchor_id)


@login_required
def shipment_details(anchor_id):
    """Разход (само ако разходът/маржът е видим за потребителя) и CO2e."""
    con = get_db()
    row, data = _anchor_or_404(con, anchor_id)
    fields, errors = {}, []
    if "co2e_kg" in request.form:
        try:
            fields["co2e_kg"] = parse_co2e(request.form.get("co2e_kg"))
        except ValueError:
            errors.append(_("CO2e, кг"))
    if money_visible():
        for key, label in (("transport_cost", _("Разход за превоз, EUR")),
                           ("other_cost", _("Други разходи, EUR"))):
            if key in request.form:
                try:
                    fields[key + "_cents"] = parse_amount_cents(request.form.get(key))
                except ValueError:
                    errors.append(label)
        if "cost_note" in request.form:
            fields["cost_note"] = " ".join((request.form.get("cost_note") or "").split())[:300]
    if errors:
        flash(_("Не е записано — невалидна стойност в: %(fields)s. Въведете число ≥ 0, напр. 1 250,50.")
              % {"fields": ", ".join("„%s“" % e for e in errors)}, "error")
        return _back(anchor_id)
    if not fields:
        return _back(anchor_id)
    rows = _related_for(con, row, data)
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure_row(con, anchor_id)
        _update(con, anchor_id, fields)
        refresh(con, anchor_id, rows)
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    applog.log_audit("данни на пратката (разход/CO2)", "%s, полета=%s" % (_label_of(row), ", ".join(sorted(fields))))
    flash(_("Данните на пратката са запазени."), "success")
    return _back(anchor_id)


@login_required
def shipment_pod_mark(anchor_id, attachment_id):
    """Отбелязва вече прикачен файл като POD (или маха отметката)."""
    con = get_db()
    row, _data = _anchor_or_404(con, anchor_id)
    kind = "" if request.form.get("action") == "unmark" else "pod"
    cur = con.execute("UPDATE document_attachments SET kind = ? WHERE id = ? AND document_id = ?",
                      (kind, attachment_id, anchor_id))
    con.commit()
    if not cur.rowcount:
        abort(404)
    applog.log_audit("POD: %s" % ("отбелязан файл" if kind else "махната отметка"),
                     "%s, файл #%s" % (_label_of(row), attachment_id))
    flash(_("Файлът е отбелязан като POD.") if kind else _("Отметката „POD“ е махната."), "success")
    return _back(anchor_id)


# ---------------------------------------------------------------- „Повтори пратка“

_REPEAT_ORDER = {"cmr": 0, "packing": 1, "invoice_br": 2, "invoice_no": 2, "invoice_dubai": 2,
                 "dualuse": 3, "export_it": 3, "waybill": 4, "pallet": 5}
#: По подразбиране отметнати: ЧМР и опаковъчният лист (останалите — по избор).
_REPEAT_DEFAULT = ("cmr", "packing")


@login_required
def shipment_repeat(anchor_id):
    """„Повтори пратка“: избор кои документи на пратката да се копират като
    нови (логиката на „Копирай като нов“ — без номера, дати и препратките
    към конкретната пратка); формите се отварят една след друга."""
    con = get_db()
    row, data = _anchor_or_404(con, anchor_id)
    rows = [r for r in _strong_rows(_related_for(con, row, data))]
    rows.sort(key=lambda r: (_REPEAT_ORDER.get(r["doc_type"], 9), r["ids"][0]))
    if request.method == "POST":
        wanted = set(request.form.getlist("doc"))
        queue, titles = [], {}
        for r in rows:
            if str(r["id"]) in wanted:
                for i in r["ids"]:
                    if len(queue) < REPEAT_MAX_DOCS:
                        queue.append(i)
                        titles[str(i)] = r["title"] if len(r["ids"]) == 1 else _dyn(
                            db.DOC_TYPES["pallet"]["title"])
        if not queue:
            flash(_("Не е избран нито един документ."), "warning")
            return redirect(url_for("shipment_repeat", anchor_id=anchor_id))
        session[_SESSION_REPEAT] = {"anchor": anchor_id, "queue": queue, "total": len(queue),
                                    "titles": titles}
        applog.log_audit("повтори пратка", "%s, %d документа" % (_label_of(row), len(queue)))
        return redirect(url_for("shipment_repeat_next"))
    items = [{"id": r["id"], "title": r["title"], "doc_type": r["doc_type"], "count": len(r["ids"]),
              "checked": r["doc_type"] in _REPEAT_DEFAULT} for r in rows]
    return render_template("shipment_repeat.html", row=row, items=items,
                           anchor_title=_dyn(db.DOC_TYPES[row["doc_type"]]["title"]))


@login_required
def shipment_repeat_next():
    state = session.get(_SESSION_REPEAT)
    if not state or not state.get("queue"):
        session.pop(_SESSION_REPEAT, None)
        flash(_("„Повтори пратка“ е завършено."), "info")
        return redirect(url_for("documents"))
    queue = list(state["queue"])
    doc_id = queue.pop(0)
    step_no = state["total"] - len(queue)
    if queue:
        state["queue"] = queue
        session[_SESSION_REPEAT] = state
    else:
        session.pop(_SESSION_REPEAT, None)
    flash(_("Повтори пратка — документ %(n)d от %(total)d. След издаването му продължете от "
            "лентата на новия документ.") % {"n": step_no, "total": state["total"]}
          if queue else _("Повтори пратка — последен документ (%(n)d от %(total)d).")
          % {"n": step_no, "total": state["total"]}, "info")
    return redirect(url_for("copy_document", doc_id=doc_id))


@login_required
def shipment_repeat_cancel():
    session.pop(_SESSION_REPEAT, None)
    flash(_("„Повтори пратка“ е прекратено."), "info")
    target = request.form.get("next") or ""
    # Само адрес в програмата (без „//host“) — не отворен пренасочващ линк.
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        target = url_for("documents")
    return redirect(target)


# ---------------------------------------------------------------- шаблон по клиент

def client_template(con, doc_type, client):
    """Предложенията от последния издаден документ от този вид за клиента
    (същият ключ на клиента като филтъра „Клиент“): {"fields", "number",
    "date", "doc_id", "sender_lang"} или None."""
    if doc_type not in TEMPLATE_FIELDS or not (client or "").strip():
        return None
    names = db.client_names_for_key(con, db.client_match_key(client))
    if not names:
        return None
    sql = ("SELECT id, number, created_at, data FROM documents WHERE doc_type = ?"  # nosec B608 -- само „?“ плейсхолдъри
           " AND client_name IN (%s) ORDER BY id DESC LIMIT 1" % ",".join("?" for _n in names))
    r = con.execute(sql, [doc_type] + names).fetchone()
    if r is None:
        return None
    data = safe_json_data(r["data"])
    fields = {}
    for key in TEMPLATE_FIELDS[doc_type]:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value
    settings = db.get_settings(con)
    sender_lang = ""
    sender_name = (data.get("sender_name") or "").strip()
    if sender_name:
        if sender_name == (settings.get("sender_name_en") or "").strip():
            sender_lang = "en"
        elif sender_name == (settings.get("sender_name") or "").strip():
            sender_lang = "bg"
    sender = {}
    if sender_lang and (settings.get("sender_name_en") or "").strip():
        for lang in ("bg", "en"):
            suffix = "_en" if lang == "en" else ""
            sender[lang] = {f: settings.get("sender_%s%s" % (f, suffix), "") for f in _SENDER_LANG_FIELDS}
    return {"fields": fields, "number": r["number"], "doc_id": r["id"],
            "date": str(r["created_at"] or "")[:10], "sender_lang": sender_lang, "sender": sender}


@login_required
def shipment_client_template():
    con = get_db()
    doc_type = request.args.get("type", "")
    result = client_template(con, doc_type, request.args.get("client", ""))
    if not result:
        return jsonify({"found": False})
    title = _dyn(db.DOC_TYPES[doc_type]["title"])
    day = result["date"]
    return jsonify({"found": True, "fields": result["fields"], "sender_lang": result["sender_lang"],
                    "sender": result["sender"],
                    "texts": {"badge": _("авто"),
                              "badge_title": _("Предложено по последния документ за този клиент — "
                                               "можете да го промените."),
                              "note": _("Попълнени са {count} празни полета по {source} за този "
                                        "клиент (означени „авто“) — проверете ги."),
                              "undo": _("Махни предложенията")},
                    "source": _("%(title)s № %(number)s от %(date)s") % {
                        "title": title, "number": result["number"],
                        "date": "%s.%s.%s" % (day[8:10], day[5:7], day[:4]) if day else "—"}})


# ---------------------------------------------------------------- настройки

@admin_required
def shipment_settings():
    con = get_db()
    value = "1" if request.form.get("money_for_employees") == "1" else "0"
    db.save_settings(con, {MONEY_FOR_EMPLOYEES_KEY: value})
    con.commit()
    applog.log_audit("настройка: разход/марж за служителите", "включено" if value == "1" else "изключено")
    flash(_("Настройката за разхода и маржа на пратките е запазена."), "success")
    return redirect(url_for("settings_page") + "#shipment-settings")


# ---------------------------------------------------------------- справки

def report_rows(con, start, end_exclusive, client_key_text=""):
    """Пратките (котвите с ред в shipment_status) в периода — за колоните
    „Разход / Приход / Марж / CO2e“ на Справки (routes_reports): списък от
    (клиент, „ГГГГ-ММ“, разход в центове или None, приход в центове или
    None, CO2e кг или None). Приходът = свързаните фактури на пратката;
    разходът за превоз — въведеният, иначе договорената цена от ЧМР-то
    (to_price_eur, група PARTIES)."""
    import routes_reports
    routes_reports._register_sql_functions(con)
    sql = ("SELECT d.id, d.client_name, substr(d.created_at, 1, 7) AS ym,"
           " s.transport_cost_cents AS t, s.other_cost_cents AS o, s.co2e_kg AS co2,"
           " CASE WHEN s.transport_cost_cents IS NULL AND d.doc_type = 'cmr' AND json_valid(d.data)"
           " THEN json_extract(d.data, '$.to_price_eur') END AS to_price"
           # CROSS JOIN: documents води по индекса на created_at (като
           # останалите заявки на справката), shipment_status — по ключа.
           " FROM documents d CROSS JOIN shipment_status s ON s.document_id = d.id"
           " WHERE d.created_at >= ? AND d.created_at < ? AND +d.doc_type IN ('cmr', 'packing')")
    params = [start, end_exclusive]
    if client_key_text:
        sql += " AND instr(rep_client_key(d.client_name), ?) > 0"
        params.append(client_key_text)
    rows = _rows(con, sql, params)
    if not rows:
        return []
    revenue = {}
    ids = [r["id"] for r in rows]
    inv_marks = ",".join("?" for _t in db.INVOICE_DOC_TYPES)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        sql = ("SELECT m.anchor_id, SUM(rep_invoice_cents(i.data)) AS cents FROM shipment_members m"  # nosec B608 -- само „?“ плейсхолдъри
               " JOIN documents i ON i.id = m.member_id"
               " WHERE m.anchor_id IN (%s) AND i.doc_type IN (%s) GROUP BY m.anchor_id"
               % (",".join("?" for _c in chunk), inv_marks))
        for r in con.execute(sql, chunk + list(db.INVOICE_DOC_TYPES)).fetchall():
            revenue[r["anchor_id"]] = r["cents"]
    out = []
    for r in rows:
        transport = r["t"]
        if transport is None and r["to_price"] is not None:
            transport = contract_price_cents({"to_price_eur": str(r["to_price"])})
        cost = None if transport is None and r["o"] is None else (transport or 0) + (r["o"] or 0)
        out.append((r["client_name"], r["ym"], cost, revenue.get(r["id"]), r["co2"]))
    return out
