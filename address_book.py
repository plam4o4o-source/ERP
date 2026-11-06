# -*- coding: utf-8 -*-
"""Единен адресник — връзка между двете адресни книги.

Заявка (06.11.2026, BOOK): „Единен адресник“ от анализа на логистичните
програми. В програмата има ДВЕ книги и двете остават:

* `clients` (routes_clients) — един адрес на фирма, раздробен на град/
  пощенски код/държава; ползва се от ЧМР, опаковъчен лист, палетна карта,
  товарителница и декларациите;
* `invoice_clients` (invoice_clients_module) — два многоредови блока
  (Consignee + Bill To) за фактурите.

Тук ги СВЪРЗВАМЕ, без да сливаме или пренаписваме данни: записът за
фактури получава незадължителна колона `client_id` (db._m017_address_book).
Към един клиент може да има няколко записа за фактури (напр. различни
адреси за доставка); записът — към най-много един клиент. Изтрит клиент
означава просто „несвързан“ (без FOREIGN KEY — по-старите версии на
програмата върху същата база не я знаят).

Във формите за фактура, ако клиентът на пратката (от „Зареди от издадена
палетна карта“) е различен от свързания клиент на избрания запис за
фактури, се показва предупреждение (виж static/book.js и /address-book/check).
"""
from flask import abort, flash, redirect, request, url_for
from flask_babel import gettext as _

import applog
import db
import invoice_clients_module
from appcore import get_db, login_required

#: Колко предложения/съвпадения се показват при свързване.
LINK_SUGGEST_LIMIT = 10


def register(app):
    app.add_url_rule("/invoices/clients/<int:entry_id>/link", "book_link_entry",
                     book_link_entry, methods=["POST"])
    app.add_url_rule("/invoices/clients/<int:entry_id>/unlink", "book_unlink_entry",
                     book_unlink_entry, methods=["POST"])
    app.add_url_rule("/clients/<int:client_id>/invoice-entry", "book_create_entry",
                     book_create_entry, methods=["POST"])
    app.add_url_rule("/address-book/check", "book_check", book_check)
    app.add_template_global(entry_link_context, name="book_entry_links")
    app.add_template_global(client_link_context, name="book_client_links")
    app.add_template_global(client_names_for_entries, name="book_client_names")


# ---------------------------------------------------------------- данни

def _valid_id(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 2 ** 63 - 1 else None


def has_link_column(con):
    """База отпреди _m017 (напр. само за четене) — без връзки, без грешка."""
    return any(r[1] == "client_id" for r in con.execute("PRAGMA table_info(invoice_clients)"))


def get_client(con, client_id):
    client_id = _valid_id(client_id)
    if client_id is None:
        return None
    return con.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def linked_client(con, entry):
    """Свързаният клиент на записа за фактури (ред) или None."""
    if entry is None or "client_id" not in entry.keys() or not entry["client_id"]:
        return None
    return get_client(con, entry["client_id"])


def entries_for_client(con, client_id):
    if not has_link_column(con):
        return []
    return con.execute("SELECT * FROM invoice_clients WHERE client_id = ? ORDER BY name",
                       (client_id,)).fetchall()


def client_names_for_entries(entries):
    """{id на запис: име на свързания клиент} — за списъка с записи за фактури."""
    con = get_db()
    ids = sorted({e["client_id"] for e in entries or ()
                  if "client_id" in e.keys() and e["client_id"]})
    if not ids:
        return {}
    names = {r["id"]: r["name"] for r in con.execute(
        "SELECT id, name FROM clients WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}  # nosec B608 -- само „?“ плейсхолдъри по брой
    return {e["id"]: names[e["client_id"]] for e in entries
            if "client_id" in e.keys() and e["client_id"] in names}


def link(con, entry_id, client_id):
    """Свързва записа за фактури с клиента. Връща (entry, client) или None."""
    entry = invoice_clients_module.get(con, entry_id)
    client = get_client(con, client_id)
    if entry is None or client is None:
        return None
    con.execute("UPDATE invoice_clients SET client_id = ? WHERE id = ?", (client["id"], entry["id"]))
    con.commit()
    return entry, client


def unlink(con, entry_id):
    entry = invoice_clients_module.get(con, entry_id)
    if entry is None:
        return None
    con.execute("UPDATE invoice_clients SET client_id = NULL WHERE id = ?", (entry["id"],))
    con.commit()
    return entry


def _address_block(client, with_tax=False):
    """Многоредовият адрес на фактурата от раздробения адрес на клиента."""
    city = " ".join(x for x in ((client["postcode"] or "").strip(), (client["city"] or "").strip()) if x)
    lines = [(client["address"] or "").strip(), city, (client["country"] or "").strip()]
    if with_tax:
        if (client["vat"] or "").strip():
            lines.append("VAT No: " + client["vat"].strip())
        elif (client["eik"] or "").strip():
            lines.append("EIK: " + client["eik"].strip())
    return "\n".join(x for x in lines if x)


def entry_values_from_client(client):
    """Полетата на нов запис за фактури, попълнени от клиента."""
    name = (client["name"] or "").strip()
    return {
        "name": (client["alias"] or "").strip() or name,
        "delivery_name": name,
        "delivery_address": _address_block(client),
        "delivery_phone": (client["phone"] or "").strip(),
        "billing_name": name,
        "billing_address": _address_block(client, with_tax=True),
        "billing_phone": (client["phone"] or "").strip(),
        "notes": "",
    }


def create_entry_from_client(con, client):
    """Нов запис за фактури от клиента, веднага свързан с него. Връща id."""
    values = entry_values_from_client(client)
    fields = list(invoice_clients_module._FIELDS) + ["client_id"]
    values["client_id"] = client["id"]
    cur = con.execute(
        "INSERT INTO invoice_clients (%s) VALUES (%s)"  # nosec B608 -- имената са от константата _FIELDS
        % (", ".join(fields), ", ".join("?" for _ in fields)),
        [values[f] for f in fields])
    con.commit()
    return cur.lastrowid


def _key(text):
    return db.client_match_key(text or "")


def client_keys(client):
    """Ключовете за сравнение на клиента — името и псевдонимът."""
    return {k for k in (_key(client["name"]), _key(client["alias"])) if k}


def suggest_clients(con, entry, query=None, limit=LINK_SUGGEST_LIMIT):
    """Клиенти за свързване: по въведеното търсене или — без търсене — тези,
    чието име/псевдоним съвпада с името на записа или на някой от блоковете."""
    query = (query or "").strip()
    if query:
        fields = ("name", "alias", "city", "country", "eik", "vat")
        terms = query.split()[:6]
        where = " AND ".join("(%s)" % " OR ".join("ci_contains(%s, ?)" % f for f in fields)
                             for _t in terms)
        params = [t for t in terms for _f in fields]
        return con.execute("SELECT * FROM clients WHERE " + where +  # nosec B608 -- имената са от константата fields
                           " ORDER BY name COLLATE NOCASE LIMIT ?", params + [limit]).fetchall()
    keys = {_key(entry[f]) for f in ("name", "delivery_name", "billing_name")} - {""}
    if not keys:
        return []
    rows = con.execute("SELECT * FROM clients WHERE ci_client_key(name) IN (%s)"  # nosec B608 -- само „?“ плейсхолдъри
                       " OR ci_client_key(alias) IN (%s) ORDER BY name COLLATE NOCASE LIMIT ?"
                       % (",".join("?" * len(keys)), ",".join("?" * len(keys))),
                       list(keys) * 2 + [limit]).fetchall()
    return rows


def suggest_entries(con, client, query=None, limit=LINK_SUGGEST_LIMIT):
    """Несвързани записи за фактури за свързване с клиента (по търсене или по име).
    Записи, свързани с ДРУГ клиент, не се предлагат — първо се развързват
    от собствената си карта (иначе лесно се „открадва“ чужда връзка)."""
    query = (query or "").strip()
    fields = ("name", "delivery_name", "billing_name", "delivery_address", "billing_address")
    if query:
        terms = query.split()[:6]
        where = " AND ".join("(%s)" % " OR ".join("ci_contains(%s, ?)" % f for f in fields)
                             for _t in terms)
        params = [t for t in terms for _f in fields]
    else:
        keys = client_keys(client)
        if not keys:
            return []
        ph = ",".join("?" * len(keys))
        where = "(ci_client_key(name) IN (%s) OR ci_client_key(delivery_name) IN (%s)" \
                " OR ci_client_key(billing_name) IN (%s))" % (ph, ph, ph)
        params = list(keys) * 3
    return con.execute(
        "SELECT * FROM invoice_clients WHERE (client_id IS NULL"  # nosec B608 -- имената са от константата fields
        " OR client_id NOT IN (SELECT id FROM clients)) AND "
        + where + " ORDER BY name LIMIT ?", params + [limit]).fetchall()


def check_mismatch(con, entry_id, shipment_names):
    """Предупреждение във формата за фактура: пратката е на друг клиент.

    Връща речник: mismatch (bool), linked_name, shipment_names (различните).
    Без свързан клиент или без клиент на пратката няма предупреждение —
    тогава програмата не знае с какво да сравни."""
    entry = invoice_clients_module.get(con, _valid_id(entry_id)) if _valid_id(entry_id) else None
    names = []
    for name in shipment_names or ():
        name = (name or "").strip()
        if name and name not in names:
            names.append(name)
    result = {"mismatch": False, "linked_name": "", "shipment_names": []}
    if entry is None or not names or not has_link_column(con):
        return result
    client = linked_client(con, entry)
    if client is None:
        return result
    keys = client_keys(client)
    other = [n for n in names if _key(n) not in keys]
    result.update(mismatch=bool(other), linked_name=client["name"], shipment_names=other)
    return result


# ---------------------------------------------------------------- шаблони

def entry_link_context(entry):
    """Картата „Връзка с общата адресна книга“ в записа за фактури."""
    con = get_db()
    if entry is None or not has_link_column(con):
        return None
    query = request.args.get("link_q", "").strip()
    client = linked_client(con, entry)
    return {"client": client, "query": query,
            "suggestions": [] if client is not None else suggest_clients(con, entry, query)}


def client_link_context(client):
    """Картата „Записи за фактури“ в клиента от общата адресна книга."""
    con = get_db()
    if client is None or not has_link_column(con):
        return None
    query = request.args.get("book_q", "").strip()
    return {"entries": entries_for_client(con, client["id"]), "query": query,
            "suggestions": suggest_entries(con, client, query)}


# ---------------------------------------------------------------- маршрути

def _back(entry_id=None, client_id=None):
    if request.form.get("back") == "client" and client_id:
        return redirect(url_for("client_edit", client_id=client_id) + "#book-links")
    return redirect(url_for("invoice_client_edit", entry_id=entry_id) + "#book-links")


@login_required
def book_link_entry(entry_id):
    """Свързва запис за фактури с клиент (от картата на записа или на клиента)."""
    con = get_db()
    if invoice_clients_module.get(con, entry_id) is None:
        abort(404)
    client_id = _valid_id(request.form.get("client_id"))
    pair = link(con, entry_id, client_id) if client_id else None
    if pair is None:
        flash(_("Клиентът не е намерен в адресната книга."), "error")
        return _back(entry_id, client_id)
    entry, client = pair
    applog.log_audit("свързан запис за фактури с клиент",
                     "запис id=%s „%s“ → клиент id=%s „%s“"
                     % (entry["id"], entry["name"], client["id"], client["name"]))
    flash(_("Записът за фактури „%(entry)s“ е свързан с клиента „%(client)s“.")
          % {"entry": entry["name"], "client": client["name"]}, "success")
    return _back(entry_id, client["id"])


@login_required
def book_unlink_entry(entry_id):
    con = get_db()
    entry = invoice_clients_module.get(con, entry_id)
    if entry is None:
        abort(404)
    client_id = entry["client_id"] if "client_id" in entry.keys() else None
    unlink(con, entry_id)
    applog.log_audit("премахната връзка запис за фактури — клиент",
                     "запис id=%s „%s“ (клиент id=%s)" % (entry["id"], entry["name"], client_id))
    flash(_("Връзката с общата адресна книга е премахната. Данните в двата записа не са променени."),
          "success")
    return _back(entry_id, client_id)


@login_required
def book_create_entry(client_id):
    """„Създай запис за фактури от този клиент“ — копира адреса и го свързва."""
    con = get_db()
    client = get_client(con, client_id)
    if client is None:
        abort(404)
    if not has_link_column(con):
        abort(409)
    new_id = create_entry_from_client(con, client)
    applog.log_audit("създаден запис за фактури от клиент",
                     "клиент id=%s „%s“ → запис id=%s" % (client["id"], client["name"], new_id))
    flash(_("Създаден е запис за фактури от клиента „%s“ и е свързан с него. "
            "Проверете адресите за доставка и за фактуриране и запазете.") % client["name"], "success")
    return redirect(url_for("invoice_client_edit", entry_id=new_id))


@login_required
def book_check():
    """JSON за предупреждението във формата за фактура (static/book.js)."""
    con = get_db()
    result = check_mismatch(con, request.args.get("entry_id"), request.args.getlist("name")[:20])
    result["ok"] = True
    return result
