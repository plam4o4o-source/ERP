# -*- coding: utf-8 -*-
"""Табло, сканиране на баркод и генериране на баркод SVG. Извлечено от
app.py (Фаза 3) без промяна в поведението."""
import json
from datetime import date, timedelta

from flask import Response, flash, redirect, render_template, request, url_for

import client_export
import db
import updater
from appcore import get_db, login_required
from barcode128 import code128_svg


def register(app):
    app.add_url_rule("/", "dashboard", dashboard)
    app.add_url_rule("/scan", "scan", scan, methods=["POST"])
    app.add_url_rule("/barcode/<code>.svg", "barcode_svg", barcode_svg)


def _month_bounds(d):
    """Връща (начало, начало на следващия месец) за месеца, съдържащ `d` —
    полуотворен интервал, удобен за `>= date(?) AND < date(?)` в SQL."""
    start = d.replace(day=1)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    return start, next_start


def _dashboard_stats(con, today=None):
    """Обобщено табло/статистика — заявка: „направи всичко което
    предлагаш“ (списък с предложения за подобрения). Брой документи за
    текущия календарен месец спрямо предходния (проста тенденция, без
    нужда от графична библиотека) и топ 5 клиента по брой документи за
    текущия месец (по СЪЩОТО правило за име на клиент като историята на
    клиента и групирането по клиент в „Издадени документи“)."""
    today = today or date.today()
    cur_start, cur_end = _month_bounds(today)
    prev_start, prev_end = _month_bounds(cur_start - timedelta(days=1))
    # Фактурите не участват в статистиката на таблото — заявка: „и от
    # таблото/историята на клиента“ (те се броят само в собствения си
    # раздел). Виж db.INVOICE_DOC_TYPES.
    not_invoice = " AND doc_type NOT IN (%s)" % ",".join("?" for _ in db.INVOICE_DOC_TYPES)
    invoice_params = list(db.INVOICE_DOC_TYPES)
    month_count = con.execute(
        "SELECT COUNT(*) AS c FROM documents"
        " WHERE date(created_at) >= date(?) AND date(created_at) < date(?)" + not_invoice,  # nosec B608 -- само „?“ плейсхолдъри по брой
        [cur_start.isoformat(), cur_end.isoformat()] + invoice_params,
    ).fetchone()["c"]
    prev_month_count = con.execute(
        "SELECT COUNT(*) AS c FROM documents"
        " WHERE date(created_at) >= date(?) AND date(created_at) < date(?)" + not_invoice,  # nosec B608 -- само „?“ плейсхолдъри по брой
        [prev_start.isoformat(), prev_end.isoformat()] + invoice_params,
    ).fetchone()["c"]
    rows = con.execute(
        "SELECT data FROM documents"
        " WHERE date(created_at) >= date(?) AND date(created_at) < date(?)" + not_invoice,  # nosec B608 -- само „?“ плейсхолдъри по брой
        [cur_start.isoformat(), cur_end.isoformat()] + invoice_params,
    ).fetchall()
    client_counts = {}
    for row in rows:
        name = client_export.resolve_client_name(json.loads(row["data"]))
        if name:
            client_counts[name] = client_counts.get(name, 0) + 1
    top_clients = sorted(client_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        "month_count": month_count,
        "prev_month_count": prev_month_count,
        "top_clients": top_clients,
    }


@login_required
def dashboard():
    con = get_db()
    # „Последни документи“ и броячите по тип също пропускат фактурите —
    # те живеят само в раздел „Фактури“ (виж db.INVOICE_DOC_TYPES).
    non_invoice = db.non_invoice_doc_types()
    recent = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by"
        " WHERE d.doc_type IN (%s)"
        " ORDER BY d.id DESC LIMIT 10" % ",".join("?" for _ in non_invoice),  # nosec B608 -- само „?“ плейсхолдъри по брой
        list(non_invoice),
    ).fetchall()
    counts = {t: con.execute(
        "SELECT COUNT(*) AS c FROM documents WHERE doc_type = ? AND year = ?",
        (t, date.today().year),
    ).fetchone()["c"] for t in non_invoice}
    return render_template("dashboard.html", recent=recent, counts=counts,
                           doc_types={k: v for k, v in db.DOC_TYPES.items()
                                      if k in non_invoice},
                           update=updater.check_cached(),
                           stats=_dashboard_stats(con),
                           recent_docs_meta=[json.loads(r["data"]) for r in recent])


@login_required
def scan():
    """Зареждане на документ чрез сканиран баркод (или въведен номер)."""
    code = request.form.get("code", "").strip()
    con = get_db()
    doc = con.execute("SELECT id FROM documents WHERE barcode = ?", (code,)).fetchone()
    if doc is None:
        # опит и по номер, напр. "0001/2026"
        doc = con.execute(
            "SELECT id FROM documents WHERE number = ? ORDER BY id DESC", (code,)
        ).fetchone()
    if doc is None:
        flash("Няма документ с баркод „%s“." % code)
        return redirect(url_for("dashboard"))
    return redirect(url_for("view_document", doc_id=doc["id"]))


@login_required
def barcode_svg(code):
    return Response(code128_svg(code), mimetype="image/svg+xml")
