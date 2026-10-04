# -*- coding: utf-8 -*-
"""Адресна книга (клиенти) — списък, добавяне/редакция, изтриване.
Извлечено от app.py (Фаза 3) без промяна в поведението.

Фаза 4 / находка M7: client_delete вече изисква администраторски права
(@admin_required), както delete_document — досега всеки логнат служител
можеше да изтрие клиент от адресната книга (случайно или злонамерено),
без възможност за връщане назад."""
import json

from flask import abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

import applog
import client_export
import db
from appcore import N_, admin_required, get_db, login_required, safe_json_data

#: Одит (19.08.2026, находка №25): адресната книга беше единственият голям
#: списък в програмата БЕЗ пагинация и БЕЗ сървърно търсене — измерено при
#: 5 000 клиента: 583 ms и 5 891 KB HTML за ЕДНО отваряне на /clients.
#: Стойността е същата като PAGE_SIZE за документите (routes_documents),
#: за да е еднакво усещането при преглед на дълъг списък.
PAGE_SIZE = 100

#: Колко записа връща най-много сървърното автодовършване в формите
#: (clients_lookup) — падащо меню с повече от толкова е неизползваемо,
#: а операторът просто дописва още знаци.
LOOKUP_LIMIT = 50


def register(app):
    app.add_url_rule("/clients", "clients_list", clients_list)
    app.add_url_rule("/clients/lookup", "clients_lookup", clients_lookup)
    app.add_url_rule("/clients/new", "client_edit", client_edit, methods=["GET", "POST"])
    app.add_url_rule("/clients/<int:client_id>/edit", "client_edit", client_edit,
                     methods=["GET", "POST"])
    app.add_url_rule("/clients/<int:client_id>/delete", "client_delete",
                     client_delete, methods=["POST"])


def _client_search_sql(query):
    """(WHERE клауза, параметри) за търсене в адресната книга.

    Одит (19.08.2026, находка №25): търсенето е СЪРВЪРНО (досега го нямаше
    изобщо — операторът търсеше с Ctrl+F в 5 MB страница). ci_contains е
    същата регистро-независима функция, ползвана от списъка с документи и
    от справочника материали (db._ci_contains) — SQLite-ското LIKE/LOWER
    сгъва само ASCII, тоест не би намерило „ООД“ при въведено „оод“."""
    query = (query or "").strip()
    if not query:
        return "", []
    fields = ("name", "alias", "city", "country", "eik", "vat", "email", "contact")
    where = " WHERE " + " OR ".join("ci_contains(%s, ?)" % f for f in fields)  # nosec B608 -- имената на колоните идват само от константата `fields`
    return where, [query] * len(fields)


def paginate_clients(con, query, page, page_size=PAGE_SIZE):
    """Пагиниран и филтриран изглед на адресната книга — огледално на
    appcore.paginate_documents (одит 19.08.2026, находка №25). Връща
    (clients, page, total_pages, total_count)."""
    where, params = _client_search_sql(query)
    total_count = con.execute(
        "SELECT COUNT(*) AS c FROM clients" + where, params).fetchone()["c"]  # nosec B608 -- where е съставен само от „?“ плейсхолдъри
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    rows = con.execute(
        "SELECT * FROM clients" + where +  # nosec B608 -- виж бележката по-горе
        " ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    return rows, page, total_pages, total_count


@login_required
def clients_list():
    con = get_db()
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1
    clients, page, total_pages, total_count = paginate_clients(con, query, page)
    return render_template("clients.html", clients=clients, q=query, page=page,
                           total_pages=total_pages, total_count=total_count)


@login_required
def clients_lookup():
    """Сървърно автодовършване на клиент за формите (одит 19.08.2026,
    находка №25).

    ЗАЩО съществува: формите вграждаха ЦЯЛАТА адресна книга в самия HTML —
    и като <option>-и, и втори път като JSON за автоматичното попълване на
    полетата. При 5 000 клиента това е над 2 MB на всяко отваряне на форма.
    Сега се вграждат само първите CLIENT_EMBED_LIMIT записа (при типична
    инсталация — тоест ВСИЧКИ, нищо не се променя), а останалите се
    намират оттук.

    ВАЖНО (автодовършването е ключова функция, не бива да се чупи):
    отговорът носи ПЪЛНИТЕ данни на всеки намерен клиент, включително
    пунктовете за разтоварване, точно както вграденият JSON — така
    попълването на полетата след избор работи еднакво, независимо дали
    клиентът е дошъл от вградения списък или от търсенето. Ако заявката се
    забави или се провали (бавна мрежа/тунел), формата продължава да
    работи с вече вградените клиенти — виж bindClientSelect в app.js."""
    con = get_db()
    query = request.args.get("q", "").strip()
    where, params = _client_search_sql(query)
    rows = con.execute(
        "SELECT * FROM clients" + where +  # nosec B608 -- where е съставен само от „?“ плейсхолдъри
        " ORDER BY name COLLATE NOCASE LIMIT ?",
        params + [LOOKUP_LIMIT + 1],
    ).fetchall()
    truncated = len(rows) > LOOKUP_LIMIT
    rows = rows[:LOOKUP_LIMIT]
    data = [dict(c) for c in rows]
    points_map = db.get_unload_points_map(con, [c["id"] for c in data]) if data else {}
    for c in data:
        c["unload_points"] = [
            {k: p.get(k, "") for k in ("label", "address", "city", "postcode", "country")}
            for p in points_map.get(c["id"], [])
        ]
    return {"ok": True, "clients": data, "truncated": truncated}


def _doc_client_name(data):
    """Името на клиента от данните на документ — client_export.
    resolve_client_name плюс `dest_name` (получателят в декларацията за
    двойна употреба). Одит (04.10.2026, F7): колоната documents.client_name
    вече го включва (db._m012) — без същото тук картата на клиента щеше да
    отсее декларациите при точната сверка по-долу."""
    return (client_export.resolve_client_name(data)
            or str(data.get("dest_name") or "").strip())


def _client_recent_documents(con, client_name, limit=10):
    """Последните документи на този клиент, за картата му в адресната
    книга — заявка: „история на документите от картата на клиента“.

    Няма отделна колона за име на клиент в таблица documents (свободните
    данни на всеки документ живеят в JSON колоната `data`), затова първо
    филтрираме грубо с LIKE (бърза SQL проверка, ограничена до 200 реда —
    достатъчно за практическа употреба, вижте бележката за „без тих таван“
    по-долу), после сверяваме ТОЧНОТО име през client_export.resolve_client_name
    — СЪЩАТА функция/приоритет (consignee_name/receiver_name/client_name),
    която ползват клиентските папки при износ и групирането по клиент в
    „Издадени документи“, за да сочи към ТОЧНО същите документи навсякъде."""
    if not client_name:
        return [], False
    # Одит (05.09.2026, находка №11): филтрира се по ИНДЕКСИРАНАТА колона
    # `client_name` (db._m011), не с `LIKE '%име%'` върху цялото JSON тяло.
    # Старият израз беше пълно сканиране: измерено 121–362 ms при топъл кеш
    # (зависеше от това колко рано SQLite среща 200-те реда), 2 576 ms при
    # студен, и 170 MB прочетени от файла — при база на мрежов диск това е
    # реален трафик при всяко отваряне на карта на клиент.
    #
    # Точната сверка по-долу ОСТАВА непроменена: колоната пази записаното
    # име както си е, а тук се сравнява без оглед на регистъра.
    ids = _client_document_ids(con, client_name)
    needle = client_name.strip().lower()
    matched = []
    # Одит (01.10.2026, F1c): редовете (с ~7 KB JSON всеки) се зареждат на
    # порции и само докато се съберат `limit` + 1 съвпадения, не всички 200.
    chunk = limit + 1
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        rows = con.execute(
            "SELECT d.*, u.full_name AS author FROM documents d"
            " LEFT JOIN users u ON u.id = d.created_by"
            " WHERE d.id IN (%s) ORDER BY d.id DESC" % ",".join("?" * len(part)),  # nosec B608 -- само „?“ плейсхолдъри по брой
            part).fetchall()
        # Одит (16.08.2026, находка №20): сравнението е без регистър (Python
        # str.lower() сгъва и кирилица) — същият клиент, записан с малко
        # различен регистър, не отпада от историята.
        for row in rows:
            data = safe_json_data(row["data"])
            name = _doc_client_name(data)
            if name and name.strip().lower() == needle:
                if len(matched) >= limit:
                    return matched, True
                matched.append(row)
    return matched, False


def _client_document_ids(con, client_name, cap=200):
    """id-тата (най-новите първи, до `cap`) на нефактурните документи с това
    име на клиент в колоната `client_name` (db._m011), без регистър.

    Одит (01.10.2026, F1c): `ORDER BY +id` кара SQLite да обходи покриващия
    индекс (client_name, doc_type) (search_index) и да сортира само id-тата,
    вместо да чете таблицата отзад напред — измерено 26 MB → 0 MB прочетени."""
    return [r[0] for r in con.execute(
        "SELECT id FROM documents"
        " WHERE ci_lower(client_name) = ci_lower(?) AND doc_type NOT IN (%s)"
        " ORDER BY +id DESC LIMIT ?" % ",".join("?" for _ in db.INVOICE_DOC_TYPES),  # nosec B608 -- само „?“ плейсхолдъри по брой
        [client_name.strip()] + list(db.INVOICE_DOC_TYPES) + [cap]).fetchall()]


def _count_client_documents(con, client_name):
    """Одит (16.08.2026, находка №20): брой документи, позоваващи се на
    ТОЧНО това име на клиент — за предупреждение при преименуване (виж
    client_edit по-долу). Собствена (не delegated) LIKE-заявка, СЪЩИЯТ
    таван от 200 сурови реда като _client_recent_documents (виж коментара
    там за пълния разказ) — при точно 200 сурови реда връща `at_least=True`
    (истинският брой МОЖЕ да е по-голям), вместо да сканира неограничено
    голяма база само за едно предупредително съобщение."""
    if not client_name:
        return 0, False
    # Одит (05.09.2026, находка №11): виж _client_recent_documents по-горе —
    # същата подмяна на пълното сканиране с индексираната колона.
    ids = _client_document_ids(con, client_name)
    rows = con.execute(
        "SELECT data FROM documents WHERE id IN (%s)" % ",".join("?" * len(ids)),  # nosec B608 -- само „?“ плейсхолдъри по брой
        ids).fetchall() if ids else []
    needle = client_name.strip().lower()
    count = 0
    for row in rows:
        data = safe_json_data(row["data"])
        name = _doc_client_name(data)
        if name and name.strip().lower() == needle:
            count += 1
    return count, len(rows) >= 200


#: Полетата на клиента в реда, в който ги подава формата (client_form.html).
CLIENT_FIELDS = ("name", "alias", "address", "city", "postcode", "country", "eik",
                 "vat", "phone", "email", "contact")

#: Одит (04.10.2026, R2): етикетите на полетата за съобщението при конфликт —
#: същите msgid-и като в client_form.html (вече преведени), маркирани с N_,
#: за да ги вижда `pybabel extract` и тук.
_FIELD_LABELS = {
    "name": N_("Фирма"), "alias": N_("Псевдоним"), "address": N_("Адрес (улица, №)"),
    "city": N_("Град"), "postcode": N_("Пощенски код"), "country": N_("Държава"),
    "eik": N_("ЕИК / Булстат"), "vat": N_("ДДС номер"), "phone": N_("Телефон"),
    "email": N_("Имейл"), "contact": N_("Лице за контакт"),
    "unload_points": N_("Пунктове за разтоварване"),
}

_POINT_KEYS = ("label", "address", "postcode", "city", "country")


def _norm_points(points):
    """Пунктовете за разтоварване в сравним вид — същото изчистване като
    db.save_unload_points (празните редове и не-речниците отпадат)."""
    out = []
    for p in points if isinstance(points, list) else []:
        if not isinstance(p, dict):
            continue
        row = tuple(db._unload_point_text(p.get(k)) for k in _POINT_KEYS)
        if any(row):
            out.append(row)
    return out


def _points_from_json(raw):
    try:
        points = json.loads(raw or "[]")
    except ValueError:
        return []
    return [p for p in points if isinstance(p, dict)] if isinstance(points, list) else []


def _points_as_dicts(rows):
    return [dict(zip(_POINT_KEYS, r)) for r in rows]


def _points_summary(rows):
    return "; ".join(", ".join(x for x in r if x) for r in rows) or "—"


def _short(value, limit=60):
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _merge_client_edit(form, current, current_points):
    """Одит (04.10.2026, R2): тристранно сливане на редакцията на клиент.

    Досега UPDATE-ът записваше ВСИЧКИ 11 полета от формата, каквито са били
    при отварянето ѝ: админ сменя телефона, служител в същото време сменя
    имейла — който запише втори, тихо връща другото поле към старото.
    Формата вече носи и оригиналите (`orig_<поле>`, `orig_unload_points_json`,
    същият модел като фирмените данни в routes_settings.settings_page):

    * поле, което операторът НЕ е пипал → остава записаното в базата (може
      да е чужда промяна);
    * пипнато поле, което никой друг не е сменил → записва се;
    * пипнато поле, сменено междувременно и от друг (на различна стойност) →
      КОНФЛИКТ: нищо не се записва, формата се връща с въведеното.

    Липсващ `orig_` (стара кеширана страница, скрипт) → полето се записва,
    както досега — същото решение като при фирмените данни.

    Връща (стойности за запис, точки за запис или None = без промяна,
    конфликти [(поле, записано, ваше)], стойности за показване при конфликт,
    точки за показване при конфликт)."""
    merged, display, conflicts = {}, {}, []
    for f in CLIENT_FIELDS:
        typed = (form.get(f) or "").strip()
        saved = (current.get(f) or "").strip()
        orig = form.get("orig_" + f)
        if orig is None:
            merged[f] = display[f] = typed
            continue
        orig = orig.strip()
        if typed == orig:
            merged[f] = display[f] = current.get(f) or ""
        elif saved in (orig, typed):
            merged[f] = display[f] = typed
        else:
            conflicts.append((f, saved, typed))
            merged[f] = display[f] = typed
    typed_points_raw = _points_from_json(form.get("unload_points_json"))
    typed_points = _norm_points(typed_points_raw)
    saved_points = _norm_points([dict(p) for p in current_points])
    orig_raw = form.get("orig_unload_points_json")
    points_to_save = typed_points_raw
    display_points = typed_points_raw
    if orig_raw is not None:
        orig_points = _norm_points(_points_from_json(orig_raw))
        if typed_points == orig_points:
            points_to_save = None  # операторът не ги е пипал — пазим записаните
            display_points = _points_as_dicts(saved_points)
        elif saved_points not in (orig_points, typed_points):
            conflicts.append(("unload_points", _points_summary(saved_points),
                              _points_summary(typed_points)))
    return merged, points_to_save, conflicts, display, display_points


def _flash_client_conflict(conflicts):
    lines = [_("%(field)s: записано „%(saved)s“, ваше „%(mine)s“")
             % {"field": _(_FIELD_LABELS[f]), "saved": _short(saved) or "—",
                "mine": _short(mine) or "—"} for f, saved, mine in conflicts]
    flash(_("Клиентът е бил променен от друг потребител, докато го редактирахте. "
            "Вашите промени НЕ са записани — показани са във формата, за да не се "
            "загубят. Ако натиснете „Запази клиента“, вашата версия ще замени "
            "записаната в посочените полета.") + " "
          + _("Записаната версия се различава от вашата в:") + " " + "; ".join(lines) + ".",
          "error")


def _orig_values(client_row, points):
    """Оригиналите за скритите `orig_` полета — от записа в базата."""
    values = {f: (client_row[f] or "") for f in CLIENT_FIELDS} if client_row is not None else {}
    points_json = json.dumps([dict(zip(_POINT_KEYS, r)) for r in _norm_points(points)],
                             ensure_ascii=False)
    return values, points_json


@login_required
def client_edit(client_id=None):
    con = get_db()
    client = None
    if client_id is not None:
        client = con.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if client is None:
            abort(404)
    # Одит (04.10.2026, R2): оригиналите за скритите полета. При отказ от
    # валидацията се връщат ПОДАДЕНИТЕ оригинали (иначе чужда промяна,
    # направена междувременно, би станала „база“ и би била презаписана);
    # при конфликт — текущите от базата (второ „Запази“ е съзнателен избор).
    orig_values, orig_points_json = _orig_values(
        client, [dict(p) for p in db.get_unload_points(con, client_id)] if client is not None else [])
    if request.method == "POST" and client is not None and "orig_name" in request.form:
        orig_values = {f: request.form.get("orig_" + f, "") for f in CLIENT_FIELDS}
        orig_points_json = request.form.get("orig_unload_points_json", orig_points_json)
    if request.method == "POST":
        fields = CLIENT_FIELDS
        values = [request.form.get(f, "").strip() for f in fields]
        if not values[0]:
            flash(_("Името на фирмата е задължително."), "error")
        else:
            # Одит (16.08.2026, находка №20): документите пазят името на
            # клиента като СВОБОДЕН ТЕКСТ в собствения си JSON (data), не
            # чрез връзка (FOREIGN KEY) към записа в адресната книга — виж
            # _client_recent_documents по-горе. Преименуване тук СЪЗНАТЕЛНО
            # НЕ променя ретроактивно вече издадените документи (те трябва
            # да пазят името, каквото е било в момента на издаване), но
            # операторът лесно може да не го знае и да очаква обратното —
            # предупреждаваме изрично КОЛКО документа остават с новото
            # старо име, преди да продължим.
            # Одит (04.10.2026, R2): проверката за чужда промяна и самият
            # запис са в една BEGIN IMMEDIATE транзакция — два почти
            # едновременни „Запази“ не могат да минат и двата проверката.
            warnings = []
            if client is not None:
                con.execute("BEGIN IMMEDIATE")
                current = con.execute("SELECT * FROM clients WHERE id = ?",
                                      (client_id,)).fetchone()
                if current is None:
                    con.rollback()
                    flash(_("Клиентът е бил изтрит от друг потребител, докато го "
                            "редактирахте. Въведеното е показано във формата — "
                            "„Запази клиента“ ще го запише като нов клиент."), "error")
                    return _render_client_form(
                        con, None, request.form,
                        _points_from_json(request.form.get("unload_points_json")),
                        {}, "[]", url_for("client_edit"))
                client = current
                current_points = db.get_unload_points(con, client_id)
                merged, points_to_save, conflicts, display, display_points = \
                    _merge_client_edit(request.form, dict(current), current_points)
                if conflicts:
                    con.rollback()
                    _flash_client_conflict(conflicts)
                    orig_values, orig_points_json = _orig_values(
                        current, [dict(p) for p in current_points])
                    return _render_client_form(con, current, display, display_points,
                                               orig_values, orig_points_json, None)
                values = [merged[f] for f in fields]
            else:
                points_to_save = _points_from_json(request.form.get("unload_points_json"))
            old_name = client["name"] if client is not None else None
            new_name = values[0]
            if old_name and old_name.strip().lower() != new_name.strip().lower():
                affected, at_least = _count_client_documents(con, old_name)
                if affected:
                    warnings.append(_("Преименувахте клиента от „%(old)s“ на „%(new)s“ — "
                            "%(count)s%(plus)s вече издадени документи ще продължат да "
                            "показват старото име „%(old)s“ (документите пазят името, "
                            "каквото е било при издаването им, не се променят "
                            "ретроактивно).") % {
                            "old": old_name, "new": new_name, "count": affected,
                            "plus": "+" if at_least else ""})
            # Одит (01.10.2026, U12): същото име (без регистър/интервали) вече
            # има — предупреждаваме, но не забраняваме (може да е друг клон).
            duplicate = con.execute(
                "SELECT name FROM clients WHERE ci_lower(TRIM(name)) = ci_lower(?) AND id <> ?"
                " LIMIT 1", (new_name.strip(), client_id or 0)).fetchone()
            if duplicate is not None:
                warnings.append(_("В адресната книга вече има клиент „%s“ — проверете дали "
                        "не е същата фирма, записана втори път.") % duplicate["name"])
            if client is None:
                # Имената на колоните идват само от хардкоднатия `fields`
                # тъпъл по-горе (никога от потребителски вход);
                # действителните СТОЙНОСТИ минават през bound params (?), не
                # през форматиране на низа — същият модел като db._ensure_column.
                cur = con.execute(
                    "INSERT INTO clients (%s) VALUES (%s)"  # nosec B608
                    % (", ".join(fields), ", ".join("?" * len(fields))),
                    values,
                )
                new_client_id = cur.lastrowid
            else:
                con.execute(
                    "UPDATE clients SET %s WHERE id = ?"  # nosec B608 -- виж бележката по-горе, същият модел
                    % ", ".join(f + " = ?" for f in fields),
                    values + [client_id],
                )
                new_client_id = client_id
            if points_to_save is not None:
                db.save_unload_points(con, new_client_id, points_to_save)
            con.commit()
            for text in warnings:
                flash(text, "warning")
            flash(_("Клиентът е запазен в адресната книга."), "success")
            return redirect(url_for("clients_list"))
    unload_points = [dict(p) for p in
                     (db.get_unload_points(con, client_id) if client_id is not None else [])]
    # Одит (01.09.2026, девети одит, находка №4): при отказ от валидацията
    # връщаме ВЪВЕДЕНОТО, не записаното в базата.
    #
    # Досега шаблонът рендираше единствено от `client`, а разтоварните пунктове
    # — от `db.get_unload_points`, тоест ОТ БАЗАТА, не от подаденото
    # `unload_points_json`. Пропуснато име на фирмата при СЪЗДАВАНЕ връщаше
    # абсолютно празна форма: 11 полета плюс всички добавени пунктове (по 5
    # полета всеки) изчезваха наведнъж. Тук отговорът е 200 (не пренасочване),
    # значи поправката не се нуждае от _store_preview — стойностите просто
    # пътуват обратно към шаблона.
    submitted = None
    if request.method == "POST":
        submitted = request.form
        unload_points = _points_from_json(request.form.get("unload_points_json"))
    return _render_client_form(con, client, submitted, unload_points,
                               orig_values, orig_points_json, None)


def _render_client_form(con, client, values, unload_points, orig_values,
                        orig_points_json, form_action):
    submitted = None
    if values is not None:
        submitted = {f: values.get(f, "") for f in CLIENT_FIELDS}
    recent_docs, recent_docs_truncated = ((), False)
    if client is not None:
        recent_docs, recent_docs_truncated = _client_recent_documents(con, client["name"])
    return render_template("client_form.html", client=client,
                           client_values=dict(client) if client is not None else {},
                           values=submitted,
                           unload_points=unload_points,
                           orig_values=orig_values,
                           orig_points_json=orig_points_json,
                           form_action=form_action,
                           doc_types=db.DOC_TYPES,
                           recent_docs=recent_docs, recent_docs_truncated=recent_docs_truncated)


@admin_required
def client_delete(client_id):
    con = get_db()
    # Одит (16.08.2026, находка №33): огледално на routes_documents.
    # delete_document — DELETE FROM ... WHERE id=? за НЕСЪЩЕСТВУВАЩ (вече
    # изтрит, напр. двоен клик/стар отворен таб) ID е no-op (0 засегнати
    # реда) без грешка; преди тази поправка операторът все пак виждаше
    # подвеждащото „Клиентът е изтрит“, сякаш реално е станало нещо.
    row = con.execute("SELECT id, name FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        abort(404)
    # Одит (04.10.2026, F10): клиент с издадени документи не се изтрива с
    # едно натискане — първо страница с БРОЯ им (както при преименуване, виж
    # client_edit) и изрично потвърждение (`confirm_documents=1`).
    # Документите не се променят, но операторът трябва да знае, че губи
    # картата на клиента с историята им и автоматичното попълване.
    if request.form.get("confirm_documents") != "1":
        affected, at_least = _count_client_documents(con, row["name"])
        if affected:
            return render_template("client_delete_confirm.html", client=row,
                                   count=affected, plus="+" if at_least else "")
    con.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    con.commit()
    # Одит (26.09.2026, находка №6): изтриването от адресната книга досега
    # не оставяше следа. Както при документите (routes_documents.
    # delete_document) — само идентификатор и име на обекта, не данните му.
    applog.log_audit("изтрит клиент", "id=%s „%s“" % (client_id, row["name"]))
    flash(_("Клиентът е изтрит от адресната книга."), "success")
    return redirect(url_for("clients_list"))
