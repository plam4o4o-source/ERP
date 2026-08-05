# -*- coding: utf-8 -*-
"""Табло, сканиране на баркод и генериране на баркод SVG. Извлечено от
app.py (Фаза 3) без промяна в поведението."""
import json
from datetime import date

from flask import Response, flash, redirect, render_template, request, url_for

import db
import updater
from appcore import login_required
from barcode128 import code128_svg


def register(app):
    app.add_url_rule("/", "dashboard", dashboard)
    app.add_url_rule("/scan", "scan", scan, methods=["POST"])
    app.add_url_rule("/barcode/<code>.svg", "barcode_svg", barcode_svg)


@login_required
def dashboard():
    con = db.get_db()
    recent = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by"
        " ORDER BY d.id DESC LIMIT 10"
    ).fetchall()
    counts = {t: con.execute(
        "SELECT COUNT(*) AS c FROM documents WHERE doc_type = ? AND year = ?",
        (t, date.today().year),
    ).fetchone()["c"] for t in db.DOC_TYPES}
    con.close()
    return render_template("dashboard.html", recent=recent, counts=counts,
                           doc_types=db.DOC_TYPES,
                           update=updater.check_cached(),
                           recent_docs_meta=[json.loads(r["data"]) for r in recent])


@login_required
def scan():
    """Зареждане на документ чрез сканиран баркод (или въведен номер)."""
    code = request.form.get("code", "").strip()
    con = db.get_db()
    doc = con.execute("SELECT id FROM documents WHERE barcode = ?", (code,)).fetchone()
    if doc is None:
        # опит и по номер, напр. "0001/2026"
        doc = con.execute(
            "SELECT id FROM documents WHERE number = ? ORDER BY id DESC", (code,)
        ).fetchone()
    con.close()
    if doc is None:
        flash("Няма документ с баркод „%s“." % code)
        return redirect(url_for("dashboard"))
    return redirect(url_for("view_document", doc_id=doc["id"]))


@login_required
def barcode_svg(code):
    return Response(code128_svg(code), mimetype="image/svg+xml")
