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
import os

from flask import (abort, flash, redirect, render_template, request, send_file,
                   session, url_for)
from flask_babel import gettext as _

import attachments
import client_export
import db
import invoice_clients_module
import pdf_export
import qr_code
from appcore import (DOCUMENT_FLOWS, PRINT_TEMPLATES, _get_preview, admin_required,
                     clients_json, fetch_document, form_data, format_bg_date,
                     format_eur_amount, get_db, invoice_row_total, invoice_row_weight,
                     load_clients, login_required, pallet_total_qty, parse_items,
                     render_preview, save_document)

FORM_TEMPLATES = {k: v["form_template"] for k, v in DOCUMENT_FLOWS.items()}


def register(app):
    app.add_url_rule("/docs", "documents", documents)
    app.add_url_rule("/doc/<int:doc_id>", "view_document", view_document)
    # Публичен, БЕЗ вход преглед през QR код на бланката (заявка: „всеки,
    # който сканира с телефон баркода..., без да има нужда от домейна,
    # който е в програмата“) — нарочно ИЗВЪН /doc/<int:doc_id>, за да не се
    # разчита на предвидимо поредно ID; виж public_document_view по-долу.
    app.add_url_rule("/p/<token>", "public_document_view", public_document_view)
    app.add_url_rule("/doc/<int:doc_id>/edit", "edit_document", edit_document, methods=["GET", "POST"])
    app.add_url_rule("/doc/<int:doc_id>/export.xlsx", "export_document_xlsx", export_document_xlsx)
    app.add_url_rule("/doc/<int:doc_id>/export.pdf", "export_document_pdf", export_document_pdf)
    app.add_url_rule("/doc/<int:doc_id>/delete", "delete_document", delete_document, methods=["POST"])
    app.add_url_rule("/doc/<int:doc_id>/attachments", "document_attachment_upload",
                     document_attachment_upload, methods=["POST"])
    app.add_url_rule("/doc/<int:doc_id>/attachments/<int:attachment_id>",
                     "document_attachment_view", document_attachment_view)
    app.add_url_rule("/doc/<int:doc_id>/attachments/<int:attachment_id>/delete",
                     "document_attachment_delete", document_attachment_delete,
                     methods=["POST"])

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
    # Фактурите НЕ се показват тук — те имат собствен списък в раздел
    # „Фактури“ (заявка: „само там да се появяват издадените фактури“).
    # Виж db.INVOICE_DOC_TYPES.
    sql = ("SELECT d.*, u.full_name AS author FROM documents d"
           " LEFT JOIN users u ON u.id = d.created_by"
           " WHERE d.doc_type NOT IN (%s)"
           % ",".join("?" for _ in db.INVOICE_DOC_TYPES))  # nosec B608 -- само „?“ плейсхолдъри по брой; стойностите са bound параметри
    params = list(db.INVOICE_DOC_TYPES)
    if doc_type in db.DOC_TYPES and doc_type not in db.INVOICE_DOC_TYPES:
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


def _public_doc_url(token):
    """Пълен адрес — текущия домейн, от който в момента се преглежда
    страницата (локален/LAN мрежов режим/Cloudflare тунел, каквото и да е,
    виж remote_tunnel.py), НЕ нещо ръчно вписано/конфигурирано някъде.
    Затова QR кодът на бланката „просто работи“ независимо как точно е
    достигната програмата в момента на печат — заявка: „без да има нужда
    от домейна, който е в програмата“.

    НАРОЧНО без @login_required — извиква се и от view_document (с вход)
    И от public_document_view (без вход, виж по-долу); decorator тук би
    развалил точно втория случай (би връщал пренасочване към /login
    вместо адрес, вграждан после в QR картинката)."""
    return request.host_url.rstrip("/") + url_for("public_document_view", token=token)


def _public_doc_context(row):
    """(public_url, qr_data_uri) за документа, или (None, None), ако
    документният тип няма публичен QR (фактури — по изричен избор на
    потребителя: „само документите с баркод вече“) или документът все още
    няма public_token (защитна проверка — не би трябвало да се случи след
    db._m002_public_token, всеки запис минава през миграцията при старт)."""
    if row["doc_type"] in db.INVOICE_DOC_TYPES or not row["public_token"]:
        return None, None
    url = _public_doc_url(row["public_token"])
    return url, qr_code.qr_png_data_uri(url)


@login_required
def view_document(doc_id):
    con = get_db()
    row, data = fetch_document(con, doc_id)
    copies = request.args.get("copies", type=int) or 1
    label_format = request.args.get("format") == "label"
    public_url, qr_data_uri = _public_doc_context(row)
    return render_template(PRINT_TEMPLATES[row["doc_type"]], doc=row, d=data,
                           copies=min(copies, 5), preview=False,
                           label_format=label_format,
                           doc_attachments=attachments.list_attachments(con, doc_id),
                           public_url=public_url, qr_data_uri=qr_data_uri)


def public_document_view(token):
    """Публичен преглед на документ БЕЗ вход, през QR кода на бланката —
    заявка: „всеки, който сканира с телефон баркода на някой от
    документите, да му се зареди директно документа, без да има нужда от
    домейна, който е в програмата“ + уточнение „само документа, нищо друго
    да не вижда“ (виж templates/base.html — public_view=True кара базовия
    шаблон да пропусне страничната лента/навигация/скенер формите изцяло,
    дори ако браузърът случайно вече има активна сесия).

    НАРОЧНО без @login_required/@admin_required — целият смисъл на
    заявката е точно обратното на изискване за вход. Защитата тук е
    ЕДИНСТВЕНО непредвидимостта на token (128-битов, виж
    db._m002_public_token) — за разлика от `barcode` (предвидим формат
    ТИП-ДДММГГГГ-####, лесен за изброяване), token не издава нищо за кой
    да е ДРУГ документ в базата, дори на човек, който познае модела.

    Непознат token ИЛИ токен на фактура (изрично изключени от заявката)
    връща обикновено 404 — не пренасочва към вход, това би издало, че
    адресът просто е "чужд", вместо "невалиден"."""
    con = get_db()
    doc_id = db.get_document_id_by_public_token(con, token)
    if doc_id is None:
        abort(404)
    row, data = fetch_document(con, doc_id)
    if row["doc_type"] in db.INVOICE_DOC_TYPES:
        abort(404)
    public_url, qr_data_uri = _public_doc_context(row)
    return render_template(PRINT_TEMPLATES[row["doc_type"]], doc=row, d=data,
                           copies=1, preview=False, label_format=False,
                           doc_attachments=[], public_url=public_url,
                           qr_data_uri=qr_data_uri, public_view=True)


@login_required
def document_attachment_upload(doc_id):
    con = get_db()
    fetch_document(con, doc_id)  # 404, ако документът не съществува
    file = request.files.get("attachment")
    if not file or not file.filename:
        flash(_("Моля, изберете файл (снимка или PDF)."), "error")
        return redirect(url_for("view_document", doc_id=doc_id))
    try:
        attachments.save_attachment(con, doc_id, file, uploaded_by=session["user_id"])
        flash(_("Файлът е прикачен към документа."), "success")
    except ValueError as exc:
        flash(_("Файлът не бе приет: %s") % exc, "error")
    return redirect(url_for("view_document", doc_id=doc_id))


@login_required
def document_attachment_view(doc_id, attachment_id):
    con = get_db()
    fetch_document(con, doc_id)  # 404, ако документът не съществува
    row = attachments.get_attachment(con, doc_id, attachment_id)
    if row is None:
        abort(404)
    path = attachments.attachment_path(doc_id, row)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=attachments.mimetype(row["ext"]),
                     download_name=row["filename"], as_attachment=False)


@admin_required
def document_attachment_delete(doc_id, attachment_id):
    con = get_db()
    fetch_document(con, doc_id)  # 404, ако документът не съществува
    if attachments.delete_attachment(con, doc_id, attachment_id):
        flash(_("Прикаченият файл е изтрит."), "success")
    else:
        flash(_("Файлът вече не съществува."), "warning")
    return redirect(url_for("view_document", doc_id=doc_id))


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
        # Кои типове имат редове артикули идва от DOCUMENT_FLOWS (същия
        # регистър, който управлява и издаването), а НЕ от изброен тук
        # списък — при добавяне на нов тип с редове (напр. фактурите)
        # изброеният списък се пропускаше лесно и редовете тихо изчезваха
        # при редакция на вече издаден документ.
        if DOCUMENT_FLOWS[doc_type]["needs_items"]:
            new_data["items"] = parse_items()
            if "items_format" in data:
                new_data["items_format"] = data["items_format"]
        # Баркодът винаги се пази от оригинала — редакцията не преиздава
        # нов. Номерът също, С ИЗКЛЮЧЕНИЕ на типовете с РЪЧЕН номер
        # (фактурите): там номерът е въведен от оператора и трябва да може
        # да се поправи при редакция, иначе сгрешен номер остава завинаги.
        number = row["number"]
        manual_field = DOCUMENT_FLOWS[doc_type]["manual_number_field"]
        if manual_field:
            typed = (new_data.get(manual_field) or "").strip()
            if typed and typed != number:
                _warn_if_number_already_used(con, doc_type, typed)
                number = typed
            _warn_if_mixed_orders(new_data.get("items"))
        new_data["number"] = number
        new_data["barcode"] = row["barcode"]
        con.execute("UPDATE documents SET data = ?, number = ? WHERE id = ?",
                    (json.dumps(new_data, ensure_ascii=False), number, doc_id))
        con.commit()
        flash(_("Документ № %s е обновен.") % number, "success")
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
    if DOCUMENT_FLOWS[doc_type]["needs_items"]:
        ctx["items"] = data.get("items", [])
    if DOCUMENT_FLOWS[doc_type]["invoice_clients"]:
        ctx["invoice_clients"] = invoice_clients_module.load_all(con)
        ctx["invoice_clients_json"] = invoice_clients_module.as_json(con)
    return render_template(FORM_TEMPLATES[doc_type], **ctx)


# ---------------------------------------------------------------- износ в Excel (.xlsx)
# Данните на всеки документ (номер, страни, стоки и т.н.) + редовете
# артикули (ако има) — в удобен за отваряне в Excel файл. PDF не се
# генерира отделно — печатните шаблони вече поддържат "Save as PDF" през
# диалога за печат на браузъра (вграден във Windows/Chromium, работи offline,
# без нужда от допълнителни компоненти в самата програма).

#: Заглавните полета на фактурите — общи за двата типа (виж коментара при
#: използването им в _XLSX_FIELDS по-долу).
_INVOICE_FIELDS = [
    ("Дата", "doc_date"), ("Държава на произход", "country_origin"),
    ("Вид транспорт", "transport_way"), ("Условия на плащане", "terms_payment"),
    ("Условия на доставка", "terms_delivery"), ("Валута", "currency"),
    ("Акредитив №", "lc_number"), ("Потвърждение №", "confirmation_number"),
    ("Банкови данни", "bank_details"),
    ("Изпращач", "sender_name"), ("Адрес изпращач", "sender_address"),
    ("ДДС № изпращач", "sender_vat"), ("Телефон изпращач", "sender_phone"),
    ("Получател", "consignee_name"), ("Адрес получател", "consignee_address"),
    ("Телефон получател", "consignee_phone"),
    ("Фактура до", "billto_name"), ("Адрес за фактуриране", "billto_address"),
    ("Телефон за фактуриране", "billto_phone"),
    ("Описание на стоката", "description"), ("Забележки", "notes"),
]

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
        ("Лице за контакт (изпращач)", "sender_contact"), ("Телефон изпращач", "sender_phone"),
        ("Имейл изпращач", "sender_email"),
        ("Получател", "receiver_name"), ("Адрес получател", "receiver_address"),
        ("Град получател", "receiver_city"), ("Държава получател", "receiver_country"),
        ("Лице за контакт (получател)", "receiver_contact"), ("Телефон получател", "receiver_phone"),
        ("Имейл получател", "receiver_email"),
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
    # Двете фактури имат ЕДНАКВИ заглавни полета (различават се само по
    # колоните на стоките, виж _XLSX_ITEM_COLUMNS) — с едно изключение:
    # „Потвърждение №“ (Confirmation Number) го има само норвежкият
    # образец. Оставено е и в двата списъка нарочно: при Бразилия полето
    # просто е празно, вместо да се поддържат два почти еднакви списъка,
    # които лесно се разминават при следваща промяна.
    "invoice_br": _INVOICE_FIELDS,
    "invoice_no": _INVOICE_FIELDS,
    "invoice_dubai": _INVOICE_FIELDS,
}

_XLSX_ITEM_COLUMNS = {
    # Ред на колоните по образеца PL.xlsx: Вид опаковка първа, после
    # Описание на материала (виж packing_form.html/packing_print.html).
    "packing": [("packing", "Вид опаковка"), ("description", "Описание на материала"),
               ("qty", "Брой"),
               ("length", "Дължина, мм"), ("width", "Широчина, мм"), ("height", "Височина, мм"),
               ("volume", "Обем, м³"), ("net", "Нето, кг"), ("gross", "Бруто, кг")],
    "pallet_generic": [("code", "Артикул/код"), ("description", "Описание"),
                       ("qty", "Количество"), ("weight", "Тегло, кг")],
    "pallet_orders": [("order_no", "Поръчка №"), ("pos", "Позиция"), ("reference", "Референция"),
                      ("reference_desc", "Описание"), ("qty", "Количество")],
    "waybill": [("description", "Наименование"), ("packing", "Опаковка"), ("marks", "Маркировка/номера"),
               ("weight", "Тегло, кг"), ("qty", "Брой")],
    # Колоните на всяка фактура са ТОЧНО тези от съответния образец и в
    # неговия ред (виж invoice_br_print.html / invoice_no_print.html) —
    # Бразилия с нето тегло и без описание, Норвегия с описание и палет №,
    # без тегло. „Обща цена“/„Общо тегло“ са изчислени колони (виж
    # _INVOICE_COMPUTED_COLUMNS в _export_fields_and_items).
    "invoice_br": [("hs_code", "HS code"), ("po_no", "P.O NO"), ("pos", "Pos"),
                   ("net_weight", "Нето тегло, кг/бр"), ("material_code", "Код на материала"),
                   ("qty", "Количество"), ("unit_price", "Единична цена, EUR"),
                   ("__row_total__", "Обща цена, EUR"), ("__row_weight__", "Общо тегло, кг")],
    "invoice_no": [("hs_code", "HS code"), ("description", "Описание на материала"),
                   ("pallet_no", "Палет №"), ("po_no", "P.O NO"), ("pos", "Pos"),
                   ("material_code", "Код на материала"), ("qty", "Количество"),
                   ("unit_price", "Единична цена, EUR"),
                   ("__row_total__", "Обща цена, EUR")],
    # Дубай (образец 12971.pdf): нито нето тегло, нито описание — само
    # HS code, P.O NO, Pos, Material code, Quantity, Unit Price.
    "invoice_dubai": [("hs_code", "HS code"), ("po_no", "P.O NO"), ("pos", "Pos"),
                      ("material_code", "Код на материала"), ("qty", "Количество"),
                      ("unit_price", "Единична цена, EUR"),
                      ("__row_total__", "Обща цена, EUR")],
}


#: Полета, показвани с "€" суфикс в износите (Excel/PDF) — заявка: "да
#: остане валута само евро". Само товарителницата за вътрешен превоз има
#: парични полета в момента (transport_price/extra_costs) — вижте
#: appcore.format_eur_amount за самото форматиране (споделено и с
#: waybill_print.html чрез Jinja global format_eur).
_MONEY_FIELDS = {"waybill": {"transport_price", "extra_costs"}}

#: Полета с ISO дата (или дата-час), които при износ (Excel/PDF) трябва да
#: минат през appcore.format_bg_date, за да излязат във вида „ДД.ММ.ГГГГ“ —
#: заявка: „в цялата програма промени изгледа на дата да е ден.месец.година“,
#: избран обхват включва изрично и Excel/PDF износа. Ключовете идват от
#: <input type="date"> полета в самите форми (ISO стойност) — doc_date не е
#: сред тях, понеже там е свободен текст в повечето шаблони, но е добавен,
#: защото формите го попълват през <input type="date"> навсякъде другаде.
_DATE_FIELDS = {
    "cmr": {"established_date", "date_loading"},
    "packing": {"doc_date"},
    "pallet": {"doc_date"},
    "waybill": {"established_date", "date_loading", "date_delivery",
                "loading_date", "unloading_date"},
    "dualuse": {"doc_date", "invoice_date"},
    "export_it": {"doc_date"},
    "invoice_br": {"doc_date"},
    "invoice_no": {"doc_date"},
    "invoice_dubai": {"doc_date"},
}

#: Изчислените колони на редовете във фактурите — не се пазят в data (за
#: да не могат да се разминат с количеството/цената при по-късна
#: редакция), а се смятат на момента от СЪЩИТЕ функции, които ползват и
#: печатните бланки (appcore.invoice_row_total/invoice_row_weight), за да
#: няма разлика между бланката и Excel/PDF износа.
_INVOICE_COMPUTED_COLUMNS = {
    "__row_total__": invoice_row_total,
    "__row_weight__": invoice_row_weight,
}


def _export_fields_and_items(doc_type, data):
    """Общата логика за "какво да покаже износът" (полета + редове+колони),
    споделена от Excel (export_document_xlsx) и PDF (export_document_pdf)
    износа — вижте pdf_export.py защо PDF-ът нарочно преизползва точно тези
    речници вместо отделен pixel-perfect PDF шаблон."""
    money_keys = _MONEY_FIELDS.get(doc_type, ())
    date_keys = _DATE_FIELDS.get(doc_type, ())
    fields = []
    for label, key in _XLSX_FIELDS.get(doc_type, []):
        # "__total_qty__" е специален случай (само за pallet) — „Общ брой“
        # НЕ се пази като суров запис в data, изчислява се на момента от
        # items (виж appcore.pallet_total_qty), точно както във формата и
        # печатните шаблони.
        value = pallet_total_qty(data.get("items")) if key == "__total_qty__" else data.get(key, "")
        if key in money_keys and value:
            value = format_eur_amount(value)
        elif key in date_keys and value:
            value = format_bg_date(value)
        fields.append((label, value))

    items = data.get("items") or []
    cols = []
    if items:
        if doc_type == "pallet":
            cols = _XLSX_ITEM_COLUMNS["pallet_orders" if data.get("items_format") == "orders"
                                      else "pallet_generic"]
        else:
            cols = _XLSX_ITEM_COLUMNS.get(doc_type, [])

    # Изчислените колони на фактурите ("Обща цена"/"Общо тегло") не
    # съществуват в записаните редове — допълваме ги тук, в КОПИЕ на всеки
    # ред, за да не променяме самите данни на документа.
    computed_keys = [key for key, _label in cols if key in _INVOICE_COMPUTED_COLUMNS]
    if computed_keys:
        enriched = []
        for it in items:
            row = dict(it) if isinstance(it, dict) else {}
            for key in computed_keys:
                row[key] = _INVOICE_COMPUTED_COLUMNS[key](it)
            enriched.append(row)
        items = enriched

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
    row = con.execute("SELECT doc_type FROM documents WHERE id = ?", (doc_id,)).fetchone()
    con.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    con.commit()
    flash(_("Документът е изтрит."), "success")
    # Фактурите не се показват в „Всички документи“ — връщаме към техния
    # собствен списък, иначе изтритата фактура „изчезва в нищото“.
    if row is not None and row["doc_type"] in db.INVOICE_DOC_TYPES:
        return redirect(url_for("invoices_list"))
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


def _warn_if_number_already_used(con, doc_type, number):
    """Предупреждава (без да блокира), ако ръчно въведеният номер на
    фактура вече е използван за същия тип документ — дублиран номер на
    счетоводен документ почти винаги е грешка при преписване, но има и
    редовни случаи (сторниране/преиздаване), затова е предупреждение, не
    забрана."""
    if not number:
        return
    row = con.execute(
        "SELECT 1 FROM documents WHERE doc_type = ? AND number = ? LIMIT 1",
        (doc_type, number),
    ).fetchone()
    if row is not None:
        flash(_("Внимание: вече има издаден документ с номер %s. "
                "Проверете дали номерът е верен.") % number, "warning")


def _warn_if_mixed_orders(items):
    """Предупреждава (без да блокира), ако редовете на фактура са от повече
    от една поръчка — заявка: „във фактури един номер на поръчка да бъде
    на една фактура“. Зареждането от палетна карта/Excel вече разделя по
    поръчка още при избора (виж routes_invoices._split_rows_by_po); това
    тук е последната предпазна мрежа за ръчно добавени/разбъркани редове.
    Редове без попълнен P.O NO не се броят — те не са „втора поръчка“."""
    pos = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        po = (it.get("po_no") or "").strip()
        if po and po not in pos:
            pos.append(po)
    if len(pos) > 1:
        flash(_("Внимание: фактурата съдържа редове от %(count)d различни поръчки "
                "(%(pos)s). Обичайно една фактура се издава за ЕДНА поръчка — "
                "проверете дали останалите не трябва да са на отделни фактури.")
              % {"count": len(pos), "pos": ", ".join(pos)}, "warning")


def _document_new(doc_type):
    flow = DOCUMENT_FLOWS[doc_type]
    con = get_db()
    if request.method == "POST":
        data = form_data()
        if flow["needs_items"]:
            data["items"] = parse_items()
        manual_number = None
        if flow["manual_number_field"]:
            manual_number = (data.get(flow["manual_number_field"]) or "").strip()
            _warn_if_number_already_used(con, doc_type, manual_number)
            _warn_if_mixed_orders(data.get("items"))
        doc_id = save_document(con, doc_type, data, manual_number=manual_number)
        flash(_(flow["success_message"]) % data["number"], "success")
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
    if flow["invoice_clients"]:
        ctx["invoice_clients"] = invoice_clients_module.load_all(con)
        ctx["invoice_clients_json"] = invoice_clients_module.as_json(con)
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
