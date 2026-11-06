# -*- coding: utf-8 -*-
"""Маршрути на група PARTIES (заявка 06.11.2026, „Направи всичко“ от анализа
на логистичните програми):

* /carriers…            — регистър на превозвачите (+ бърз запис от ЧМР,
                           търсене за формата, „последният превозвач за клиента“);
* /vies/check           — проверка на ДДС номер във VIES (само по бутон);
* /doc/<id>/transport-order[.pdf] — „Поръчка за превоз“ от ЧМР (печат + PDF);
* /carriers/settings    — условията и плащането в поръчката (администратор);
* /pallets…             — палетни сметки: салда, движения, ръчна корекция
                           (администратор), Excel.

Регистрира се от routes_clients.register — app.py, serve.py и тестовете не
се пипат (същият модел като shipment/routes_reports)."""
import io
import json
import re
from datetime import date

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_babel import gettext as _

import applog
import carriers
import db
import pallet_ledger
import pdf_export
import vies
from appcore import N_, admin_required, fetch_document, get_db, login_required, read_snapshot

#: Ключове в Настройки за поръчката за превоз.
TO_TERMS_KEY = "transport_order_terms"
TO_PAYMENT_KEY = "transport_order_payment_terms"

#: Подразбиращите се условия (двуезични, както бланката) — сменят се от
#: /carriers/settings; празно поле там = без условия на бланката.
TO_TERMS_DEFAULT = (
    "1. Превозът се извършва при условията на Конвенцията CMR. / The carriage is subject to the CMR Convention.\n"
    "2. Превозвачът потвърждава поръчката писмено преди натоварване; превозното средство е технически "
    "изправно, чисто и подходящо за товара. / The carrier confirms the order in writing before loading; "
    "the vehicle is roadworthy, clean and suitable for the goods.\n"
    "3. Претоварване, превоз с друг превозвач или промяна на маршрута — само с писмено съгласие. / "
    "No transhipment, subcontracting or route change without written consent.\n"
    "4. Размяната на палети се отбелязва в ЧМР. / Pallet exchange is recorded on the CMR.\n"
    "5. Фактурата се придружава от оригинален ЧМР с подпис и печат на получателя. / The invoice must be "
    "accompanied by the original CMR signed and stamped by the consignee.")
TO_PAYMENT_DEFAULT = ("30 дни след получаване на фактурата и оригиналния ЧМР / "
                      "30 days after receipt of the invoice and the original CMR")

#: Полетата на ЧМР за поръчката за превоз (по избор, в отделна секция на формата).
TO_DATA_KEYS = ("to_price_eur", "to_payment_terms", "to_date_delivery")

_PARTY_KIND_LABELS = {"client": N_("Клиент"), "carrier": N_("Превозвач")}


def register(app):
    app.add_url_rule("/carriers", "carriers_list", carriers_list)
    app.add_url_rule("/carriers/lookup", "carriers_lookup", carriers_lookup)
    app.add_url_rule("/carriers/last-for-client", "carriers_last_for_client",
                     carriers_last_for_client)
    app.add_url_rule("/carriers/quick-add", "carrier_quick_add", carrier_quick_add,
                     methods=["POST"])
    app.add_url_rule("/carriers/new", "carrier_edit", carrier_edit, methods=["GET", "POST"])
    app.add_url_rule("/carriers/<int:carrier_id>/edit", "carrier_edit", carrier_edit,
                     methods=["GET", "POST"])
    app.add_url_rule("/carriers/<int:carrier_id>/delete", "carrier_delete", carrier_delete,
                     methods=["POST"])
    app.add_url_rule("/carriers/settings", "transport_order_settings", transport_order_settings,
                     methods=["GET", "POST"])
    app.add_url_rule("/vies/check", "vies_check", vies_check, methods=["POST"])
    app.add_url_rule("/doc/<int:doc_id>/transport-order", "transport_order_view",
                     transport_order_view)
    app.add_url_rule("/doc/<int:doc_id>/transport-order.pdf", "transport_order_pdf",
                     transport_order_pdf)
    app.add_url_rule("/pallets", "pallet_accounts", pallet_accounts)
    app.add_url_rule("/pallets/party", "pallet_party", pallet_party)
    app.add_url_rule("/pallets/entry", "pallet_entry_add", pallet_entry_add, methods=["POST"])
    app.add_url_rule("/pallets/entry/<int:entry_id>/delete", "pallet_entry_delete",
                     pallet_entry_delete, methods=["POST"])
    app.add_url_rule("/pallets/export.xlsx", "pallet_export_xlsx", pallet_export_xlsx)
    app.add_template_global(parties_cmr_context, name="parties_cmr_context")
    app.add_template_global(_pallet_balance_for, name="pallet_balance_for")
    app.add_template_global(vies.cached_result, name="vies_cached")
    app.add_template_global(_pallet_types, name="pallet_types")


# ---------------------------------------------------------------- шаблонни помощници

def _pallet_types():
    return [(t, pallet_ledger.PALLET_TYPE_LABELS[t]) for t in pallet_ledger.PALLET_TYPES]


def parties_cmr_context():
    """Данните за блока „Превозвач“ на формата на ЧМР (templates/_parties.html):
    вградените превозвачи (до carriers.EMBED_LIMIT) като JSON + общия брой.
    При база отпреди миграцията (само за четене) — празен регистър."""
    con = get_db()
    try:
        rows = carriers.load_for_form(con)
        total = carriers.count_all(con)
    except Exception:
        rows, total = [], 0
    return {"carriers": rows, "carriers_json": carriers.as_json(rows), "total": total}


def _pallet_balance_for(kind, party_id, name):
    """Салдото за картата на клиента/превозвача (client_form/carrier_form)."""
    con = get_db()
    keys = (pallet_ledger.client_keys(party_id, name) if kind == "client"
            else pallet_ledger.carrier_keys(party_id, name))
    balance = pallet_ledger.party_balance(con, kind, keys)
    balance["key"] = keys[0] or keys[1]
    return balance


def _kind_label(kind):
    """Преведеният етикет на вида партньор (msgid-ите са в _PARTY_KIND_LABELS)."""
    label = _PARTY_KIND_LABELS.get(kind, kind)
    return _(label)


def _json_body():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form


# ---------------------------------------------------------------- регистър на превозвачите

@login_required
@read_snapshot
def carriers_list():
    con = get_db()
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int) or 1
    rows, page, total_pages, total = carriers.paginate(con, query, page)
    return render_template("carriers.html", carriers=rows, q=query, page=page,
                           total_pages=total_pages, total_count=total)


@login_required
def carriers_lookup():
    con = get_db()
    rows, truncated = carriers.search(con, request.args.get("q", ""))
    return {"ok": True, "carriers": rows, "truncated": truncated}


@login_required
def carriers_last_for_client():
    """„Последният превозвач за този клиент“ — за формата на ЧМР
    (static/parties.js). ?client_id=<id от адресната книга> или ?name=<име>."""
    con = get_db()
    key = request.args.get("client_id", "").strip() or request.args.get("name", "").strip()
    found = carriers.last_for_client(con, key) if key else None
    return {"ok": True, "found": found is not None, "last": found}


def _rows_from_json(raw, fields):
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        rows = []
    return carriers.clean_rows(rows, fields)


@login_required
def carrier_edit(carrier_id=None):
    con = get_db()
    carrier = None
    if carrier_id is not None:
        carrier = carriers.get(con, carrier_id)
        if carrier is None:
            abort(404)
    if request.method == "POST":
        values = carriers.clean_values(request.form)
        trucks = _rows_from_json(request.form.get("trucks_json"), carriers.VEHICLE_FIELDS)
        trailers = _rows_from_json(request.form.get("trailers_json"), carriers.VEHICLE_FIELDS)
        drivers = _rows_from_json(request.form.get("drivers_json"), carriers.DRIVER_FIELDS)
        typed = dict(values, trucks=trucks, trailers=trailers, drivers=drivers)
        if not values["name"]:
            flash(_("Името на превозвача е задължително."), "error")
            return _render_carrier_form(carrier, typed, request.form.get("version"))
        duplicate = con.execute(
            "SELECT name FROM carriers WHERE ci_lower(TRIM(name)) = ci_lower(?) AND id <> ? LIMIT 1",
            (values["name"], carrier_id or 0)).fetchone()
        con.execute("BEGIN IMMEDIATE")
        try:
            new_id = carriers.save(con, values, trucks, trailers, drivers, carrier_id=carrier_id,
                                   version=request.form.get("version") if carrier_id else None)
            con.commit()
        except carriers.EditConflict as exc:
            con.rollback()
            if exc.current is None:
                flash(_("Превозвачът е бил изтрит от друг потребител, докато го редактирахте. "
                        "Въведеното е показано — „Запази“ ще го запише като нов превозвач."),
                      "error")
                return _render_carrier_form(None, typed, None, action=url_for("carrier_edit"))
            flash(_("Превозвачът е бил променен от друг потребител, докато го редактирахте. "
                    "Вашите промени НЕ са записани — показани са във формата. Ако натиснете "
                    "„Запази“, вашата версия ще замени записаната."), "error")
            return _render_carrier_form(exc.current, typed, exc.current["version"])
        except Exception:
            con.rollback()
            raise
        applog.log_audit("редактиран превозвач" if carrier_id else "нов превозвач",
                         "id=%s „%s“" % (new_id, values["name"]))
        if duplicate is not None:
            flash(_("В регистъра вече има превозвач „%s“ — проверете дали не е същата фирма, "
                    "записана втори път.") % duplicate["name"], "warning")
        flash(_("Превозвачът е запазен в регистъра."), "success")
        return redirect(url_for("carriers_list", hl=new_id))
    return _render_carrier_form(carrier, None, carrier["version"] if carrier else None)


def _render_carrier_form(carrier, typed, version, action=None):
    values = typed if typed is not None else (carrier or {})
    balance = None
    if carrier is not None:
        balance = _pallet_balance_for("carrier", carrier["id"], carrier["name"])
    return render_template("carrier_form.html", carrier=carrier, v=values,
                           version=version, form_action=action, balance=balance)


@admin_required
def carrier_delete(carrier_id):
    con = get_db()
    row = con.execute("SELECT id, name FROM carriers WHERE id = ?", (carrier_id,)).fetchone()
    if row is None:
        abort(404)
    carriers.delete(con, carrier_id)
    con.commit()
    applog.log_audit("изтрит превозвач", "id=%s „%s“" % (carrier_id, row["name"]))
    flash(_("Превозвачът е изтрит от регистъра. Издадените ЧМР и палетната сметка не се "
            "променят."), "success")
    return redirect(url_for("carriers_list"))


@login_required
def carrier_quick_add():
    """Бърз запис на превозвач от формата на ЧМР (JSON) — фирма, ДДС, адрес,
    телефон и по един камион/ремарке/шофьор. Връща записа с децата му."""
    con = get_db()
    body = _json_body()
    values = carriers.clean_values(body)
    if not values["name"]:
        return jsonify(ok=False, error=_("Името на превозвача е задължително.")), 400
    existing = carriers.find_by_name(con, values["name"])
    if existing:
        return jsonify(ok=True, existing=True, carrier=carriers.get(con, existing),
                       message=_("Превозвачът вече е в регистъра — избран е записаният."))
    trucks = carriers.clean_rows([{"plate": body.get("truck_reg", "")}], carriers.VEHICLE_FIELDS)
    trailers = carriers.clean_rows([{"plate": body.get("trailer_reg", "")}], carriers.VEHICLE_FIELDS)
    drivers = carriers.clean_rows([{"name": body.get("driver", ""),
                                    "phone": body.get("driver_phone", "")}], carriers.DRIVER_FIELDS)
    con.execute("BEGIN IMMEDIATE")
    try:
        new_id = carriers.save(con, values, trucks, trailers, drivers)
        con.commit()
    except Exception:
        con.rollback()
        raise
    applog.log_audit("нов превозвач (от ЧМР)", "id=%s „%s“" % (new_id, values["name"]))
    return jsonify(ok=True, existing=False, carrier=carriers.get(con, new_id),
                   message=_("Превозвачът е добавен в регистъра."))


@admin_required
def transport_order_settings():
    con = get_db()
    if request.method == "POST":
        terms = carriers._clean_multiline(request.form.get(TO_TERMS_KEY), 6000)
        payment = carriers._clean(request.form.get(TO_PAYMENT_KEY), 500)
        db.save_settings(con, {TO_TERMS_KEY: terms, TO_PAYMENT_KEY: payment})
        con.commit()
        applog.log_audit("настройки на поръчката за превоз", "")
        flash(_("Условията на поръчката за превоз са запазени."), "success")
        return redirect(url_for("transport_order_settings"))
    s = db.get_settings(con)
    return render_template("transport_order_settings.html",
                           terms=s.get(TO_TERMS_KEY, TO_TERMS_DEFAULT),
                           payment=s.get(TO_PAYMENT_KEY, TO_PAYMENT_DEFAULT))


# ---------------------------------------------------------------- VIES

def _vies_error_text(exc):
    if exc.kind == "input":
        if exc.code == "EMPTY":
            return _("Въведете ДДС номер.")
        if exc.code == "NO_PREFIX":
            return _("Въведете ДДС номера с префикса на държавата (напр. BG123456789, DE123456789).")
        if exc.code == "COUNTRY":
            return _("„%s“ не е държава от ЕС във VIES (Гърция е EL). Проверката е само за ДДС номера от ЕС.") % exc.detail
        return _("ДДС номерът не е във вид, който VIES приема — проверете цифрите.")
    if exc.kind == "network":
        return _("Няма връзка с VIES (няма интернет или изтече времето за отговор, %(sec)d s). "
                 "Опитайте по-късно.") % {"sec": vies.VIES_TIMEOUT}
    if exc.kind == "unavailable":
        return _("Услугата VIES или регистърът на държавата не отговаря в момента (%s). "
                 "Опитайте по-късно.") % exc.code
    if exc.kind == "blocked":
        return _("VIES отказа заявката (%s) — опитайте по-късно.") % exc.code
    return _("VIES върна неочакван отговор (%s). Опитайте по-късно.") % (exc.code or "?")


@login_required
def vies_check():
    """Проверка на ДДС номер във VIES — само по изрично натиснат бутон
    (static/parties.js). Резултатът (валиден/невалиден, име, адрес, дата) се
    кешира на записа, ако е подаден (target + target_id). Одит: всяка проверка."""
    body = _json_body()
    vat = str(body.get("vat") or "")
    country = str(body.get("country") or "")
    target = str(body.get("target") or "")
    try:
        target_id = int(body.get("target_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    try:
        result = vies.check_vat(vat, country)
    except vies.ViesError as exc:
        applog.log_audit("проверка във VIES — неуспешна",
                         "%s %s (%s %s)" % (target or "-", target_id or "-", exc.kind, exc.code))
        return {"ok": False, "error": _vies_error_text(exc), "kind": exc.kind}
    saved = False
    if target in vies.CACHE_TABLES and 1 <= target_id <= 2 ** 63 - 1:
        con = get_db()
        saved = vies.save_result(con, target, target_id, result)
        con.commit()
    applog.log_audit("проверка във VIES",
                     "%s — %s; %s %s" % (result["vat"], "валиден" if result["valid"] else "невалиден",
                                         target or "-", target_id or "-"))
    return {"ok": True, "result": result, "saved": saved,
            "checked_at_display": _display_dt(result["checked_at"])}


def _display_dt(value):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", value or "")
    return "%s.%s.%s %s:%s" % (m.group(3), m.group(2), m.group(1), m.group(4), m.group(5)) if m else value


# ---------------------------------------------------------------- поръчка за превоз

def _transport_order_context(con, doc_id):
    row, data = fetch_document(con, doc_id)
    if row["doc_type"] != "cmr":
        abort(404)
    s = db.get_settings(con)
    record = None
    raw = str(data.get("carrier_id") or "").strip()
    if raw.isdigit():
        record = carriers.get(con, int(raw))
    driver = carriers.driver_details(record, data.get("driver"))
    terms = s.get(TO_TERMS_KEY, TO_TERMS_DEFAULT)
    payment = (data.get("to_payment_terms") or "").strip() or s.get(TO_PAYMENT_KEY, TO_PAYMENT_DEFAULT)
    pallets, _invalid = pallet_ledger.cmr_movements(data)
    return dict(doc=row, d=data, s=s, carrier=record, driver=driver, terms=terms,
                payment=payment, pallets=pallets,
                pallet_labels=pallet_ledger.PALLET_TYPE_LABELS)


@login_required
@read_snapshot
def transport_order_view(doc_id):
    con = get_db()
    return render_template("transport_order_print.html", **_transport_order_context(con, doc_id))


def _pdf_fields(ctx):
    """(етикет, стойност) за PDF износа — същите блокове като бланката."""
    d, s, rec, drv = ctx["d"], ctx["s"], ctx["carrier"], ctx["driver"]
    from appcore import format_bg_date, format_eur_amount
    orderer = "\n".join(x for x in (
        s.get("sender_name", ""), s.get("sender_address", ""),
        " ".join(x for x in (s.get("sender_postcode", ""), s.get("sender_city", "")) if x),
        s.get("sender_country", ""),
        ("VAT: " + s["sender_vat"]) if s.get("sender_vat") else (
            ("ЕИК: " + s["sender_eik"]) if s.get("sender_eik") else ""),
        " ".join(x for x in (s.get("sender_phone", ""), s.get("sender_email", "")) if x)) if x)
    if rec:
        carrier = "\n".join(x for x in (
            rec["name"], rec["address"], " ".join(x for x in (rec["postcode"], rec["city"]) if x),
            rec["country"], ("VAT: " + rec["vat"]) if rec["vat"] else "",
            ("EORI: " + rec["eori"]) if rec["eori"] else "",
            " ".join(x for x in (rec["phone"], rec["email"]) if x)) if x)
    else:
        carrier = d.get("carrier", "")
    driver = d.get("driver", "")
    if drv:
        extra = ", ".join(x for x in (drv.get("phone"), drv.get("id_doc")) if x)
        if extra:
            driver = "%s (%s)" % (driver, extra)
    pallets = "; ".join("%s: %d%s" % (ctx["pallet_labels"][t], g,
                                       " (размяна / exchange %d)" % x if x else "")
                        for t, g, _r, x in ctx["pallets"] if g or x)
    goods = "\n".join(x for x in (
        d.get("goods", ""),
        " ".join(x for x in (d.get("packages", ""), d.get("packing", "")) if x),
        ("Марки / Marks: " + d["marks"]) if d.get("marks") else "") if x)
    price = format_eur_amount(d.get("to_price_eur")) if (d.get("to_price_eur") or "").strip() else ""
    consignee = "\n".join(x for x in (d.get("consignee_name", ""), d.get("consignee_address", ""),
                                      " ".join(x for x in (d.get("consignee_city", ""),
                                                           d.get("consignee_country", "")) if x)) if x)
    return [
        ("Възложител / Ordering party", orderer),
        ("Превозвач / Carrier", carrier),
        ("ЧМР № / CMR No", ctx["doc"]["number"]),
        ("Влекач / Truck", d.get("truck_reg", "")),
        ("Ремарке / Trailer", d.get("trailer_reg", "")),
        ("Шофьор / Driver", driver),
        ("Товарен пункт / Loading place", d.get("place_loading", "")),
        ("Дата на товарене / Loading date", format_bg_date(d.get("date_loading", ""))),
        ("Разтоварен пункт / Delivery place", d.get("place_delivery", "")),
        ("Дата на доставка / Delivery date", format_bg_date(d.get("to_date_delivery", ""))),
        ("Получател / Consignee", consignee),
        ("Стока / Goods", goods),
        ("Палети / Pallets", pallets),
        ("Бруто, кг / Gross weight, kg", d.get("weight", "")),
        ("Обем, м³ / Volume, m³", d.get("volume", "")),
        ("Договорена цена / Agreed price (EUR)", price),
        ("Плащане / Payment terms", ctx["payment"]),
        ("Указания / Instructions", d.get("sender_instructions", "")),
        ("Условия / Conditions", ctx["terms"]),
        ("Възложител — подпис и печат / Ordering party — signature & stamp", "\n\n" + "_" * 28),
        ("Превозвач — приел поръчката / Carrier — order accepted", "\n\n" + "_" * 28),
    ]


@login_required
def transport_order_pdf(doc_id):
    con = get_db()
    ctx = _transport_order_context(con, doc_id)
    title = "Поръчка за превоз / Transport order"
    try:
        pdf_bytes = pdf_export.generate_document_pdf(title, ctx["doc"]["number"], "",
                                                     _pdf_fields(ctx), [], [])
    except pdf_export.PdfBusyError:
        flash(_("Точно сега се генерират други PDF файлове. Изчакайте няколко "
                "секунди и опитайте пак."), "warning")
        return redirect(url_for("transport_order_view", doc_id=doc_id))
    except RuntimeError:
        flash(_("PDF файлът не можа да се генерира за този документ. "
                "Опитайте пак — ако продължава, съобщете на администратор."), "error")
        return redirect(url_for("transport_order_view", doc_id=doc_id))
    applog.log_audit("поръчка за превоз (PDF)", "ЧМР id=%s №%s" % (doc_id, ctx["doc"]["number"]))
    safe_number = re.sub(r"[^0-9A-Za-z_-]+", "-", ctx["doc"]["number"]).strip("-") or str(doc_id)
    resp = send_file(io.BytesIO(pdf_bytes), as_attachment=True,
                     download_name="transport_order_%s.pdf" % safe_number,
                     mimetype="application/pdf")
    token = (request.args.get("dl") or "")[:32]
    if token and token.isalnum():
        from routes_documents import PDF_READY_COOKIE
        resp.set_cookie(PDF_READY_COOKIE, token, max_age=120, samesite="Lax",
                        secure=request.is_secure)
    return resp


# ---------------------------------------------------------------- палетни сметки

def _balance_filter():
    kind = request.args.get("kind", "")
    return {"kind": kind if kind in pallet_ledger.PARTY_KINDS else "",
            "q": request.args.get("q", "").strip(),
            "nonzero": request.args.get("nonzero") == "1"}


@login_required
@read_snapshot
def pallet_accounts():
    con = get_db()
    f = _balance_filter()
    page = request.args.get("page", 1, type=int) or 1
    rows, page, total_pages, total = pallet_ledger.balances(
        con, f["kind"], f["q"], f["nonzero"], page)
    return render_template("pallet_accounts.html", rows=rows, f=f, page=page,
                           total_pages=total_pages, total_count=total,
                           kind_labels=_PARTY_KIND_LABELS, today=date.today().isoformat())


def _party_args():
    kind = request.args.get("kind", "")
    key = request.args.get("key", "").strip()
    if kind not in pallet_ledger.PARTY_KINDS or not key or len(key) > 300:
        abort(404)
    return kind, key


@login_required
@read_snapshot
def pallet_party():
    con = get_db()
    kind, key = _party_args()
    info = pallet_ledger.party_display_name(con, kind, key)
    if info is None:
        abort(404)
    page = request.args.get("page", 1, type=int) or 1
    rows, page, total_pages, total = pallet_ledger.movements(con, kind, key, page)
    balance = pallet_ledger.party_balance(con, kind, [key])
    return render_template("pallet_party.html", kind=kind, key=key, info=info, rows=rows,
                           page=page, total_pages=total_pages, total_count=total,
                           balance=balance, kind_labels=_PARTY_KIND_LABELS,
                           today=date.today().isoformat())


@admin_required
def pallet_entry_add():
    """Ръчна корекция на палетна сметка (само администратор)."""
    con = get_db()
    kind = request.form.get("party_kind", "")
    key = request.form.get("party_key", "").strip()
    name = " ".join(request.form.get("party_name", "").split())
    back = request.form.get("back") or url_for("pallet_accounts")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("pallet_accounts")
    ptype = request.form.get("pallet_type", "EUR")
    qty = {q: pallet_ledger.parse_qty(request.form.get(q)) for q in pallet_ledger.QTY_FIELDS}
    entry_date = request.form.get("entry_date", "").strip() or date.today().isoformat()
    note = " ".join(request.form.get("note", "").split())[:300]
    errors = []
    if kind not in pallet_ledger.PARTY_KINDS:
        errors.append(_("Изберете вид партньор (клиент или превозвач)."))
    if ptype not in pallet_ledger.PALLET_TYPES:
        errors.append(_("Изберете вид палет."))
    if any(v is None for v in qty.values()):
        errors.append(_("Количествата са цели числа от 0 до %d.") % pallet_ledger.MAX_QTY)
    elif not any(qty.values()):
        errors.append(_("Въведете поне едно количество (дадени, върнати или разменени)."))
    if not re.match(r"\d{4}-\d{2}-\d{2}\Z", entry_date):
        errors.append(_("Невалидна дата."))
    client_id = carrier_id = None
    if not errors:
        if key:
            info = pallet_ledger.party_display_name(con, kind, key)
            if info is None:
                errors.append(_("Партньорът не е намерен."))
            else:
                name = info["display_name"]
                m = re.match(r"([cr])(\d+)\Z", key)
                if m and m.group(1) == "c":
                    client_id = int(m.group(2))
                elif m:
                    carrier_id = int(m.group(2))
        elif name:
            key, pid, name = pallet_ledger.resolve_manual_party(con, kind, name)
            client_id = pid if kind == "client" else None
            carrier_id = pid if kind == "carrier" else None
        else:
            errors.append(_("Въведете име на партньора."))
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(back)
    con.execute("BEGIN IMMEDIATE")
    try:
        # Свързаният запис може да е изтрит междувременно (FOREIGN KEY).
        if client_id and con.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone() is None:
            client_id = None
        if carrier_id and con.execute("SELECT 1 FROM carriers WHERE id = ?", (carrier_id,)).fetchone() is None:
            carrier_id = None
        entry_id = pallet_ledger.add_manual(con, kind, key, name, ptype, qty["given"],
                                            qty["returned"], qty["exchanged"], entry_date, note,
                                            session.get("user_id"), client_id, carrier_id)
        con.commit()
    except Exception:
        con.rollback()
        raise
    applog.log_audit("палетна сметка — ръчна корекция",
                     "id=%s %s „%s“ %s +%d/−%d/⇄%d" % (entry_id, kind, name, ptype, qty["given"],
                                                       qty["returned"], qty["exchanged"]))
    flash(_("Корекцията е записана в палетната сметка на „%s“.") % name, "success")
    return redirect(url_for("pallet_party", kind=kind, key=key))


@admin_required
def pallet_entry_delete(entry_id):
    con = get_db()
    row = con.execute("SELECT * FROM pallet_ledger WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        abort(404)
    if row["source"] != "manual":
        flash(_("Движение от ЧМР не се трие оттук — редактирайте палетите в самото ЧМР."), "error")
        return redirect(url_for("pallet_party", kind=row["party_kind"], key=row["party_key"]))
    con.execute("DELETE FROM pallet_ledger WHERE id = ?", (entry_id,))
    con.commit()
    applog.log_audit("палетна сметка — изтрита корекция",
                     "id=%s %s „%s“" % (entry_id, row["party_kind"], row["party_name"]))
    flash(_("Корекцията е изтрита."), "success")
    return redirect(url_for("pallet_party", kind=row["party_kind"], key=row["party_key"]))


@login_required
def pallet_export_xlsx():
    """Excel: лист „Салда“ (по филтъра на страницата) и лист „Движения“ (на
    партньора, ако е подаден ?key=, иначе всички — до EXPORT_LIMIT реда)."""
    from openpyxl import Workbook

    from routes_documents import (_list_row_styler, _list_sheet_finish, _list_sheet_header,
                                  _xlsx_append, _xlsx_styles)
    con = get_db()
    f = _balance_filter()
    party_key = request.args.get("key", "").strip()
    st = _xlsx_styles()
    wb = Workbook()
    ws = wb.active
    ws.title = _("Салда")[:31]
    heads = [_("Вид"), _("Партньор"), "EUR", "EPAL", _("Друг"), _("Общо"),
             _("Дадени"), _("Върнати"), _("Разменени"), _("Движения"), _("Последно")]
    last = _list_sheet_header(ws, heads, st)
    style = _list_row_styler(ws, st, len(heads))
    if party_key and f["kind"]:
        rows = [dict(pallet_ledger.party_balance(con, f["kind"], [party_key]),
                     party_kind=f["kind"],
                     display_name=(pallet_ledger.party_display_name(con, f["kind"], party_key)
                                   or {}).get("display_name", party_key))]
    else:
        rows, _p, _tp, _t = pallet_ledger.balances(con, f["kind"], f["q"], f["nonzero"],
                                                   limit=pallet_ledger.EXPORT_LIMIT)
    for r in rows:
        last = _xlsx_append(ws, [_kind_label(r["party_kind"]), r["display_name"],
                                 r["bal_eur"], r["bal_epal"], r["bal_other"], r["bal_total"],
                                 r["sum_given"], r["sum_returned"], r["sum_exchanged"],
                                 r["movements"], r["last_date"] or ""])
        style(last)
    _list_sheet_finish(ws, len(heads), [14, 40, 9, 9, 9, 9, 10, 10, 11, 10, 12], last)
    ws2 = wb.create_sheet(_("Движения")[:31])
    heads2 = [_("Дата"), _("Вид"), _("Партньор"), _("Вид палет"), _("Дадени"), _("Върнати"),
              _("Разменени"), _("Салдо"), _("ЧМР №"), _("Източник"), _("Бележка"), _("Въвел")]
    last2 = _list_sheet_header(ws2, heads2, st)
    style2 = _list_row_styler(ws2, st, len(heads2))
    if party_key and f["kind"]:
        moves = pallet_ledger.movements(con, f["kind"], party_key, limit=pallet_ledger.EXPORT_LIMIT)[0]
    else:
        moves = pallet_ledger.all_movements(con, f["kind"])
    for m in moves:
        last2 = _xlsx_append(ws2, [
            m["entry_date"], _kind_label(m["party_kind"]),
            m["party_name"], pallet_ledger.PALLET_TYPE_LABELS.get(m["pallet_type"], m["pallet_type"]),
            m["given"], m["returned"], m["exchanged"], m["given"] - m["returned"] - m["exchanged"],
            m["doc_number"], _("ЧМР") if m["source"] == "cmr" else _("Ръчна корекция"),
            m["note"], m["author"] or ""])
        style2(last2)
    _list_sheet_finish(ws2, len(heads2), [12, 12, 36, 12, 9, 9, 10, 9, 14, 16, 30, 18], last2)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    applog.log_audit("износ на палетните сметки в Excel", "%d салда, %d движения" % (len(rows), len(moves)))
    return send_file(buf, as_attachment=True,
                     download_name="pallet_accounts_%s.xlsx" % date.today().isoformat(),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
