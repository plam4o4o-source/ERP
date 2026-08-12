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
from appcore import admin_required, get_db, load_clients, login_required, safe_json_data


def register(app):
    app.add_url_rule("/clients", "clients_list", clients_list)
    app.add_url_rule("/clients/new", "client_edit", client_edit, methods=["GET", "POST"])
    app.add_url_rule("/clients/<int:client_id>/edit", "client_edit", client_edit,
                     methods=["GET", "POST"])
    app.add_url_rule("/clients/<int:client_id>/delete", "client_delete",
                     client_delete, methods=["POST"])


@login_required
def clients_list():
    con = get_db()
    clients = load_clients(con)
    return render_template("clients.html", clients=clients)


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
    like = "%" + client_name + "%"
    # Фактурите се изключват — те живеят само в раздел „Фактури“ и имат
    # собствена адресна книга (заявка: „и от таблото/историята на
    # клиента“). Виж db.INVOICE_DOC_TYPES.
    rows = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by"
        " WHERE d.data LIKE ? AND d.doc_type NOT IN (%s)"
        " ORDER BY d.id DESC LIMIT 200"
        % ",".join("?" for _ in db.INVOICE_DOC_TYPES),  # nosec B608 -- само „?“ плейсхолдъри по брой
        [like] + list(db.INVOICE_DOC_TYPES),
    ).fetchall()
    matched = []
    truncated = False
    for row in rows:
        data = safe_json_data(row["data"])
        if client_export.resolve_client_name(data) == client_name:
            if len(matched) >= limit:
                truncated = True
                break
            matched.append(row)
    return matched, truncated


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
    unload_points = db.get_unload_points(con, client_id) if client_id is not None else []
    recent_docs, recent_docs_truncated = ((), False)
    if client is not None:
        recent_docs, recent_docs_truncated = _client_recent_documents(con, client["name"])
    return render_template("client_form.html", client=client,
                           unload_points=[dict(p) for p in unload_points],
                           doc_types=db.DOC_TYPES,
                           recent_docs=recent_docs, recent_docs_truncated=recent_docs_truncated)


@admin_required
def client_delete(client_id):
    con = get_db()
    con.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    con.commit()
    flash(_("Клиентът е изтрит от адресната книга."), "success")
    return redirect(url_for("clients_list"))
