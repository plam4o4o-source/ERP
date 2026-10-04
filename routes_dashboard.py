# -*- coding: utf-8 -*-
"""Табло, сканиране на баркод и генериране на баркод SVG. Извлечено от
app.py (Фаза 3) без промяна в поведението."""
from datetime import date, timedelta

from flask import Response, abort, flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _

import backup
import bg_keyboard
import db
import updater
from appcore import get_db, login_required
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
    # Одит (26.09.2026, находка №33): „ACME Ltd“, „Acme Ltd“ и „acme ltd“
    # излизаха като трима клиенти — групира се без регистър (ci_lower
    # сгъва и кирилица), както в историята на клиента.
    top_clients = [(r["client_name"], r["c"]) for r in con.execute(
        "SELECT MIN(TRIM(client_name)) AS client_name, COUNT(*) AS c FROM documents"
        " WHERE created_at >= ? AND created_at < ? AND TRIM(client_name) <> ''"
        + not_invoice +  # nosec B608 -- само „?“ плейсхолдъри по брой
        " GROUP BY ci_lower(TRIM(client_name)) ORDER BY c DESC, client_name ASC LIMIT 5",
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
    # Одит (01.10.2026, F1a): NOT IN (фактурите), не IN (останалите типове) —
    # с IN планерът сортираше ~16 000 пълни реда (114 MB) за 10 на екрана;
    # NOT IN минава по id отзад напред и спира след 10-ия.
    recent = con.execute(
        "SELECT d.id, d.doc_type, d.number, d.barcode, d.client_name, d.created_at,"
        " u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by"
        " WHERE d.doc_type NOT IN (%s)"
        " ORDER BY d.id DESC LIMIT 10" % ",".join("?" for _ in db.INVOICE_DOC_TYPES),  # nosec B608 -- само „?“ плейсхолдъри по брой
        list(db.INVOICE_DOC_TYPES),
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
                           # Одит (01.10.2026, O4/O7): неуспешен архив и
                           # изоставащ часовник — иначе остават незабелязани.
                           backup_status=(backup.status(con)
                                          if session.get("role") == "admin" else None),
                           clock_skew=db.clock_skew_warning(con))


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


#: Одит (01.10.2026, U2): най-много толкова съвпадения в екрана за избор.
_SCAN_CHOICES_LIMIT = 50


def _number_candidates(code):
    """Въведеният номер + краткият му запис, допълнен до „0001/2026“ —
    същата толерантност като „добави от палетна карта“
    (routes_pallet_extra._find_pallet_by_code)."""
    candidates = [code]
    stripped = code.strip()
    if stripped.isdecimal():
        candidates.append("%04d/%d" % (int(stripped), date.today().year))
    elif "/" in stripped:
        left, _sep, right = stripped.partition("/")
        if left.strip().isdecimal() and right.strip().isdecimal():
            candidates.append("%04d/%s" % (int(left.strip()), right.strip()))
    return list(dict.fromkeys(c for c in candidates if c))


def _find_documents_by_code(con, code):
    """Документите с този баркод (уникален) или номер. Номерът „0001/2026“
    съществува във всеки тип документ, затова може да има няколко."""
    cols = "id, doc_type, number, barcode, client_name, created_at"
    doc = con.execute("SELECT %s FROM documents WHERE barcode = ?" % cols, (code,)).fetchone()  # nosec B608 -- колоните са литерал
    if doc is not None:
        return [doc]
    types = list(db.DOC_TYPES)
    for candidate in _number_candidates(code):
        # doc_type IN (...) позволява индекса (doc_type, year, number).
        rows = con.execute(
            "SELECT %s FROM documents WHERE doc_type IN (%s) AND number = ?"  # nosec B608 -- колоните са литерал; иначе само „?“ плейсхолдъри
            " ORDER BY id DESC LIMIT ?" % (cols, ",".join("?" for _ in types)),
            types + [candidate, _SCAN_CHOICES_LIMIT + 1]).fetchall()
        if rows:
            return rows
    return []


@login_required
def scan():
    """Зареждане на документ чрез сканиран баркод (или въведен номер)."""
    code = request.form.get("code", "").strip()
    con = get_db()
    docs = _find_documents_by_code(con, code)
    if not docs:
        # Одит (04.10.2026, F8): Caps Lock („cmr-04102026-0001“) и
        # ФОНЕТИЧНАТА подредба („ЦМР-…“) — виж bg_keyboard.code_variants.
        # Буквалният код вече е пробван по-горе; тук идват само резервните.
        for variant in bg_keyboard.code_variants(code)[1:]:
            docs = _find_documents_by_code(con, variant)
            if docs:
                break
    if not docs and any("Ѐ" <= ch <= "ӿ" for ch in code):
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
            docs = _find_documents_by_code(con, normalized)
    if not docs:
        flash(_("Няма документ с баркод „%s“.") % code, "error")
        return redirect(url_for("dashboard"))
    if len(docs) == 1:
        return redirect(url_for("view_document", doc_id=docs[0]["id"]))
    # Одит (01.10.2026, U2): няколко документа с този номер (различни типове)
    # — досега тихо се отваряше най-новият от който и да е тип.
    return render_template("scan_choose.html", code=code,
                           docs=docs[:_SCAN_CHOICES_LIMIT],
                           truncated=len(docs) > _SCAN_CHOICES_LIMIT,
                           doc_types=db.DOC_TYPES)


@login_required
def barcode_svg(code):
    # Одит (26.09.2026, находка №26): знаци извън Code128 (напр. кирилица)
    # хвърляха ValueError → 302 от общия обработчик вместо 404.
    try:
        svg = code128_svg(code)
    except ValueError:
        abort(404)
    return Response(svg, mimetype="image/svg+xml")
