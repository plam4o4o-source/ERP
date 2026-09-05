# -*- coding: utf-8 -*-
"""Табло, сканиране на баркод и генериране на баркод SVG. Извлечено от
app.py (Фаза 3) без промяна в поведението."""
import json
from datetime import date, timedelta

from flask import Response, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

import bg_keyboard
import client_export
import db
import updater
from appcore import get_db, login_required, safe_json_data
from barcode128 import code128_svg


def register(app):
    app.add_url_rule("/", "dashboard", dashboard)
    app.add_url_rule("/scan", "scan", scan, methods=["POST"])
    app.add_url_rule("/barcode/<code>.svg", "barcode_svg", barcode_svg)
    app.add_url_rule("/update/pending-restart", "update_pending_restart",
                     update_pending_restart)


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
    # Одит (16.08.2026, находка №22, дребна): преди тази поправка тук
    # стоеше `date(created_at) >= date(?) AND date(created_at) < date(?)`
    # — обвиването на САМАТА КОЛОНА в `date(...)` прави израза НЕ-sargable:
    # SQLite не може да ползва idx_documents_created_at (виж db._m008
    # по-долу), защото трябва да изчисли `date(created_at)` за ВСЕКИ ред,
    # преди изобщо да сравни — пълно сканиране на цялата таблица documents
    # при всяко зареждане на таблото, независимо от индекса. `_month_bounds`
    # по-горе вече връща ПОЛУОТВОРЕН интервал [start, next_start) от ЧИСТИ
    # ISO дати (без часова част) — лексикографското сравнение на текстовите
    # timestamp-и directно ("2026-08-17 09:15:00" >= "2026-08-01" и
    # < "2026-09-01") дава ТОЧНО СЪЩИЯ резултат, без да пипа функция върху
    # колоната — заявката вече МОЖЕ да ползва индекса.
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
        " WHERE created_at >= ? AND created_at < ?" + not_invoice,  # nosec B608 -- само „?“ плейсхолдъри по брой
        [cur_start.isoformat(), cur_end.isoformat()] + invoice_params,
    ).fetchone()["c"]
    prev_month_count = con.execute(
        "SELECT COUNT(*) AS c FROM documents"
        " WHERE created_at >= ? AND created_at < ?" + not_invoice,  # nosec B608 -- само „?“ плейсхолдъри по брой
        [prev_start.isoformat(), prev_end.isoformat()] + invoice_params,
    ).fetchone()["c"]
    # Одит (05.09.2026, находка №11): броенето е в SQL, по индексираната
    # колона (db._m011). Досега тук се четеше `data` на ВСЕКИ документ от
    # месеца и имената се брояха в Python — измерено при 20 000 документа:
    # 730 извиквания на `json.loads`, 20.8 MB прочетени, 176 ms общо за
    # таблото (при празна база 5 ms). Расте линейно с месечния оборот.
    top_clients = [(r["client_name"], r["c"]) for r in con.execute(
        "SELECT client_name, COUNT(*) AS c FROM documents"
        " WHERE created_at >= ? AND created_at < ? AND client_name <> ''"
        + not_invoice +  # nosec B608 -- само „?“ плейсхолдъри по брой
        " GROUP BY client_name ORDER BY c DESC, client_name ASC LIMIT 5",
        [cur_start.isoformat(), cur_end.isoformat()] + invoice_params,
    ).fetchall()]
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
                           recent_docs_meta=[safe_json_data(r["data"]) for r in recent])


@login_required
def update_pending_restart():
    """Одит (находка В6): полинг-крайна точка (виж initPendingRestartBanner
    в app.js) — оставена лека и на всеки логнат потребител (не само admin),
    защото автоматичният рестарт засяга ВСЕКИ, работещ в момента, а не
    само администраторите. Връща JSON вместо HTML, за да не пипа
    session/CSRF middleware-а на обикновените страници."""
    info = updater.get_pending_restart()
    return {"pending": info is not None,
           "seconds_left": info["seconds_left"] if info else None,
           "version": info["version"] if info else None}


def _find_document_by_code(con, code):
    """Търси документ по баркод или номер — извадено от scan() по-долу, за
    да може да се извиква ДВА пъти (буквално подадения код, после —
    евентуално — нормализирания му вариант, вижте scan())."""
    doc = con.execute("SELECT id FROM documents WHERE barcode = ?", (code,)).fetchone()
    if doc is None:
        # опит и по номер, напр. "0001/2026"
        doc = con.execute(
            "SELECT id FROM documents WHERE number = ? ORDER BY id DESC", (code,)
        ).fetchone()
    return doc


@login_required
def scan():
    """Зареждане на документ чрез сканиран баркод (или въведен номер)."""
    code = request.form.get("code", "").strip()
    con = get_db()
    doc = _find_document_by_code(con, code)
    if doc is None and any("Ѐ" <= ch <= "ӿ" for ch in code):
        # Одит (находка С4, среден риск): кодът съдържа кирилски букви —
        # най-вероятният случай е активна кирилска подредба на
        # клавиатурата по време на сканиране/ръчно въвеждане (виж
        # bg_keyboard.py за пълното обяснение). Пробваме ВТОРИ опит с
        # нормализирания (обратно преведен към латиница по БДС картата)
        # вариант — БЕЗОПАСНО по конструкция: ако нормализацията не е
        # точната за конкретната машина, резултатът просто НЕ намира
        # никакъв документ (същото поведение като преди поправката),
        # никога не пренасочва към ПОГРЕШЕН документ.
        normalized = bg_keyboard.normalize_bds_cyrillic(code)
        if normalized != code:
            doc = _find_document_by_code(con, normalized)
    if doc is None:
        flash(_("Няма документ с баркод „%s“.") % code, "error")
        return redirect(url_for("dashboard"))
    return redirect(url_for("view_document", doc_id=doc["id"]))


@login_required
def barcode_svg(code):
    return Response(code128_svg(code), mimetype="image/svg+xml")
