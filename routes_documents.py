# -*- coding: utf-8 -*-
"""Списък/преглед/редакция/износ на документи, плюс издаването на петте
типа документи (ЧМР, опаковъчен лист, палетна карта, декларация за двойна
употреба, декларация за износ Италия).

Петте типа документи бяха по-рано пет почти идентични двойки *_new/*_preview
хендлъра в app.py. Тук са заменени с ДВЕ generic функции (_document_new,
_document_preview), управлявани от appcore.DOCUMENT_FLOWS (регистър с
разликите между типовете — виж appcore.py за подробности защо). Десетте
тънки wrapper-а по-долу (cmr_new, cmr_preview, packing_new, ...) пазят
ТОЧНО оригиналните endpoint имена и URL адреси, за да не се налага НИКАКВА
промяна в url_for(...) извикванията из 24-те Jinja шаблона."""
import io
import json

from flask import abort, flash, redirect, render_template, request, send_file, url_for
from flask_babel import gettext as _

import client_export
import db
import pdf_export
from appcore import (DOCUMENT_FLOWS, PRINT_TEMPLATES, _get_preview, admin_required,
                     clients_json, fetch_document, form_data, format_eur_amount, get_db,
                     load_clients, login_required, pallet_total_qty, parse_items,
                     render_preview, save_document)

FORM_TEMPLATES = {k: v["form_template"] for k, v in DOCUMENT_FLOWS.items()}


def register(app):
    app.add_url_rule("/docs", "documents", documents)
    app.add_url_rule("/doc/<int:doc_id>", "view_document", view_document)
    app.add_url_rule("/doc/<int:doc_id>/edit", "edit_document", edit_document, methods=["GET", "POST"])
    app.add_url_rule("/doc/<int:doc_id>/export.xlsx", "export_document_xlsx", export_document_xlsx)
    app.add_url_rule("/doc/<int:doc_id>/export.pdf", "export_document_pdf", export_document_pdf)
    app.add_url_rule("/doc/<int:doc_id>/delete", "delete_document", delete_document, methods=["POST"])

    app.add_url_rule("/cmr/new", "cmr_new", cmr_new, methods=["GET", "POST"])
    app.add_url_rule("/cmr/preview", "cmr_preview", cmr_preview, methods=["POST"])
    app.add_url_rule("/packing/new", "packing_new", packing_new, methods=["GET", "POST"])
    app.add_url_rule("/packing/preview", "packing_preview", packing_preview, methods=["POST"])
    app.add_url_rule("/pallet/new", "pallet_new", pallet_new, methods=["GET", "POST"])
    app.add_url_rule("/pallet/preview", "pallet_preview", pallet_preview, methods=["POST"])
    app.add_url_rule("/waybill/new", "waybill_new", waybill_new, methods=["GET", "POST"])
    app.add_url_rule("/waybill/preview", "waybill_preview", waybill_preview, methods=["POST"])
    app.add_url_rule("/dualuse/new", "dualuse_new", dualuse_new, methods=["GET", "POST"])
    app.add_url_rule("/dualuse/preview", "dualuse_preview", dualuse_preview, methods=["POST"])
    app.add_url_rule("/export-it/new", "export_it_new", export_it_new, methods=["GET", "POST"])
    app.add_url_rule("/export-it/preview", "export_it_preview", export_it_preview, methods=["POST"])


# ---------------------------------------------------------------- списък/преглед/редакция

@login_required
def documents():
    doc_type = request.args.get("type", "")
    query = request.args.get("q", "").strip()
    group_by_client = request.args.get("group") == "client"
    # Филтър по диапазон от дати (заявка: подобрения по списъка с
    # документи) — по d.created_at (винаги попълнена автоматично при
    # издаване, виж db.py SCHEMA), не по doc_date от свободните данни на
    # документа (то е свободен текст, попълван ръчно, невинаги налично за
    # всички типове документи). date_from/date_to идват от <input
    # type="date"> (YYYY-MM-DD) — сравняваме само календарната дата през
    # SQLite date(), за да покрие целия ден на date_to включително.
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    sql = ("SELECT d.*, u.full_name AS author FROM documents d"
           " LEFT JOIN users u ON u.id = d.created_by WHERE 1=1")
    params = []
    if doc_type in db.DOC_TYPES:
        sql += " AND d.doc_type = ?"
        params.append(doc_type)
    if query:
        sql += " AND (d.number LIKE ? OR d.barcode LIKE ? OR d.data LIKE ?)"
        like = "%" + query + "%"
        params += [like, like, like]
    if date_from:
        sql += " AND date(d.created_at) >= date(?)"
        params.append(date_from)
    if date_to:
        sql += " AND date(d.created_at) <= date(?)"
        params.append(date_to)
    sql += " ORDER BY d.id DESC LIMIT 300"
    con = get_db()
    docs = con.execute(sql, params).fetchall()
    metas = [json.loads(d["data"]) for d in docs]
    if group_by_client:
        # Групиране по клиент (заявка: „всеки клиент да се запазват в
        # отделни папки във всички документи“ — тук е UI-групирането,
        # виж client_export.py за реалните папки на диска). Сортира се по
        # СЪЩОТО име, което се показва в таблицата и се ползва за
        # клиентската папка (client_export.resolve_client_name), но с
        # обърнат приоритет на dict.get, за да съвпадне 1:1 с колоната
        # „Получател/Клиент“ (m.consignee_name or m.receiver_name or
        # m.client_name) в templates/documents.html.
        def client_key(m):
            name = (m.get("consignee_name") or m.get("receiver_name")
                   or m.get("client_name") or "").strip()
            return (name == "", name.lower(), name)
        paired = sorted(zip(docs, metas), key=lambda p: client_key(p[1]))
        docs = [p[0] for p in paired]
        metas = [p[1] for p in paired]
    return render_template("documents.html", docs=docs, metas=metas,
                           doc_types=db.DOC_TYPES, sel_type=doc_type, q=query,
                           group_by_client=group_by_client,
                           date_from=date_from, date_to=date_to)


@login_required
def view_document(doc_id):
    con = get_db()
    row, data = fetch_document(con, doc_id)
    copies = request.args.get("copies", type=int) or 1
    label_format = request.args.get("format") == "label"
    return render_template(PRINT_TEMPLATES[row["doc_type"]], doc=row, d=data,
                           copies=min(copies, 5), preview=False,
                           label_format=label_format)


@login_required
def edit_document(doc_id):
    """Редакция на вече издаден документ — номерът, баркодът, годината и
    поредността се пазят непроменени (не се преиздава нов номер); само
    съдържанието (data) се обновява. Ползва СЪЩИТЕ форми, както при
    издаване, предварително попълнени с текущите стойности."""
    con = get_db()
    row, data = fetch_document(con, doc_id)
    doc_type = row["doc_type"]
    if doc_type not in FORM_TEMPLATES:
        abort(404)

    if request.method == "POST":
        new_data = form_data()
        if doc_type in ("packing", "pallet", "waybill", "dualuse", "export_it"):
            new_data["items"] = parse_items()
            if "items_format" in data:
                new_data["items_format"] = data["items_format"]
        # номерът/баркодът се пазят от оригинала — редакцията не преиздава нов номер
        new_data["number"] = row["number"]
        new_data["barcode"] = row["barcode"]
        con.execute("UPDATE documents SET data = ? WHERE id = ?",
                    (json.dumps(new_data, ensure_ascii=False), doc_id))
        con.commit()
        flash(_("Документ № %s е обновен.") % row["number"])
        return redirect(url_for("view_document", doc_id=doc_id))

    clients = load_clients(con)
    settings = db.get_settings(con)
    ctx = {
        "clients": clients,
        "clients_json": clients_json(clients, con) if doc_type == "cmr" else clients_json(clients),
        "s": settings,
        "edit_doc": row,
        "edit_data": data,
    }
    if doc_type in ("packing", "pallet", "waybill", "dualuse", "export_it"):
        ctx["items"] = data.get("items", [])
    return render_template(FORM_TEMPLATES[doc_type], **ctx)


# ---------------------------------------------------------------- износ в Excel (.xlsx)
# Данните на всеки документ (номер, страни, стоки и т.н.) + редовете
# артикули (ако има) — в удобен за отваряне в Excel файл. PDF не се
# генерира отделно — печатните шаблони вече поддържат "Save as PDF" през
# диалога за печат на браузъра (вграден във Windows/Chromium, работи offline,
# без нужда от допълнителни компоненти в самата програма).

_XLSX_FIELDS = {
    "cmr": [
        ("Дата на съставяне", "established_date"), ("Място на съставяне", "established_place"),
        ("Изпращач", "sender_name"), ("Адрес изпращач", "sender_address"),
        ("Град изпращач", "sender_city"), ("Държава изпращач", "sender_country"),
        ("Получател", "consignee_name"), ("Адрес получател", "consignee_address"),
        ("Град получател", "consignee_city"), ("Държава получател", "consignee_country"),
        ("Разтоварен пункт", "place_delivery"), ("Товарен пункт", "place_loading"),
        ("Дата на натоварване", "date_loading"), ("Приложени документи", "attached_docs"),
        ("Марки и номера", "marks"), ("Брой колети", "packages"), ("Вид на опаковката", "packing"),
        ("Вид на стоката", "goods"), ("Статистически №", "stat_no"),
        ("Бруто тегло, кг", "weight"), ("Обем, м³", "volume"),
        ("Указания на изпращача", "sender_instructions"), ("Плащане на превоза", "payment_instructions"),
        ("Наложен платеж", "cod"), ("Специални споразумения", "special_agreements"),
        ("Превозвач", "carrier"), ("Последващи превозвачи", "successive_carriers"),
        ("Рег. № влекач", "truck_reg"), ("Рег. № ремарке", "trailer_reg"), ("Шофьор", "driver"),
        ("Резерви на превозвача", "reservations"),
    ],
    "packing": [
        ("Дата", "doc_date"), ("Изпращач", "sender_name"), ("Адрес изпращач", "sender_address"),
        ("Получател", "receiver_name"), ("Адрес получател", "receiver_address"),
        ("Град получател", "receiver_city"), ("Държава получател", "receiver_country"),
        ("Фактура №", "invoice_no"), ("Поръчка №", "order_no"),
        ("Условия на доставка", "terms_delivery"), ("Вид транспорт", "transport_type"),
        ("HS Code", "hs_code"),
        ("Общо колети", "total_packages"), ("Общо обем, м³", "total_volume"),
        ("Общо нето, кг", "total_net"), ("Общо бруто, кг", "total_gross"),
        ("Забележки", "notes"),
    ],
    "pallet": [
        ("Дата", "doc_date"), ("Палет №", "pallet_no"), ("Тип палет", "pallet_type"),
        ("Изпращач", "sender_name"), ("Клиент", "client_name"), ("Адрес клиент", "client_address"),
        ("Град клиент", "client_city"), ("Държава клиент", "client_country"),
        ("Вид опаковка", "packaging_type"), ("Общ брой", "__total_qty__"),
        ("Бруто, кг", "gross"), ("Височина, см", "height"),
        ("Свързано ЧМР №", "ref_cmr"), ("Забележки", "notes"),
    ],
    "waybill": [
        ("Издадена в", "established_place"), ("Издадена на", "established_date"),
        ("Изпращач", "sender_name"), ("Адрес изпращач", "sender_address"),
        ("Превозвач", "carrier_name"), ("Адрес превозвач", "carrier_address"),
        ("Получател", "consignee_name"), ("Адрес получател", "consignee_address"),
        ("Град получател", "consignee_city"), ("Държава получател", "consignee_country"),
        ("Място на натоварване", "place_loading"), ("Дата на натоварване", "date_loading"),
        ("Място на разтоварване", "place_delivery"), ("Дата на разтоварване", "date_delivery"),
        ("Пробег, км", "mileage"),
        ("Опасен товар — клас", "dangerous_class"), ("Опасен товар — наименование", "dangerous_name"),
        ("Придружител на товара", "escort_name"), ("Брой придружители", "escort_count"),
        ("Превозна цена, EUR", "transport_price"), ("Допълнителни разходи, EUR", "extra_costs"),
        ("Марка на автомобила", "vehicle_make"), ("Модел на автомобила", "vehicle_model"),
        ("Рег. № на автомобила", "vehicle_reg"), ("Пътен лист №", "route_sheet_no"),
        ("Инструкции на превозвача", "carrier_instructions"),
        ("Натоварване — дата", "loading_date"), ("Натоварване — от час", "loading_from"),
        ("Натоварване — до час", "loading_to"),
        ("Разтоварване — дата", "unloading_date"), ("Разтоварване — от час", "unloading_from"),
        ("Разтоварване — до час", "unloading_to"),
        ("Забележка", "notes"),
    ],
    "dualuse": [
        ("Дата", "doc_date"), ("Износител", "sender_name"), ("ЕИК/ЕГН", "sender_eik"),
        ("Фактура/и №", "invoice_numbers"), ("Дата на фактурата", "invoice_date"),
        ("Държава на износ", "destination_country"), ("Място на съставяне", "place"),
        ("Декларатор", "declarant_name"), ("Длъжност", "declarant_position"),
    ],
    "export_it": [
        ("Дата", "doc_date"), ("Декларатор", "declarant_name"),
        ("Пълномощник на", "represented_company"), ("Фактура №", "invoice_no"),
        ("Износител", "exporter_company"), ("Получател", "receiver_name"),
        ("Ref. ЧМР №", "ref_cmr"), ("Място на съставяне", "place"),
    ],
}

_XLSX_ITEM_COLUMNS = {
    "packing": [("description", "Описание"), ("qty", "Количество"), ("packing", "Опаковка"),
               ("length", "Дължина, мм"), ("width", "Широчина, мм"), ("height", "Височина, мм"),
               ("volume", "Обем, м³"), ("net", "Нето, кг"), ("gross", "Бруто, кг")],
    "pallet_generic": [("code", "Артикул/код"), ("description", "Описание"),
                       ("qty", "Количество"), ("weight", "Тегло, кг")],
    "pallet_orders": [("due_date", "Дата на падеж"), ("order_no", "Поръчка №"), ("pos", "Позиция"),
                      ("project", "Проект"), ("reference", "Референция"),
                      ("reference_desc", "Описание"), ("qty", "Количество"),
                      ("unit", "Мярка"), ("stock", "Склад")],
    "waybill": [("description", "Наименование"), ("packing", "Опаковка"), ("marks", "Маркировка/номера"),
               ("weight", "Тегло, кг"), ("qty", "Брой")],
}


#: Полета, показвани с "€" суфикс в износите (Excel/PDF) — заявка: "да
#: остане валута само евро". Само товарителницата за вътрешен превоз има
#: парични полета в момента (transport_price/extra_costs) — вижте
#: appcore.format_eur_amount за самото форматиране (споделено и с
#: waybill_print.html чрез Jinja global format_eur).
_MONEY_FIELDS = {"waybill": {"transport_price", "extra_costs"}}


def _export_fields_and_items(doc_type, data):
    """Общата логика за "какво да покаже износът" (полета + редове+колони),
    споделена от Excel (export_document_xlsx) и PDF (export_document_pdf)
    износа — вижте pdf_export.py защо PDF-ът нарочно преизползва точно тези
    речници вместо отделен pixel-perfect PDF шаблон."""
    money_keys = _MONEY_FIELDS.get(doc_type, ())
    fields = []
    for label, key in _XLSX_FIELDS.get(doc_type, []):
        # "__total_qty__" е специален случай (само за pallet) — „Общ брой“
        # НЕ се пази като суров запис в data, изчислява се на момента от
        # items (виж appcore.pallet_total_qty), точно както във формата и
        # печатните шаблони.
        value = pallet_total_qty(data.get("items")) if key == "__total_qty__" else data.get(key, "")
        if key in money_keys and value:
            value = format_eur_amount(value)
        fields.append((label, value))

    items = data.get("items") or []
    cols = []
    if items:
        if doc_type == "pallet":
            cols = _XLSX_ITEM_COLUMNS["pallet_orders" if data.get("items_format") == "orders"
                                      else "pallet_generic"]
        else:
            cols = _XLSX_ITEM_COLUMNS.get(doc_type, [])
    return fields, items, cols


@login_required
def export_document_xlsx(doc_id):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    con = get_db()
    row, data = fetch_document(con, doc_id)
    doc_type = row["doc_type"]
    title = db.DOC_TYPES.get(doc_type, {}).get("title", doc_type)
    fields, items, cols = _export_fields_and_items(doc_type, data)

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31] or "Документ"

    bold = Font(bold=True)
    ws.append(["%s № %s" % (title, row["number"])])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append(["Баркод", row["barcode"]])
    ws.cell(row=2, column=1).font = bold
    ws.append([])

    for label, value in fields:
        ws.append([label, value])
        ws.cell(row=ws.max_row, column=1).font = bold

    if items and cols:
        ws.append([])
        header_row = ws.max_row + 1
        ws.append([label for _key, label in cols])
        for c in range(1, len(cols) + 1):
            ws.cell(row=header_row, column=c).font = bold
        for it in items:
            ws.append([it.get(key, "") for key, _label in cols])

    for col_cells in ws.columns:
        lengths = [len(str(c.value)) for c in col_cells if c.value is not None]
        width = max(lengths) + 2 if lengths else 10
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(width, 10), 50)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = "%s_%s.xlsx" % (doc_type, row["number"].replace("/", "-"))

    # Клиентски папки (виж client_export.py) — best-effort копие в
    # <базова папка>/<клиент>/, ако е включено в системните настройки.
    # НЕ бива да провали свалянето на файла за потребителя при грешка.
    client_export.save_client_export_copy(db.get_settings(con), doc_type, data,
                                          filename, buf.getvalue())

    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet")


@login_required
def export_document_pdf(doc_id):
    """Износ на документ в PDF (бутон „Изтегли PDF“) — вижте pdf_export.py
    за пълния коментар защо е ЕДИН споделен генеричен PDF шаблон, а не 6
    pixel-perfect копия на печатните шаблони."""
    con = get_db()
    row, data = fetch_document(con, doc_id)
    doc_type = row["doc_type"]
    title = db.DOC_TYPES.get(doc_type, {}).get("title", doc_type)
    fields, items, cols = _export_fields_and_items(doc_type, data)

    pdf_bytes = pdf_export.generate_document_pdf(
        title, row["number"], row["barcode"], fields, items, cols)
    filename = "%s_%s.pdf" % (doc_type, row["number"].replace("/", "-"))

    # Клиентски папки (виж client_export.py) — best-effort копие, СЪЩИЯТ
    # механизъм като при Excel износа по-горе (заявка: "И двете" — важи за
    # ВСИЧКИ износи, не само Excel).
    client_export.save_client_export_copy(db.get_settings(con), doc_type, data,
                                          filename, pdf_bytes)

    return send_file(io.BytesIO(pdf_bytes), as_attachment=True, download_name=filename,
                     mimetype="application/pdf")


@admin_required
def delete_document(doc_id):
    con = get_db()
    con.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    con.commit()
    flash(_("Документът е изтрит."))
    return redirect(url_for("documents"))


# ---------------------------------------------------------------- generic издаване/преглед
# Замества петте почти еднакви *_new/*_preview двойки от стария app.py.
# Разликите между типовете (needs_items/embed_unload_points/success_message)
# идват от appcore.DOCUMENT_FLOWS — виж там за пълния коментар защо точно
# тези полета и защо success_message е дословен текст, не генериран.

_SENDER_LANG_FIELDS = ("sender_name", "sender_address", "sender_city", "sender_country")


def _apply_sender_lang(settings, sender_lang):
    """При ?sender_lang=en замества BG стойностите на фирмата изпращач
    (в prefill речника settings, ПРЕДИ да стигне до шаблона) с техните
    английски версии от Настройки (sender_name_en/sender_address_en/...),
    ако администраторът ги е попълнил там — виж routes_settings.py и
    templates/settings.html. Пропуска полета без попълнена английска
    версия (остават на BG стойността, вместо да се изпразнят) — така
    непопълненият превод никога не проваля попълването на формата."""
    if sender_lang != "en":
        return
    for field in _SENDER_LANG_FIELDS:
        en_value = (settings.get(field + "_en") or "").strip()
        if en_value:
            settings[field] = en_value


def _document_new(doc_type):
    flow = DOCUMENT_FLOWS[doc_type]
    con = get_db()
    if request.method == "POST":
        data = form_data()
        if flow["needs_items"]:
            data["items"] = parse_items()
        doc_id = save_document(con, doc_type, data)
        flash(_(flow["success_message"]) % data["number"])
        return redirect(url_for("view_document", doc_id=doc_id))
    clients = load_clients(con)
    settings = db.get_settings(con)
    sender_lang = request.args.get("sender_lang") if request.args.get("sender_lang") == "en" else "bg"
    _apply_sender_lang(settings, sender_lang)

    # Възстановяване на въведените данни след „Предварителен преглед" →
    # „Назад към формата" (виж _macros.doc_toolbar/app.preview_document) —
    # ?restore=<token> сочи към същия временен _preview_store запис, който
    # прегледът вече е показал. Пренаизползва СЪЩИЯ edit_data/data-edit/
    # prefillForm() механизъм като редакция на вече издаден документ, само
    # че БЕЗ edit_doc — това си остава истинско издаване на НОВ документ,
    # просто с предварително попълнени полета.
    restore_data = None
    restore_token = request.args.get("restore")
    if restore_token:
        payload = _get_preview(restore_token, "doc")
        if payload is not None and payload[0] == doc_type:
            restore_data = payload[1]

    if flow["embed_unload_points"]:
        cj = clients_json(clients, con)  # con все още отворен — вгражда unload_points
        ctx = {"clients": clients, "clients_json": cj, "s": settings, "sender_lang": sender_lang}
    else:
        ctx = {"clients": clients, "clients_json": clients_json(clients), "s": settings,
               "sender_lang": sender_lang}
    if flow["needs_items"]:
        ctx["items"] = restore_data.get("items", []) if restore_data else []
    if restore_data is not None:
        ctx["edit_data"] = restore_data
    return render_template(flow["form_template"], **ctx)


def _document_preview(doc_type):
    flow = DOCUMENT_FLOWS[doc_type]
    data = form_data()
    if flow["needs_items"]:
        data["items"] = parse_items()
    return render_preview(doc_type, data)


# ---------------------------------------------------------------- ЧМР

@login_required
def cmr_new():
    return _document_new("cmr")


@login_required
def cmr_preview():
    return _document_preview("cmr")


# ---------------------------------------------------------------- Опаковъчен лист

@login_required
def packing_new():
    return _document_new("packing")


@login_required
def packing_preview():
    return _document_preview("packing")


# ---------------------------------------------------------------- Палетна карта

@login_required
def pallet_new():
    return _document_new("pallet")


@login_required
def pallet_preview():
    return _document_preview("pallet")


# ---------------------------------------------------------------- Товарителница (вътрешен превоз)

@login_required
def waybill_new():
    return _document_new("waybill")


@login_required
def waybill_preview():
    return _document_preview("waybill")


# ---------------------------------------------------------------- Декларация за двойна употреба

@login_required
def dualuse_new():
    return _document_new("dualuse")


@login_required
def dualuse_preview():
    return _document_preview("dualuse")


# ---------------------------------------------------------------- Декларация за износ (Италия)

@login_required
def export_it_new():
    return _document_new("export_it")


@login_required
def export_it_preview():
    return _document_preview("export_it")
