# -*- coding: utf-8 -*-
"""Адресна книга (клиенти) — списък, добавяне/редакция, изтриване.
Извлечено от app.py (Фаза 3) без промяна в поведението.

ЗАБЕЛЕЖКА (Фаза 4, все още НЕ приложено тук): находка M7 в
ПЛАН_ЗА_РАЗРАБОТКА.md отбелязва, че client_delete не е ограничен само до
администратори (за разлика от delete_document, който вече ползва
@admin_required) — всеки логнат служител може да изтрие клиент от
адресната книга. Тук поведението е запазено ТОЧНО както в оригинала
(Фаза 3 не променя авторизация); поправката е планирана за Фаза 4."""
import json

from flask import abort, flash, redirect, render_template, request, url_for

import db
from appcore import load_clients, login_required


def register(app):
    app.add_url_rule("/clients", "clients_list", clients_list)
    app.add_url_rule("/clients/new", "client_edit", client_edit, methods=["GET", "POST"])
    app.add_url_rule("/clients/<int:client_id>/edit", "client_edit", client_edit,
                     methods=["GET", "POST"])
    app.add_url_rule("/clients/<int:client_id>/delete", "client_delete",
                     client_delete, methods=["POST"])


@login_required
def clients_list():
    con = db.get_db()
    clients = load_clients(con)
    con.close()
    return render_template("clients.html", clients=clients)


@login_required
def client_edit(client_id=None):
    con = db.get_db()
    client = None
    if client_id is not None:
        client = con.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if client is None:
            con.close()
            abort(404)
    if request.method == "POST":
        fields = ("name", "address", "city", "postcode", "country", "eik",
                  "vat", "phone", "email", "contact")
        values = [request.form.get(f, "").strip() for f in fields]
        if not values[0]:
            flash("Името на фирмата е задължително.")
        else:
            if client is None:
                cur = con.execute(
                    "INSERT INTO clients (%s) VALUES (%s)"
                    % (", ".join(fields), ", ".join("?" * len(fields))),
                    values,
                )
                new_client_id = cur.lastrowid
            else:
                con.execute(
                    "UPDATE clients SET %s WHERE id = ?"
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
            con.close()
            flash("Клиентът е запазен в адресната книга.")
            return redirect(url_for("clients_list"))
    unload_points = db.get_unload_points(con, client_id) if client_id is not None else []
    con.close()
    return render_template("client_form.html", client=client,
                           unload_points=[dict(p) for p in unload_points])


@login_required
def client_delete(client_id):
    con = db.get_db()
    con.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    con.commit()
    con.close()
    flash("Клиентът е изтрит от адресната книга.")
    return redirect(url_for("clients_list"))
