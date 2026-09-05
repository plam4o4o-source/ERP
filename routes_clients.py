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

import client_export
import db
from appcore import admin_required, get_db, login_required, safe_json_data

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
    rows = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by"
        " WHERE ci_lower(d.client_name) = ci_lower(?) AND d.doc_type NOT IN (%s)"
        " ORDER BY d.id DESC LIMIT 200"
        % ",".join("?" for _ in db.INVOICE_DOC_TYPES),  # nosec B608 -- само „?“ плейсхолдъри по брой
        [client_name.strip()] + list(db.INVOICE_DOC_TYPES),
    ).fetchall()
    # Одит (16.08.2026, находка №20): сравнението по-долу беше буквално
    # (`==`, различаващо главни/малки букви) — документ, записан навремето
    # с малко различен регистър на същото име (напр. „АББ“ вместо „ABB“
    # при латиница/кирилица размяна, или обикновена печатна разлика в
    # регистъра при ръчно въвеждане на друг оператор), тихо отпадаше от
    # историята, макар да е СЪЩИЯТ клиент за практически цели. `.lower()`
    # тук е Python-ов (не SQLite LOWER()) — за разлика от нея, вградената
    # Python str.lower() коректно сгъва и кирилица, затова не е нужен
    # отделен ci_lower() (виж db._ci_lower — там причината е чисто SQLite-
    # специфична, LOWER() вътре в SQL заявка).
    needle = client_name.strip().lower()
    matched = []
    truncated = False
    for row in rows:
        data = safe_json_data(row["data"])
        name = client_export.resolve_client_name(data)
        if name and name.strip().lower() == needle:
            if len(matched) >= limit:
                truncated = True
                break
            matched.append(row)
    return matched, truncated


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
    rows = con.execute(
        "SELECT data FROM documents"
        " WHERE ci_lower(client_name) = ci_lower(?) AND doc_type NOT IN (%s)"
        " LIMIT 200" % ",".join("?" for _ in db.INVOICE_DOC_TYPES),  # nosec B608 -- само „?“ плейсхолдъри по брой
        [client_name.strip()] + list(db.INVOICE_DOC_TYPES),
    ).fetchall()
    needle = client_name.strip().lower()
    count = 0
    for row in rows:
        data = safe_json_data(row["data"])
        name = client_export.resolve_client_name(data)
        if name and name.strip().lower() == needle:
            count += 1
    return count, len(rows) >= 200


@login_required
def client_edit(client_id=None):
    con = get_db()
    client = None
    if client_id is not None:
        client = con.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if client is None:
            abort(404)
    if request.method == "POST":
        fields = ("name", "alias", "address", "city", "postcode", "country", "eik",
                  "vat", "phone", "email", "contact")
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
            old_name = client["name"] if client is not None else None
            new_name = values[0]
            if old_name and old_name.strip().lower() != new_name.strip().lower():
                affected, at_least = _count_client_documents(con, old_name)
                if affected:
                    flash(_("Преименувахте клиента от „%(old)s“ на „%(new)s“ — "
                            "%(count)s%(plus)s вече издадени документи ще продължат да "
                            "показват старото име „%(old)s“ (документите пазят името, "
                            "каквото е било при издаването им, не се променят "
                            "ретроактивно).") % {
                            "old": old_name, "new": new_name, "count": affected,
                            "plus": "+" if at_least else ""}, "warning")
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
            try:
                unload_points = json.loads(request.form.get("unload_points_json", "[]"))
            except ValueError:
                unload_points = []
            db.save_unload_points(con, new_client_id,
                                  unload_points if isinstance(unload_points, list) else [])
            con.commit()
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
        submitted = {f: request.form.get(f, "") for f in (
            "name", "alias", "address", "city", "postcode", "country", "eik",
            "vat", "phone", "email", "contact")}
        try:
            typed_points = json.loads(request.form.get("unload_points_json", "[]"))
        except ValueError:
            typed_points = []
        if isinstance(typed_points, list):
            unload_points = [p for p in typed_points if isinstance(p, dict)]
    recent_docs, recent_docs_truncated = ((), False)
    if client is not None:
        recent_docs, recent_docs_truncated = _client_recent_documents(con, client["name"])
    return render_template("client_form.html", client=client,
                           client_values=dict(client) if client is not None else {},
                           values=submitted,
                           unload_points=unload_points,
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
    row = con.execute("SELECT id FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        abort(404)
    con.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    con.commit()
    flash(_("Клиентът е изтрит от адресната книга."), "success")
    return redirect(url_for("clients_list"))
