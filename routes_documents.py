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
import errno
import io
import ipaddress
import json
import math
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from copy import deepcopy
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from flask import (abort, flash, redirect, render_template, request, send_file,
                   session, url_for)
from flask_babel import gettext as _
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

import applog
import attachments
import client_export
import db
import invoice_clients_module
import net
import pdf_export
import qr_code
import remote_tunnel
from appcore import (CLIENT_EMBED_LIMIT, DOCUMENT_FLOWS, PRINT_TEMPLATES, N_, _get_preview,
                     _parse_decimal, _store_preview, count_clients, packing_total_mismatches,
                     admin_required, clients_json, fetch_document, form_data,
                     fmt_num, format_bg_date, format_eur_amount, get_db, invoice_row_total,
                     invoice_row_weight, invoice_totals, json_value_search, load_clients, login_required,
                     negative_item_rows, packing_sum, paginate_documents, pallet_total_qty, parse_items,
                     public_token_expiry, PUBLIC_TOKEN_TTL_DAYS,
                     render_preview, safe_json_data, save_document,
                     suspicious_header_numbers, unparsable_item_rows)

# Одит (12.08.2026, находка №5): SQL израз за извличане на „името на
# клиента“ директно от JSON колоната `data` — СЪЩАТА логика (и същият
# приоритет: consignee_name → receiver_name → client_name) като преди
# прилаганата в Python СЛЕД пагинацията (виж documents() по-долу за пълния
# разказ защо това беше грешно). json_extract е част от вградения в
# SQLite JSON1 extension (стандартно наличен в Python 3.11's sqlite3).
#
# Одит (16.08.2026, находка №2, регресия от находка №5): json_extract()
# хвърля sqlite3.OperationalError ("malformed JSON") при ред с невалиден
# JSON в `data` — потвърдено с изпълнение. Без групиране такъв ред минава
# нормално (safe_json_data() го поглъща на Python ниво), но „Групирай по
# клиент“ гърмеше ЦЕЛИЯ списък заради ЕДИН такъв ред — иронично, точно
# класът срив, заради който съществува safe_json_data (находка К2).
# `json_valid(d.data)` пазачът връща NULL вместо да гърми за такива редове
# (третират се като „без разпознато име на клиент“ — падат накрая на
# списъка, както празно име, вместо да събарят цялата страница).
_CLIENT_NAME_SQL = (
    "COALESCE("
    "NULLIF(TRIM(CASE WHEN json_valid(d.data) THEN json_extract(d.data,'$.consignee_name') END),''),"
    "NULLIF(TRIM(CASE WHEN json_valid(d.data) THEN json_extract(d.data,'$.receiver_name') END),''),"
    "NULLIF(TRIM(CASE WHEN json_valid(d.data) THEN json_extract(d.data,'$.client_name') END),''),"
    "'')"
)

FORM_TEMPLATES = {k: v["form_template"] for k, v in DOCUMENT_FLOWS.items()}


def register(app):
    app.add_url_rule("/docs", "documents", documents)
    # Одит (04.10.2026, UX-№4): износ на ФИЛТРИРАНИЯ списък в Excel — същите
    # параметри на адреса като /docs (type, q, from, to, group).
    app.add_url_rule("/docs/export.xlsx", "documents_export_xlsx", documents_export_xlsx)
    app.add_url_rule("/doc/<int:doc_id>", "view_document", view_document)
    # Публичен, БЕЗ вход преглед през QR код на бланката (заявка: „всеки,
    # който сканира с телефон баркода..., без да има нужда от домейна,
    # който е в програмата“) — нарочно ИЗВЪН /doc/<int:doc_id>, за да не се
    # разчита на предвидимо поредно ID; виж public_document_view по-долу.
    app.add_url_rule("/p/<token>", "public_document_view", public_document_view)
    # Одит (22.08.2026, находка №8): подновяване/отнемане на публичния QR
    # достъп. POST (променят състояние, CSRF от base формата), @login_required —
    # достъпът е фирмен, всеки служител, който вижда документа, трябва да
    # може да „загаси“ изтекъл линк или да спре разпространен такъв.
    app.add_url_rule("/doc/<int:doc_id>/public-link/renew", "public_link_renew",
                     public_link_renew, methods=["POST"])
    app.add_url_rule("/doc/<int:doc_id>/public-link/revoke", "public_link_revoke",
                     public_link_revoke, methods=["POST"])
    app.add_url_rule("/doc/<int:doc_id>/edit", "edit_document", edit_document, methods=["GET", "POST"])
    app.add_url_rule("/doc/<int:doc_id>/copy", "copy_document", copy_document)
    app.add_url_rule("/preview/<token>/issue", "issue_from_preview", issue_from_preview,
                     methods=["POST"])
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
    # Одит (05.10.2026): „Печат на цялата пратка“ — маршрутите и шаблонната
    # функция живеят в shipment.py; регистрират се оттук, за да не се пипат
    # app.py и тестовите/сервизните списъци с модули.
    import shipment
    shipment.register(app)


# ---------------------------------------------------------------- списък/преглед/редакция

#: Одит (находка В15, висок риск): списъкът с документи/фактури четеше
#: фиксирано "ORDER BY d.id DESC LIMIT 300" БЕЗ никаква пагинация в
#: интерфейса — документ №301 (и всеки по-стар) ставаше практически
#: невидим/ненамираем през тези екрани (освен с изричен филтър, изкарващ
#: го под 300-те), тих таван без предупреждение. Заменено с истинска
#: пагинация — вижте documents()/routes_invoices.invoices_list() по-долу.
PAGE_SIZE = 100


def _documents_list_filter():
    """Филтрите на списъка с документи от адреса — споделени от списъка
    (documents) и износа му в Excel (documents_export_xlsx, одит 04.10.2026,
    UX-№4), за да изнася износът ТОЧНО това, което операторът вижда.
    Връща речник с филтрите + where/params/order_by за SQL заявката."""
    doc_type = request.args.get("type", "")
    # Одит (03.09.2026, находка №9): непознат тип се НУЛИРА веднага, не само
    # за SQL филтъра. Шаблонът прави `doc_types[sel_type].title` — суровата
    # стойност от адреса стигаше дотам и вдигаше UndefinedError, тоест
    # анонимно съставим линк (/docs?type=') гарантирано сваляше 500-ка и
    # задействаше пренасочването по Referer (виж и поправката в appcore).
    #
    # Одит (09.09.2026, находка №2): проверката тук беше срещу ЦЕЛИЯ
    # db.DOC_TYPES, включително трите фактурни типа — валидираше ги като
    # „познати“, докато шаблонът вече получава РЕЧНИК БЕЗ тях (виж по-долу).
    # Резултатът би бил точно същият UndefinedError, срещу който тази
    # проверка е създадена: /docs?type=invoice_br щеше да мине проверката,
    # после `doc_types[sel_type].title` да гръмне, защото sel_type вече не
    # е ключ в подадения речник.
    if doc_type not in db.DOC_TYPES or doc_type in db.INVOICE_DOC_TYPES:
        doc_type = ""
    query = request.args.get("q", "").strip()
    group_by_client = request.args.get("group") == "client"
    page = request.args.get("page", 1, type=int) or 1
    # Филтър по диапазон от дати (заявка: подобрения по списъка с
    # документи) — по d.created_at (винаги попълнена автоматично при
    # издаване, виж db.py SCHEMA), не по doc_date от свободните данни на
    # документа (то е свободен текст, попълван ръчно, невинаги налично за
    # всички типове документи). date_from/date_to идват от <input
    # type="date"> (YYYY-MM-DD).
    #
    # Одит (16.08.2026, находка №22, дребна): преди тази поправка тук
    # стоеше `date(d.created_at) >= date(?)`/`<= date(?)` — обвиването на
    # САМАТА КОЛОНА (не параметъра) в `date(...)` прави израза НЕ-sargable
    # (виж СЪЩИЯ разказ в routes_dashboard._dashboard_stats и
    # db._m008_documents_created_at_index) — SQLite не може да ползва
    # индекс по created_at, ако трябва да изчисли функция върху колоната
    # за ВСЕКИ ред. created_at е ВИНАГИ "YYYY-MM-DD HH:MM:SS" (db.py
    # SCHEMA DEFAULT) — лексикографското сравнение directno върху текста
    # дава ТОЧНО СЪЩИЯ резултат за долната граница (>=), а горната (<=,
    # трябва да покрие ЦЕЛИЯ ден на date_to) минава през полуотворен
    # интервал: created_at < date(date_to, '+1 day') — date() тук
    # обвива ПАРАМЕТЪРА, не колоната, затова остава sargable.
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    # Фактурите НЕ се показват тук — те имат собствен списък в раздел
    # „Фактури“ (заявка: „само там да се появяват издадените фактури“).
    # Виж db.INVOICE_DOC_TYPES.
    where = ("WHERE d.doc_type NOT IN (%s)"
            % ",".join("?" for _ in db.INVOICE_DOC_TYPES))  # nosec B608 -- само „?“ плейсхолдъри по брой; стойностите са bound параметри
    params = list(db.INVOICE_DOC_TYPES)
    if doc_type in db.DOC_TYPES and doc_type not in db.INVOICE_DOC_TYPES:
        where += " AND d.doc_type = ?"
        params.append(doc_type)
    if query:
        # В7: ci_contains (db._ci_contains) сгъва регистъра с Python
        # str.lower() (правилно за кирилица), за разлика от LIKE тук.
        # Одит (26.09.2026, находка №11): стойностите, не суровият JSON —
        # виж appcore.json_value_search.
        data_sql, data_params = json_value_search("d.data", query)
        where += " AND (ci_contains(d.number, ?) OR ci_contains(d.barcode, ?) OR %s)" % data_sql  # nosec B608 -- data_sql е фиксиран израз от json_value_search с „?“ плейсхолдъри
        params += [query, query] + data_params
    if date_from:
        where += " AND d.created_at >= ?"
        params.append(date_from)
    if date_to:
        where += " AND d.created_at < date(?, '+1 day')"
        params.append(date_to)
    # Групиране по клиент (заявка: „всеки клиент да се запазват в отделни
    # папки във всички документи“ — тук е UI-групирането, виж
    # client_export.py за реалните папки на диска).
    #
    # Одит (12.08.2026, находка №5, critical): преди тази поправка
    # сортирането по клиент се правеше в Python СЛЕД като SQL заявката
    # вече беше взела само PAGE_SIZE=100 документа (LIMIT/OFFSET) — при
    # филтър с над 100 документа групирането важеше САМО в рамките на
    # текущата страница; документи на един и същ клиент, разпределени на
    # различни страници, изобщо не се събираха заедно — самата цел на
    # функцията отпадаше точно при активна фирма с много документи. Сега
    # сортирането (СЪЩАТА логика — вижте _CLIENT_NAME_SQL по-горе, същия
    # приоритет consignee_name → receiver_name → client_name, празно име
    # накрая) е част от самата SQL заявка, ПРЕДИ LIMIT/OFFSET — пагинацията
    # вече обхожда СОРТИРАНИЯ по клиент резултат, страница по страница,
    # точно както при подредба по номер.
    order_by = "d.id DESC"
    if group_by_client:
        # Одит (16.08.2026, находка №15): ci_lower (db._ci_lower) вместо
        # вграденото LOWER() — вижте db._ci_lower за пълното обяснение
        # защо LOWER() не сгъва кирилица.
        #
        # Одит (05.09.2026, находка №11): сортира се по ПОСТОЯННАТА колона
        # `d.client_name` (db._m011), не по изваждане от JSON-а. Досега
        # изразът стоеше в ORDER BY ТРИ пъти, всяко копие правеше
        # `json_valid` + до три `json_extract` върху цялото тяло на всеки
        # документ, а после temp B-tree сортираше целия резултат — за 100
        # реда. Измерено при 20 000 документа: 441 ms и 170 MB прочетени.
        order_by = ("(d.client_name = '') ASC, ci_lower(d.client_name) ASC,"
                    " d.client_name ASC, d.id DESC")
    return {"doc_type": doc_type, "query": query, "group_by_client": group_by_client,
            "page": page, "date_from": date_from, "date_to": date_to,
            "where": where, "params": params, "order_by": order_by}


@login_required
def documents():
    f = _documents_list_filter()
    doc_type, query, group_by_client = f["doc_type"], f["query"], f["group_by_client"]
    date_from, date_to = f["date_from"], f["date_to"]
    con = get_db()
    docs, page, total_pages, total_count = paginate_documents(
        con, f["where"], f["params"], f["page"], page_size=PAGE_SIZE, order_by=f["order_by"])
    # Одит (01.10.2026, F1d): името на клиента е в постоянната колона
    # d.client_name (db._m011, същият приоритет) — без json.loads на всеки ред.
    metas = [{"client_name": d["client_name"]} for d in docs]
    # Одит (09.09.2026, находка №2): падащото меню показваше и трите
    # фактурни типа, макар заявката ПОСТОЯННО да ги изключва с
    # `NOT IN (...)` малко по-горе (фактурите имат собствен раздел
    # „Фактури“) — изборът им връщаше същия резултат като „без филтър“,
    # тоест никога документ от избрания тип. Огледално на `invoice_types`
    # в routes_invoices.py (обратната посока — там СТЕСНЯВАТ до
    # фактурните типове, тук изключваме ги).
    non_invoice_doc_types = {k: v for k, v in db.DOC_TYPES.items()
                             if k not in db.INVOICE_DOC_TYPES}
    return render_template("documents.html", docs=docs, metas=metas,
                           doc_types=non_invoice_doc_types,
                           sel_type=doc_type, q=query,
                           group_by_client=group_by_client,
                           date_from=date_from, date_to=date_to,
                           page=page, total_pages=total_pages, total_count=total_count)


#: Одит (04.10.2026, UX-№4): таван на реда в износа на списъка (защита от
#: изнасяне на цялата база при забравен филтър; над него — бележка в края).
_LIST_EXPORT_MAX_ROWS = 20000


def _list_sum_items(items, key):
    total = None
    for it in items or []:
        if isinstance(it, dict):
            num = _xlsx_number(it.get(key))
            if num is not None:
                total = (total or 0.0) + num
    return total


def _list_export_values(doc_type, data):
    """Одит (04.10.2026, UX-№4): (PO, фактура №, колети, бруто, нето, обем,
    сума EUR) за един документ — от полетата, които всеки тип реално има."""
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
    po = invoice_no = ""
    packages = gross = net = volume = amount = None
    if doc_type == "cmr":
        packages, gross, volume = data.get("packages"), data.get("weight"), data.get("volume")
    elif doc_type == "packing":
        po, invoice_no = data.get("order_no") or "", data.get("invoice_no") or ""
        packages, gross = data.get("total_packages"), data.get("total_gross")
        net, volume = data.get("total_net"), data.get("total_volume")
    elif doc_type == "pallet":
        orders = []
        for it in items:
            order = str(it.get("order_no") or "").strip()
            if order and order not in orders:
                orders.append(order)
        po = ", ".join(orders)
        gross = data.get("gross")
    elif doc_type == "waybill":
        packages, gross = _list_sum_items(items, "qty"), _list_sum_items(items, "weight")
        parts = [_xlsx_number(data.get(k)) for k in ("transport_price", "extra_costs")]
        parts = [x for x in parts if x is not None]
        amount = sum(parts) if parts else None
    elif doc_type == "dualuse":
        invoice_no = data.get("invoice_numbers") or ""
    elif doc_type == "export_it":
        invoice_no = data.get("invoice_no") or ""
    return po, invoice_no, packages, gross, net, volume, amount


def _list_number_format(col, raw, num):
    """Маска за числова клетка в износа на списъка: сумата — в евро; иначе по
    въведената точност (текст) или по самото число (изчислени суми)."""
    if col == 11:
        return _EUR_NUMBER_FORMAT
    if isinstance(raw, str):
        return _quantity_number_format(raw)
    text = ("%.6f" % num).rstrip("0").rstrip(".")
    return _quantity_number_format(text)


@login_required
def documents_export_xlsx():
    """Одит (04.10.2026, UX-№4): Excel износ на списъка „Издадени документи“
    с ТЕКУЩИТЕ филтри (тип, търсене, период, групиране) — по един ред на
    документ и ред „Общо“. Достъпът е като на самия списък (всеки влязъл
    потребител); фактурите не влизат (те имат собствен раздел)."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    f = _documents_list_filter()
    con = get_db()
    sql = ("SELECT d.id, d.doc_type, d.number, d.created_at, d.client_name, d.data,"
           " (SELECT COUNT(*) FROM document_attachments a WHERE a.document_id = d.id)"
           " AS attachment_count FROM documents d %s ORDER BY %s LIMIT ?"
           % (f["where"], f["order_by"]))  # nosec B608 -- where/order_by са константи с „?“ плейсхолдъри (_documents_list_filter)
    rows = con.execute(sql, list(f["params"]) + [_LIST_EXPORT_MAX_ROWS + 1]).fetchall()
    truncated = len(rows) > _LIST_EXPORT_MAX_ROWS
    rows = rows[:_LIST_EXPORT_MAX_ROWS]
    st = _xlsx_styles()

    headers = [_("Дата"), _("Тип"), "№", _("Клиент"), _("Поръчка №"), _("Фактура №"),
               _("Брой колети"), _("Бруто, кг"), _("Нето, кг"), _("Обем, м³"), _("Сума, EUR"),
               _("Подписано ЧМР прикачено")]
    numeric_cols = (7, 8, 9, 10, 11)
    wb = Workbook()
    ws = wb.active
    ws.title = _("Документи")[:31]
    header_row = _xlsx_append(ws, headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=header_row, column=c)
        cell.font = st["bold"]
        cell.fill = st["head_fill"]
        cell.border = st["border"]
        cell.alignment = st["wrap"]
    totals = {c: 0.0 for c in numeric_cols}
    last = header_row
    for r in rows:
        data = safe_json_data(r["data"])
        doc_type = r["doc_type"]
        doc_day = None
        for key in ("doc_date", "established_date"):
            doc_day = _xlsx_date(data.get(key))
            if doc_day is not None:
                break
        if doc_day is None:
            doc_day = _xlsx_date(r["created_at"])
        po, invoice_no, *numbers = _list_export_values(doc_type, data)
        signed = ""
        if doc_type == "cmr":
            signed = _("Да") if r["attachment_count"] else _("Не")
        values = [doc_day or "", _(db.DOC_TYPES.get(doc_type, {}).get("title", doc_type)),
                  r["number"], r["client_name"] or "", po, invoice_no]
        formats = {}
        for c, raw in zip(range(7, 12), numbers):
            num = _xlsx_number(raw)
            if num is None:
                values.append("" if raw is None else raw)
            else:
                values.append(num)
                totals[c] += num
                formats[c] = _list_number_format(c, raw, num)
        values.append(signed)
        last = _xlsx_append(ws, values)
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=last, column=c)
            cell.border = st["border"]
            cell.alignment = st["top"]
        if doc_day:
            ws.cell(row=last, column=1).number_format = _DATE_NUMBER_FORMAT
        for c, fmt in formats.items():
            ws.cell(row=last, column=c).number_format = fmt
    total_values = [_("Общо"), "", len(rows), "", "", ""]
    total_values += [round(totals[c], 6) for c in range(7, 12)] + [""]
    total_row = _xlsx_append(ws, total_values)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=total_row, column=c)
        cell.font = st["bold"]
        cell.fill = st["label_fill"]
        cell.border = st["border"]
    for c in numeric_cols:
        ws.cell(row=total_row, column=c).number_format = _list_number_format(
            c, None, round(totals[c], 6))
    if truncated:
        _xlsx_append(ws, [_("Показани са първите %(count)d документа — стеснете филтъра, "
                            "за да изнесете останалите.") % {"count": _LIST_EXPORT_MAX_ROWS}])
    widths = [12, 26, 14, 34, 18, 18, 10, 12, 12, 12, 14, 14]
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(headers)), max(last, 1))
    ws.print_title_rows = "1:1"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    applog.log_audit("износ на списъка с документи в Excel", "%d документа" % len(rows))
    return send_file(buf, as_attachment=True,
                     download_name="documents_%s.xlsx" % date.today().strftime("%Y-%m-%d"),
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet")


def _host_is_local_or_private(hostname):
    """Дали адрес с този host би бил достъпен САМО от този компютър или
    само от локалната мрежа (не от телефон на мобилен интернет)."""
    if not hostname or hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False  # истинско домейн име (напр. *.trycloudflare.com) → публично
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _public_doc_url(token, for_print=False):
    """(адрес за QR кода, дали е само локален) — избира НАЙ-ДОСТЪПНИЯ
    ОТВЪН адрес, а не буквално този, от който операторът гледа страницата.

    Поправка на реален проблем (заявка: „QR кодът не показва документа —
    поправи всеки да го вижда“): преди тук се вземаше request.host_url
    дословно, а на инсталираната програма той е http://127.0.0.1:5000 —
    адрес, който на телефона сочи САМИЯ телефон, не компютъра със
    сървъра, затова сканираният QR не отваряше нищо. Редът на избор:

    1. Активен „Отдалечен достъп“ (Cloudflare тунел, виж remote_tunnel.py)
       → неговият публичен https адрес: работи от ВСЯКА мрежа, включително
       мобилен интернет — точно „всеки да го вижда“.
    2. Иначе, ако страницата се гледа през 127.0.0.1/localhost → същият
       порт, но с истинския IP адрес на компютъра в локалната мрежа
       (net.lan_ip): работи от телефони в офисната Wi-Fi мрежа (при
       включен „Мрежов режим“).
    3. Иначе (вече се гледа през LAN IP или собствен домейн) → адресът
       се ползва както е — той вече е най-доброто известно.

    Вторият елемент от резултата (True при локален/частен адрес) кара
    изгледа на оператора да покаже подсказка как да включи достъп
    отвсякъде — само на екрана, не на печатната бланка.

    НАРОЧНО без @login_required — извиква се и от view_document (с вход)
    И от public_document_view (без вход, виж по-долу); decorator тук би
    развалил точно втория случай (би връщал пренасочване към /login
    вместо адрес, вграждан после в QR картинката)."""
    path = url_for("public_document_view", token=token)

    # Одит (19.08.2026, находка №21, средна): тунелният адрес е ЕФИМЕРЕН —
    # `*.trycloudflare.com` поддомейнът е случаен, тунелът се самоспира
    # след 2 часа (виж remote_tunnel._AUTO_STOP_SECONDS), а Cloudflare
    # преизползва тези поддомейни за ЧУЖДИ тунели. Вграден в ХАРТИЕНА
    # бланка, той е бомба със закъснител: шофьор или митничар, сканиращ
    # товарителницата седмица по-късно, попада на нечий чужд сървър —
    # идеална основа за фалшив документ с вида на нашия. Затова тунелният
    # адрес важи само за ЕКРАНА (for_print=False); печатната бланка носи
    # стабилния локален/мрежов адрес, който поне не сочи към непознат.
    # Одит (22.08.2026, находка №2): ПОСТОЯНЕН публичен адрес, ако е зададен.
    #
    # Поправката на №21 (19.08) правилно махна ефимерния trycloudflare адрес
    # от печатната бланка, но не постави НИЩО на негово място — бланката
    # тръгна да носи LAN адрес (безполезен извън офиса), а без LAN изобщо —
    # `127.0.0.1`, тоест ТОЧНО първоначалният дефект, заради който тази
    # функция съществува („на телефона сочи самия телефон“). Затова тук идва
    # изрично настройваният в „Системни настройки“ постоянен адрес (собствен
    # домейн или named tunnel): той НЕ изтича и НЕ се преизползва от чужд
    # тунел, значи е единственият, който има работа върху хартия.
    configured = (db.get_settings(get_db()).get("public_base_url") or "").strip()
    if configured:
        return configured.rstrip("/") + path, False

    if not for_print:
        tunnel = remote_tunnel.status()
        if tunnel.get("status") == "running" and tunnel.get("url"):
            return tunnel["url"].rstrip("/") + path, False

    parts = urlsplit(request.host_url)
    hostname = parts.hostname or ""
    if hostname in ("localhost", "127.0.0.1", "::1"):
        lan = net.lan_ip()
        if lan:
            hostname = lan
    netloc = hostname + (":%d" % parts.port if parts.port else "")
    return "%s://%s%s" % (parts.scheme, netloc, path), _host_is_local_or_private(hostname)


def _public_doc_context(row, for_print=False):
    """(public_url, qr_data_uri, local_hint) за документа, или
    (None, None, False), ако документният тип няма публичен QR (фактури —
    по изричен избор на потребителя: „само документите с баркод вече“) или
    документът все още няма public_token (защитна проверка — не би
    трябвало да се случи след db._m002_public_token, всеки запис минава
    през миграцията при старт)."""
    if row["doc_type"] in db.INVOICE_DOC_TYPES or not row["public_token"]:
        return None, None, False
    url, local = _public_doc_url(row["public_token"], for_print=for_print)
    return url, qr_code.qr_png_data_uri(url), local


@login_required
def view_document(doc_id):
    con = get_db()
    row, data = fetch_document(con, doc_id)
    # Одит (12.08.2026, находка №21): "or 1" НЕ хваща отрицателни стойности
    # (-1 е истинно в Python, "-1 or 1" си остава -1) — ?copies=-1 минаваше
    # без грешка и даваше `range(-1)` в шаблона (празна страница за печат,
    # без никакво съобщение защо). Сега изрично се отхвърля всичко < 1.
    copies = request.args.get("copies", type=int) or 1
    if copies < 1:
        copies = 1
    label_format = request.args.get("format") == "label"
    return render_template(PRINT_TEMPLATES[row["doc_type"]],
                           **_print_context(con, row, data, copies, label_format=label_format))


def _print_context(con, row, data, copies=1, label_format=False, bundle=False):
    """Променливите на печатния шаблон на издаден документ.

    Одит (05.10.2026): изнесено от view_document, за да сглобява „Печат на
    цялата пратка“ (shipment.py) всеки документ със СЪЩИЯ код като
    самостоятелния печат. bundle=True: само съдържанието на бланката — без
    екранните подсказки, прикачените файлове и временния публичен адрес
    (шаблоните пропускат лентата/картите при `bundle`)."""
    # Одит (19.08.2026, находка №21): for_print=True — в QR кода влиза
    # СТАБИЛНИЯТ адрес, защото тази страница Е печатната бланка. Временният
    # публичен адрес на тунела се показва отделно, само на екрана.
    public_url, qr_data_uri, qr_local_hint = _public_doc_context(row, for_print=True)
    if bundle:
        return dict(doc=row, d=data, copies=min(copies, 5), preview=False,
                    label_format=False, doc_attachments=[], remote_public_url=None,
                    print_qr_is_local=False, public_expires_at=None, public_expired=False,
                    public_ttl_days=PUBLIC_TOKEN_TTL_DAYS, public_url=public_url,
                    qr_data_uri=qr_data_uri, qr_local_hint=False, edit_doc_id=None,
                    bundle=True)
    # Одит (22.08.2026, находка №2): вярно, когато печатният QR носи локален
    # адрес — тогава показваме на екрана (не на бланката) как да се оправи.
    print_qr_is_local = bool(public_url) and qr_local_hint
    remote_public_url = None
    if public_url:
        tunnel = remote_tunnel.status()
        if tunnel.get("status") == "running" and tunnel.get("url"):
            remote_public_url = tunnel["url"].rstrip("/") + url_for(
                "public_document_view", token=row["public_token"])
            # Подсказката „включете Отдалечен достъп“ няма смисъл, когато
            # той ВЕЧЕ е включен — на нейно място показваме самия временен
            # публичен адрес (виж _macros.doc_qr).
            qr_local_hint = False
    # Одит (22.08.2026, находка №8, средна): срокът на публичния достъп
    # вече се ВИЖДА. Дотук колоната се попълваше при издаване и после
    # никой (нито код, нито интерфейс) не я четеше — операторът нямаше как
    # да разбере, че QR кодът на бланката ще спре да работи, нито кога.
    public_expires_at = row["public_token_expires_at"] if public_url else None
    public_expired = db.public_token_is_expired(public_expires_at)
    return dict(doc=row, d=data, copies=min(copies, 5), preview=False,
                label_format=label_format,
                doc_attachments=attachments.list_attachments(con, row["id"]),
                remote_public_url=remote_public_url,
                print_qr_is_local=print_qr_is_local,
                public_expires_at=public_expires_at,
                public_expired=public_expired,
                public_ttl_days=PUBLIC_TOKEN_TTL_DAYS,
                public_url=public_url, qr_data_uri=qr_data_uri,
                qr_local_hint=qr_local_hint, edit_doc_id=None)


def _public_link_doc(con, doc_id):
    """Ред от `documents` за подновяване/отнемане на публичния достъп, или
    404. Отделно от fetch_document (което сглобява и данните за показване)
    — тук трябват само типът и токенът."""
    row = con.execute(
        "SELECT id, doc_type, number, public_token, public_token_expires_at"
        " FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@login_required
def public_link_renew(doc_id):
    """Одит (22.08.2026, находка №8): „Поднови за още %d дни“.

    Реалният сценарий: рекламация или митническа проверка месеци след
    доставката. Насрещната страна сканира QR кода от архивното копие на
    бланката и получава страницата „линкът е изтекъл“; операторът отваря
    документа в програмата и с ЕДИН бутон връща достъпа, вместо да
    преиздава документа (което би му дало НОВ номер — недопустимо за вече
    подписан транспортен документ)."""
    con = get_db()
    row = _public_link_doc(con, doc_id)
    new_expiry = public_token_expiry()
    con.execute("UPDATE documents SET public_token_expires_at = ? WHERE id = ?",
                (new_expiry, doc_id))
    con.commit()
    applog.log_audit("подновен публичен достъп до документ",
                     "id=%s %s №%s до %s"
                     % (doc_id, row["doc_type"], row["number"], new_expiry))
    flash(_("Публичният достъп е подновен до %(until)s.")
          % {"until": format_bg_date(new_expiry)}, "success")
    return redirect(url_for("view_document", doc_id=doc_id))


@login_required
def public_link_revoke(doc_id):
    """Одит (22.08.2026, находка №8): „Отнеми достъпа сега“.

    Точно това беше заявено от находка №20 (19.08) и точно това НЕ беше
    доставено: колоната се попълваше при издаване и повече никой не я
    пипаше. Сценарият е бланка, попаднала у когото не трябва (сгрешен
    получател, снимка на документа, напуснал шофьор) — достъпът трябва да
    спре ВЕДНАГА, без да се трие самият документ.

    Срокът се измества с една секунда в МИНАЛОТО (а не се занулява):
    NULL в тази колона означава „безсрочен“, тоест зануляването би било
    точно обратното на исканото."""
    con = get_db()
    row = _public_link_doc(con, doc_id)
    revoked_at = (datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("UPDATE documents SET public_token_expires_at = ? WHERE id = ?",
                (revoked_at, doc_id))
    con.commit()
    applog.log_audit("отнет публичен достъп до документ",
                     "id=%s %s №%s" % (doc_id, row["doc_type"], row["number"]))
    flash(_("Публичният достъп е отнет — QR кодът на бланката вече не отваря "
            "документа."), "success")
    return redirect(url_for("view_document", doc_id=doc_id))


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
    # Одит (22.08.2026, находка №8, средна): непознат и ИЗТЕКЪЛ токен вече
    # НЕ са едно и също. Дотук и двата даваха гол 404 — човек, сканирал
    # архивна бланка при рекламация/митническа проверка шест месеца
    # по-късно, виждаше „не е намерено“ и нямаше как да се досети, че
    # трябва просто да поиска нов линк. Непознатият токен си остава 404
    # (не издаваме нищо за чужди адреси), а изтеклият получава обяснение —
    # БЕЗ никакви данни от документа (виж public_link_expired.html).
    status, doc_id = db.get_public_token_status(con, token)
    if status == db.PUBLIC_TOKEN_MISSING:
        abort(404)
    row, data = fetch_document(con, doc_id)
    if row["doc_type"] in db.INVOICE_DOC_TYPES:
        abort(404)
    if status == db.PUBLIC_TOKEN_EXPIRED:
        # 410 Gone, не 404: ресурсът Е СЪЩЕСТВУВАЛ на този адрес и е
        # премахнат нарочно — точното значение на кода.
        return render_template("public_link_expired.html", public_view=True), 410
    public_url, qr_data_uri, _local = _public_doc_context(row, for_print=True)
    return render_template(PRINT_TEMPLATES[row["doc_type"]], doc=row, d=data,
                           copies=1, preview=False, label_format=False,
                           doc_attachments=[], public_url=public_url,
                           # Подсказката за локален адрес е за ОПЕРАТОРА
                           # (как да включи достъп отвсякъде) — на човека,
                           # който вече е отворил документа през телефона
                           # си, тя не говори нищо.
                           qr_data_uri=qr_data_uri, qr_local_hint=False,
                           public_view=True, edit_doc_id=None)


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
    # Одит (19.08.2026, находка №18, средна): PDF-ите се СВАЛЯТ, не се
    # отварят вградено. Магическите байтове („%PDF-“) доказват, че файлът е
    # PDF, но НЕ че е безобиден скан: PDF с `/OpenAction /JavaScript` или
    # вграден фишинг формуляр минава проверката и — при `as_attachment=False`
    # + `Content-Type: application/pdf` — се отваря в четеца на браузъра
    # В СОБСТВЕНИЯ origin на приложението, с активната сесия на оператора.
    # Всеки логнат служител може да прикачи такъв файл към кой да е
    # документ. Снимките (png/jpg/gif) остават вградени — там няма активно
    # съдържание, а прегледът им на място е реалната причина функцията да
    # съществува.
    inline = row["ext"] != "pdf"
    resp = send_file(path, mimetype=attachments.mimetype(row["ext"]),
                     download_name=row["filename"], as_attachment=not inline)
    # Втора линия: изрично забранява изпълнението на каквото и да е активно
    # съдържание от този отговор, независимо от типа му.
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return resp


@admin_required
def document_attachment_delete(doc_id, attachment_id):
    con = get_db()
    fetch_document(con, doc_id)  # 404, ако документът не съществува
    if attachments.delete_attachment(con, doc_id, attachment_id):
        flash(_("Прикаченият файл е изтрит."), "success")
    else:
        flash(_("Файлът вече не съществува."), "warning")
    return redirect(url_for("view_document", doc_id=doc_id))


def _label(text):
    """Одит (04.10.2026, I5): етикет на поле за показване в интерфейса —
    преведен на езика на потребителя (msgid е българският етикет, маркиран с
    N_ в _XLSX_FIELDS / appcore.PACKING_TOTAL_FIELDS)."""
    return _(text) if text else text


def _short(value, limit=60):
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _conflict_differences(doc_type, saved, mine, limit=8):
    """Одит (26.09.2026, находка №3): при конфликт формата пази ВАШИТЕ
    незаписани данни, но с текущата версия — второ „Запази“ би заменило
    чуждата редакция. Затова операторът трябва да ВИДИ кое точно се
    различава, преди да реши. Връща четими редове „Поле: записано / ваше“."""
    lines = []
    for label, key in _XLSX_FIELDS.get(doc_type, []):
        a, b = saved.get(key), mine.get(key)
        if _short(a) != _short(b):
            lines.append(_("%(field)s: записано „%(saved)s“, ваше „%(mine)s“")
                         % {"field": _label(label), "saved": _short(a) or "—",
                            "mine": _short(b) or "—"})
    if DOCUMENT_FLOWS[doc_type]["needs_items"]:
        a_items, b_items = saved.get("items") or [], mine.get("items") or []
        if a_items != b_items:
            lines.append(_("Редове: записани %(saved)d, във вашата версия %(mine)d "
                           "(съдържанието се различава)")
                         % {"saved": len(a_items), "mine": len(b_items)})
    if len(lines) > limit:
        lines = lines[:limit] + [_("… и още %d разлики") % (len(lines) - limit)]
    return lines


def _flash_edit_conflict(doc_type, saved, mine):
    diffs = _conflict_differences(doc_type, saved, mine)
    msg = _("Документът е бил променен от друг потребител, докато го редактирахте. "
            "Вашите промени НЕ са записани — показани са във формата, за да не се "
            "загубят. Ако натиснете „Запази“, вашата версия ще ЗАМЕНИ записаната. "
            "За да видите записаната версия, отворете документа наново.")
    if diffs:
        msg += " " + _("Записаната версия се различава от вашата в:") + " " + "; ".join(diffs) + "."
    flash(msg, "error")


def _save_document_edit(con, row, data, submitted, submitted_version):
    """Записва редакция на издаден документ (POST от формата или „Запази“ от
    предварителния преглед). `submitted` е данните на формата (с `items` при
    типовете с редове), `submitted_version` — версията от зареждането на формата."""
    doc_id = row["id"]
    doc_type = row["doc_type"]
    # Одит (16.08.2026, находка №39): оптимистично заключване — вижте
    # db._m006_document_version за пълното обяснение. Формата носи
    # версията от МОМЕНТА НА ЗАРЕЖДАНЕТО си (edit_doc_version, скрито
    # поле); ако вече не съвпада с текущата версия в базата, значи друг
    # потребител (или друг таб/устройство на същия) е записал междинна
    # редакция — спираме тук, вместо тихо да я презапишем.
    submitted = dict(submitted)
    # Одит (04.10.2026, F2): потвърждението за номер не е част от документа.
    confirm_reuse = submitted.pop(CONFIRM_NUMBER_REUSE_FIELD, None)
    submitted_version = str(submitted_version or "").strip()
    current_version = row["version"] if "version" in row.keys() else 1
    # Одит (19.08.2026, находка №10, висока — fail-closed): преди това
    # условието беше `if submitted_version.isdigit() and ...`, тоест при
    # ЛИПСВАЩО или нечислово поле проверката просто се ПРОПУСКАШЕ и
    # записът минаваше. Проверено с изпълнение: POST без
    # `edit_doc_version` презаписваше документа безшумно. `WHERE version
    # = ?` в самия UPDATE по-долу не помага — стойността се чете в
    # СЪЩАТА заявка секунди преди UPDATE-а, така че винаги съвпада.
    # Защита, която се изключва сама при липсващо поле, не е защита:
    # сега липсата се третира като конфликт (формата се презарежда с
    # актуалните данни и валидна версия).
    if not submitted_version.isdecimal() or int(submitted_version) != current_version:
        # Одит (03.09.2026, находка №15): и конфликтният изход пази
        # въведеното. Защитата работеше правилно (чуждата редакция не се
        # презаписва), но формата се връщаше ПРАЗНА — при фактура с 200
        # реда това е преписване наново. Съседният `IntegrityError` клон
        # ползва точно този механизъм от 31.08; тук просто не беше
        # приложен. Версията е ТЕКУЩАТА от базата, за да може вторият
        # опит да мине.
        #
        # Одит (05.09.2026, находка №1, ВИСОКА — РЕГРЕСИЯ от горното):
        # `form_data()` изрично ИЗКЛЮЧВА `items_json`; редовете идват
        # само през `parse_items()`. Тоест поправката отгоре пазеше само
        # заглавните полета, а при GET с `?restore=` подаденото ЗАМЕСТВА
        # изцяло данните от базата — формата се рендираше с ПРАЗНА
        # таблица (нито въведените редове, нито съществуващите), докато
        # съобщението твърди „презаредена с актуалните данни“. Оператор,
        # който натисне „Запази“ втори път, ЗАНУЛЯВАШЕ редовете на вече
        # издаден документ. Преди поправката от 03.09 конфликтът просто
        # пренасочваше и редовете си стояха — тоест бях направил нещата
        # ПО-ЛОШИ. Проверено с изпълнение: `items: []` в базата.
        #
        # Сега се пази същото, което пази огледалният клон по-долу:
        # заглавните полета И редовете (плюс `items_format`, който също
        # не идва от `form_data`).
        #
        # Одит (26.09.2026, находка №3): формата носи ВАШИТЕ данни, но
        # текущата версия — второ „Запази“ заменя чуждата редакция.
        # Съобщението твърдеше „презаредена с актуалните данни“, което не
        # беше вярно; сега казва истината и изброява разликите спрямо
        # записаната версия (_flash_edit_conflict), така че презаписът е
        # съзнателно решение, а не тиха загуба.
        conflict_data = dict(submitted)
        if DOCUMENT_FLOWS[doc_type]["needs_items"]:
            if "items_format" in data:
                conflict_data["items_format"] = data["items_format"]
        _flash_edit_conflict(doc_type, data, conflict_data)
        token = _store_preview("doc", (doc_type, conflict_data, doc_id,
                                       current_version))
        return redirect("%s?restore=%s"
                        % (url_for("edit_document", doc_id=doc_id), token)), None
    new_data = dict(submitted)
    # Одит (16.08.2026, находка №37): формите не пресъздават ВИНАГИ
    # всяко поле, което документът може да носи в data (напр. поле от
    # по-стара версия на формата, вече премахнато от шаблона, или поле,
    # попълвано само от друг код път като импорт от Excel/палетна
    # карта) — преди тази поправка new_data = form_data() ЗАМЕСТВАШЕ
    # изцяло старото data, и всяко такова „чуждо“ поле тихо изчезваше
    # при първата редакция. Сега тръгваме от старите данни и само
    # ПРЕЗАПИСВАМЕ с подадените от формата полета — полета извън
    # формата се запазват непроменени.
    merged = dict(data)
    merged.update(new_data)
    new_data = merged
    # Кои типове имат редове артикули идва от DOCUMENT_FLOWS (същия
    # регистър, който управлява и издаването), а НЕ от изброен тук
    # списък — при добавяне на нов тип с редове (напр. фактурите)
    # изброеният списък се пропускаше лесно и редовете тихо изчезваха
    # при редакция на вече издаден документ.
    if DOCUMENT_FLOWS[doc_type]["needs_items"]:
        new_data["items"] = submitted.get("items") or []
        if "items_format" in data:
            new_data["items_format"] = data["items_format"]
        # Одит (16.08.2026, находка №32): _warn_if_negative_values се
        # викаше само при ПЪРВОНАЧАЛНОТО издаване (_document_new) — при
        # редакция на вече издаден документ отрицателен ред минаваше
        # без никакво предупреждение, макар пак да изчезва мълчаливо от
        # сборовете под таблицата (виж appcore.negative_item_rows).
        _warn_if_negative_values(new_data["items"])
        if doc_type == "packing":
            _warn_if_packing_totals_mismatch(new_data)  # находка №8
    # Одит (03.09.2026, находка №6): заглавните числа се проверяват за
    # ВСИЧКИ типове — и за тези без редове (ЧМР), и при редакция.
    _warn_if_suspicious_header_numbers(doc_type, new_data)
    # Баркодът винаги се пази от оригинала — редакцията не преиздава
    # нов. Номерът също, С ИЗКЛЮЧЕНИЕ на типовете с РЪЧЕН номер
    # (фактурите): там номерът е въведен от оператора и трябва да може
    # да се поправи при редакция, иначе сгрешен номер остава завинаги.
    number = row["number"]
    manual_field = DOCUMENT_FLOWS[doc_type]["manual_number_field"]
    if manual_field:
        typed = (new_data.get(manual_field) or "").strip()
        if typed and typed != number:
            # Одит (31.08.2026, находка №20): годината на САМИЯ документ,
            # не текущата — виж помощната функция.
            if _number_taken_by_same_type(con, doc_type, typed, year=row["year"],
                                          exclude_doc_id=doc_id,
                                          confirmed_value=confirm_reuse,
                                          action_label=_("Запази")):
                token = _store_preview("doc", (doc_type, new_data, doc_id, current_version))
                return redirect("%s?restore=%s"
                                % (url_for("edit_document", doc_id=doc_id), token)), None
            number = typed
        _warn_if_mixed_orders(new_data.get("items"))
    new_data["number"] = number
    new_data["barcode"] = row["barcode"]
    # Одит (04.10.2026, F10): при смяна на клиента — псевдонимът на новия;
    # иначе запазеният (клиентът може вече да не е в адресната книга).
    if (doc_type == "pallet"
            and client_export.resolve_client_name(new_data) != client_export.resolve_client_name(data)):
        stamp_client_alias(con, doc_type, new_data)
    try:
        # Одит (16.08.2026, находка №39): "WHERE ... AND version = ?" +
        # проверка на rowcount затваря и тясната междина между проверката
        # по-горе и самия UPDATE (два почти едновременни submit-а) — не
        # само по-грубата разлика, хваната преди началото на функцията.
        cur = con.execute(
            "UPDATE documents SET data = ?, number = ?, version = version + 1"
            " WHERE id = ? AND version = ?",
            (json.dumps(new_data, ensure_ascii=False), number, doc_id, current_version))
        if cur.rowcount == 0:
            con.rollback()
            # Одит (03.09.2026, находка №15): виж горния клон — тясната
            # междина между проверката и самия UPDATE също запазва
            # въведеното. Текущата версия се чете наново, защото
            # чуждият запис вече я е вдигнал.
            fresh = con.execute("SELECT version, data FROM documents WHERE id = ?",
                                (doc_id,)).fetchone()
            try:
                fresh_data = json.loads(fresh["data"]) if fresh else data
            except (TypeError, ValueError):
                fresh_data = data
            _flash_edit_conflict(doc_type, fresh_data, new_data)
            token = _store_preview(
                "doc", (doc_type, new_data, doc_id,
                        fresh["version"] if fresh else current_version))
            return redirect("%s?restore=%s"
                            % (url_for("edit_document", doc_id=doc_id), token)), None
        con.commit()
    except sqlite3.IntegrityError:
        # Одит (16.08.2026, находка №14): огледално на _document_new по-
        # горе — при РЪЧЕН номер (фактурите) редакция, сменяща номера на
        # стойност, заета точно междувременно от друг документ, гърмеше
        # тук с необяснен 500 вместо ясна грешка (виж db._m004/_m005 за
        # уникалния индекс). con.rollback() е нужен, за да не остане
        # отворена транзакция.
        con.rollback()
        # Одит (31.08.2026, находка №4): и тук въведеното се ЗАПАЗВА —
        # редакцията на вече издаден документ е също толкова скъпа за
        # преписване наново, колкото първоначалното въвеждане.
        flash(_("Номер %s вече е зает от друг документ от същата година. "
                "Въведеното е запазено — променете номера и опитайте пак.")
              % number, "error")
        token = _store_preview("doc", (doc_type, new_data, doc_id, current_version))
        return redirect("%s?restore=%s" % (url_for("edit_document", doc_id=doc_id), token)), None
    flash(_("Документ № %s е обновен.") % number, "success")
    return redirect(url_for("view_document", doc_id=doc_id)), doc_id


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
        submitted = _apply_fixed_fields(doc_type, form_data())
        if DOCUMENT_FLOWS[doc_type]["needs_items"]:
            submitted["items"] = _normalize_invoice_items(doc_type, parse_items())
        return _save_document_edit(con, row, data, submitted,
                                   request.form.get("edit_doc_version"))[0]

    # Одит (19.08.2026, находка №25) — виж _document_new по-долу.
    clients = load_clients(con, CLIENT_EMBED_LIMIT)
    clients_total = count_clients(con)
    settings = db.get_settings(con)
    # Възстановяване след „Предварителен преглед" → „Назад към формата" по
    # време на РЕДАКЦИЯ на вече издаден документ (заявка: „при връщане
    # назад от преглед за печат въведената информация се губи") — огледално
    # на СЪЩИЯ механизъм в _document_new по-горе (?restore=<token>), само
    # че тук edit_doc/номерът/баркодът остават от реалния запис в базата
    # (row) — заменя се САМО съдържанието (data), с which формата се
    # предзарежда, за да не изгубим коя точно редакция продължаваме.
    restore_token = request.args.get("restore")
    restored_version = None
    if restore_token:
        payload = _get_preview(restore_token, "doc")
        if payload is not None and payload[0] == doc_type:
            data = payload[1]
            # Одит (19.08.2026, находка №10): версията от МОМЕНТА, в който
            # операторът е започнал редакцията — не пресният ред от базата.
            # len() проверката приема и стари 3-елементни токени, издадени
            # преди обновяването и още живи в паметта.
            if len(payload) > 3:
                restored_version = payload[3]
        else:
            # Одит (16.08.2026, находка №31): токенът за preview изтича
            # (виж _get_preview/PREVIEW_TTL) — до тази поправка при изтекъл/
            # невалиден токен формата тихо зареждаше СТАРИТЕ стойности от
            # базата (row/data по-горе), сякаш нищо не се е случило, и
            # операторът не разбираше, че въведеното в „Предварителен
            # преглед" НЕ е възстановено.
            flash(_("Данните от предварителния преглед вече не са налични (изтекъл "
                    "линк) — показани са последно запазените стойности на документа."),
                  "warning")
    ctx = {
        "clients": clients,
        "clients_json": clients_json(clients, con) if doc_type == "cmr" else clients_json(clients),
        "clients_total": clients_total,
        "s": settings,
        "edit_doc": row,
        "edit_data": data,
        # Одит (19.08.2026, находка №10): шаблоните рендират точно това (а
        # не edit_doc.version), за да оцелее версията през „Преглед → Назад“.
        "edit_doc_version": (restored_version if restored_version is not None
                             else (row["version"] if "version" in row.keys() else 1)),
    }
    if DOCUMENT_FLOWS[doc_type]["needs_items"]:
        ctx["items"] = data.get("items", [])
    if DOCUMENT_FLOWS[doc_type]["invoice_clients"]:
        # Одит (05.09.2026, находка №12): вграждат се първите EMBED_LIMIT
        # записа (при типична инсталация — всичките), а останалите се
        # намират през /invoices/clients/lookup.
        ctx["invoice_clients"] = invoice_clients_module.load_all(
            con, limit=invoice_clients_module.EMBED_LIMIT)
        ctx["invoice_clients_total"] = invoice_clients_module.count_all(con)
        ctx["invoice_clients_json"] = invoice_clients_module.as_json(con)
    return render_template(FORM_TEMPLATES[doc_type], **ctx)


#: Одит (01.10.2026, P1): какво „Копирай като нов“ НЕ пренася — самоличността
#: на документа (номер, баркод). Датите и ръчният номер на фактура се махат
#: отделно (по тип), за да вземе формата своите подразбиращи се стойности.
_COPY_SKIP_KEYS = frozenset(("number", "barcode", "public_token",
                             "public_token_expires_at"))


@login_required
def copy_document(doc_id):
    """„Копирай като нов“: отваря формата за НОВ документ от същия тип,
    попълнена с данните на този (през ?restore=), без номер/баркод/дати."""
    con = get_db()
    row, data = fetch_document(con, doc_id)
    doc_type = row["doc_type"]
    flow = DOCUMENT_FLOWS.get(doc_type)
    if flow is None:
        abort(404)
    skip = set(_COPY_SKIP_KEYS) | set(_DATE_FIELDS.get(doc_type, ()))
    if flow["manual_number_field"]:
        skip.add(flow["manual_number_field"])
    copied = {k: deepcopy(v) for k, v in data.items() if k not in skip}
    token = _store_preview("doc", (doc_type, copied, None, None))
    flash(_("Формата е попълнена с данните от %(title)s № %(number)s. Номерът и "
            "датите не са копирани — проверете данните и издайте новия документ.")
          % {"title": _(db.DOC_TYPES.get(doc_type, {}).get("title", doc_type)),
             "number": row["number"]}, "info")
    return redirect(url_for(doc_type + "_new", restore=token))


# ---------------------------------------------------------------- износ в Excel (.xlsx)
# Данните на всеки документ (номер, страни, стоки и т.н.) + редовете
# артикули (ако има) — в удобен за отваряне в Excel файл. PDF не се
# генерира отделно — печатните шаблони вече поддържат "Save as PDF" през
# диалога за печат на браузъра (вграден във Windows/Chromium, работи offline,
# без нужда от допълнителни компоненти в самата програма).

#: Заглавните полета на фактурите — общи за двата типа (виж коментара при
#: използването им в _XLSX_FIELDS по-долу).
#:
#: Одит (04.10.2026, I5): етикетите са маркирани с N_() — стойността остава
#: българският текст (Excel/PDF износът не се променя), но pybabel ги
#: извлича и предупрежденията/диалогът при конфликт на редакция ги
#: превеждат на мястото на показване (_label) — досега в EN/TR интерфейса
#: излизаше „Бруто тегло, кг“, „Дата на съставяне“…
_INVOICE_FIELDS = [
    (N_("Дата"), "doc_date"), (N_("Държава на произход"), "country_origin"),
    (N_("Вид транспорт"), "transport_way"), (N_("Условия на плащане"), "terms_payment"),
    (N_("Условия на доставка"), "terms_delivery"), (N_("Валута"), "currency"),
    (N_("Акредитив №"), "lc_number"), (N_("Потвърждение №"), "confirmation_number"),
    (N_("Банкови данни"), "bank_details"),
    (N_("Изпращач"), "sender_name"), (N_("Адрес изпращач"), "sender_address"),
    (N_("ДДС № изпращач"), "sender_vat"), (N_("Телефон изпращач"), "sender_phone"),
    (N_("Получател"), "consignee_name"), (N_("Адрес получател"), "consignee_address"),
    (N_("Телефон получател"), "consignee_phone"),
    (N_("Фактура до"), "billto_name"), (N_("Адрес за фактуриране"), "billto_address"),
    (N_("Телефон за фактуриране"), "billto_phone"),
    (N_("Описание на стоката"), "description"), (N_("Забележки"), "notes"),
]

_XLSX_FIELDS = {
    "cmr": [
        (N_("Дата на съставяне"), "established_date"), (N_("Място на съставяне"), "established_place"),
        (N_("Изпращач"), "sender_name"), (N_("Адрес изпращач"), "sender_address"),
        (N_("Град изпращач"), "sender_city"), (N_("Държава изпращач"), "sender_country"),
        # Одит (находка С10): ЕИК/ДДС номерата ги има във формата и на
        # печатната бланка, но липсваха тук — за митнически документ като
        # ЧМР идентификацията по ДДС номер не е козметична подробност.
        (N_("ЕИК/ДДС изпращач"), "sender_eik"),
        (N_("Получател"), "consignee_name"), (N_("Адрес получател"), "consignee_address"),
        (N_("Град получател"), "consignee_city"), (N_("Държава получател"), "consignee_country"),
        (N_("ДДС/ЕИК получател"), "consignee_vat"),
        (N_("Разтоварен пункт"), "place_delivery"), (N_("Товарен пункт"), "place_loading"),
        (N_("Дата на натоварване"), "date_loading"), (N_("Приложени документи"), "attached_docs"),
        (N_("Марки и номера"), "marks"), (N_("Брой колети"), "packages"), (N_("Вид на опаковката"), "packing"),
        (N_("Вид на стоката"), "goods"), (N_("Статистически №"), "stat_no"),
        (N_("Бруто тегло, кг"), "weight"), (N_("Обем, м³"), "volume"),
        (N_("Указания на изпращача"), "sender_instructions"), (N_("Плащане на превоза"), "payment_instructions"),
        (N_("Наложен платеж"), "cod"), (N_("Специални споразумения"), "special_agreements"),
        (N_("Превозвач"), "carrier"), (N_("Последващи превозвачи"), "successive_carriers"),
        (N_("Рег. № влекач"), "truck_reg"), (N_("Рег. № ремарке"), "trailer_reg"), (N_("Шофьор"), "driver"),
        (N_("Резерви на превозвача"), "reservations"),
    ],
    "packing": [
        (N_("Дата"), "doc_date"), (N_("Изпращач"), "sender_name"), (N_("Адрес изпращач"), "sender_address"),
        (N_("Лице за контакт (изпращач)"), "sender_contact"), (N_("Телефон изпращач"), "sender_phone"),
        (N_("Имейл изпращач"), "sender_email"),
        (N_("Получател"), "receiver_name"), (N_("Адрес получател"), "receiver_address"),
        (N_("Град получател"), "receiver_city"), (N_("Държава получател"), "receiver_country"),
        (N_("Лице за контакт (получател)"), "receiver_contact"), (N_("Телефон получател"), "receiver_phone"),
        (N_("Имейл получател"), "receiver_email"),
        (N_("Фактура №"), "invoice_no"), (N_("Поръчка №"), "order_no"),
        (N_("Условия на доставка"), "terms_delivery"), (N_("Вид транспорт"), "transport_type"),
        (N_("HS Code"), "hs_code"),
        (N_("Общо колети"), "total_packages"), (N_("Общо обем, м³"), "total_volume"),
        (N_("Общо нето, кг"), "total_net"), (N_("Общо бруто, кг"), "total_gross"),
        (N_("Забележки"), "notes"),
    ],
    "pallet": [
        (N_("Дата"), "doc_date"), (N_("Палет №"), "pallet_no"), (N_("Тип палет"), "pallet_type"),
        (N_("Изпращач"), "sender_name"), (N_("Клиент"), "client_name"), (N_("Адрес клиент"), "client_address"),
        (N_("Град клиент"), "client_city"), (N_("Държава клиент"), "client_country"),
        (N_("Вид опаковка"), "packaging_type"), (N_("Общ брой"), "__total_qty__"),
        (N_("Бруто, кг"), "gross"), (N_("Височина, см"), "height"),
        (N_("Свързано ЧМР №"), "ref_cmr"), (N_("Забележки"), "notes"),
    ],
    "waybill": [
        (N_("Издадена в"), "established_place"), (N_("Издадена на"), "established_date"),
        (N_("Изпращач"), "sender_name"), (N_("Адрес изпращач"), "sender_address"),
        (N_("Превозвач"), "carrier_name"), (N_("Адрес превозвач"), "carrier_address"),
        (N_("Получател"), "consignee_name"), (N_("Адрес получател"), "consignee_address"),
        (N_("Град получател"), "consignee_city"), (N_("Държава получател"), "consignee_country"),
        (N_("Място на натоварване"), "place_loading"), (N_("Дата на натоварване"), "date_loading"),
        (N_("Място на разтоварване"), "place_delivery"), (N_("Дата на разтоварване"), "date_delivery"),
        (N_("Пробег, км"), "mileage"),
        (N_("Опасен товар — клас"), "dangerous_class"), (N_("Опасен товар — наименование"), "dangerous_name"),
        (N_("Придружител на товара"), "escort_name"), (N_("Брой придружители"), "escort_count"),
        (N_("Превозна цена, EUR"), "transport_price"), (N_("Допълнителни разходи, EUR"), "extra_costs"),
        (N_("Марка на автомобила"), "vehicle_make"), (N_("Модел на автомобила"), "vehicle_model"),
        (N_("Рег. № на автомобила"), "vehicle_reg"), (N_("Пътен лист №"), "route_sheet_no"),
        (N_("Инструкции на превозвача"), "carrier_instructions"),
        (N_("Натоварване — дата"), "loading_date"), (N_("Натоварване — от час"), "loading_from"),
        (N_("Натоварване — до час"), "loading_to"),
        (N_("Разтоварване — дата"), "unloading_date"), (N_("Разтоварване — от час"), "unloading_from"),
        (N_("Разтоварване — до час"), "unloading_to"),
        (N_("Забележка"), "notes"),
    ],
    "dualuse": [
        (N_("Дата"), "doc_date"), (N_("Износител"), "sender_name"), (N_("ЕИК/ЕГН"), "sender_eik"),
        (N_("Фактура/и №"), "invoice_numbers"), (N_("Дата на фактурата"), "invoice_date"),
        (N_("Държава на износ"), "destination_country"), (N_("Място на съставяне"), "place"),
        # Одит (01.09.2026, девети одит, находка №7): `place_country` излизаше
        # САМО на бланката (dualuse_print.html: „{{ d.place }}{% if
        # d.place_country %}, {{ d.place_country }}{% endif %}“) — и
        # CHANGELOG-ът, и тестът от онази поправка покриват само печата.
        # Стойността се записва от формата (appcore.form_data), но никога не
        # стигаше до износа: бланката казваше „Габрово, България“, а Excel и
        # PDF — само „Габрово“. Класическата „непокрита половина“.
        (N_("Държава на съставяне"), "place_country"),
        (N_("Декларатор"), "declarant_name"), (N_("Длъжност"), "declarant_position"),
    ],
    "export_it": [
        (N_("Дата"), "doc_date"), (N_("Декларатор"), "declarant_name"),
        (N_("Пълномощник на"), "represented_company"), (N_("Фактура №"), "invoice_no"),
        (N_("Износител"), "exporter_company"), (N_("Получател"), "receiver_name"),
        (N_("Ref. ЧМР №"), "ref_cmr"), (N_("Място на съставяне"), "place"),
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
    # без тегло. „Обща цена“ е изчислена колона (виж
    # _INVOICE_COMPUTED_COLUMNS в _export_fields_and_items).
    #
    # Одит (04.10.2026, X2): БЕЗ „Общо тегло, кг“. Колоната „Total weight“
    # беше махната от бланката по изрична заявка на потребителя, но Excel/
    # PDF износът продължаваше да я носи (и тегло в реда TOTAL) — износът
    # трябва да следва бланката. „Нето тегло, кг/бр“ остава (има я и бланката).
    "invoice_br": [("hs_code", "HS code"), ("po_no", "P.O NO"), ("pos", "Pos"),
                   ("net_weight", "Нето тегло, кг/бр"), ("material_code", "Код на материала"),
                   ("qty", "Количество"), ("unit_price", "Единична цена, EUR"),
                   ("__row_total__", "Обща цена, EUR")],
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

# Одит (16.08.2026, находка №19, средна): всички стойности в data/items се
# пазят като ТЕКСТ (формите ги подават суров request.form) — преди тази
# поправка export_document_xlsx ги записваше В КЛЕТКАТА КАТО ТЕКСТ дори за
# колони, които сa по същество числа (количество/тегло/цена), затова Excel
# ги показваше подравнени вляво (текстов формат), не участваха в SUM()
# формула без ръчно "Convert to Number" от получателя, и не се сортираха
# числово. Списъкът тук изброява ключовете на колоните от _XLSX_ITEM_COLUMNS
# по-горе, за които export_document_xlsx (по-долу) записва РЕАЛНО число
# (float) с number_format, вместо суровия текст — вижте и цитата в
# КОЛОНИ по-горе за кои полета НЕ са тук нарочно (hs_code/po_no/pos/
# material_code/reference/code — кодове, не количества, ПАЗЯТ водещи нули
# и не бива да минават през числово форматиране).
_NUMERIC_ITEM_COLUMN_KEYS = {
    "qty", "weight", "net", "gross", "length", "width", "height", "volume",
    "net_weight", "unit_price", "__row_total__", "__row_weight__",
}

#: Одит (19.08.2026, находка №31): ЗАГЛАВНИТЕ (обобщаващи) полета на
#: документа, които са числа и трябва да се запишат в Excel като истински
#: числа, не като текст. Поправката на находка №19 (16.08) покри само
#: РЕДОВЕТЕ артикули — заглавните полета останаха низове, при това точно
#: онези, които получателят най-често сумира („Общо нето, кг“, „Общо бруто,
#: кг“, „Общо обем, м³“, „Общо колети“). Проверено: в един и същ файл
#: клетките на редовете бяха числа (0.0625), а „Общо нето, кг“ — текстът
#: „1.11“ (подравнен вляво, невъзможен за SUM/сортиране).
#:
#: Паричните полета (_MONEY_FIELDS) и датите (_DATE_FIELDS) СЪЗНАТЕЛНО не
#: са тук — те вече минават през форматиране („123.45 €“, „07.08.2026“) и
#: са текст по предназначение.
_NUMERIC_FIELD_KEYS = {
    "total_net", "total_gross", "total_volume", "total_packages",
    "gross", "height", "length", "width", "net", "volume", "weight",
    "__total_qty__",
}


#: Полета, показвани с "€" суфикс в износите (Excel/PDF) — заявка: "да
#: остане валута само евро". Само товарителницата за вътрешен превоз има
#: парични полета в момента (transport_price/extra_costs) — вижте
#: appcore.format_eur_amount за самото форматиране (споделено и с
#: waybill_print.html чрез Jinja global format_eur).
_MONEY_FIELDS = {"waybill": {"transport_price", "extra_costs"}}

#: Одит (31.08.2026, находка №10): ключовете на редовите колони, които са
#: ПАРИ, а не количества — общата маска за количества („0.###“) режеше
#: единичната цена 0.0125 до „0.013“, а обикновена цена „1.20“ показваше
#: като „1.2“.
#:
#: Одит (01.09.2026, девети одит, находка №9): маската вече НЕ е една и съща
#: за двете. „0.00“ реши горния проблем, но създаде огледален: единичната
#: цена е СВОБОДЕН текст (въвежда се ръчно или идва от Excel импорт), тоест
#: може законно да има повече от два знака — 0.0125 се показваше „0.01“,
#: докато бланката и PDF-ът (fmt_num, пази въведената точност) казват
#: 0.0125. Получателят пресмята 1000 × 0.01 = 10.00, а редът TOTAL твърди
#: 12.50 — точно противоречието, срещу което е писана самата находка №10.
#: „0.00###“ показва НАЙ-МАЛКО два знака (счетоводният вид на цена) и до
#: пет, ако въведеното наистина ги има. Общата сума на реда (`__row_total__`)
#: остава с точно два — тя минава през `_fmt_money`, който сам квантува до
#: два, значи повече знаци там са невъзможни по конструкция.
#:
#: Одит (04.10.2026, X3): с разделител за хиляди („#,##0“ — в българския
#: Excel излиза „1 234,50“).
_MONEY_ITEM_COLUMN_FORMATS = {"unit_price": "#,##0.00###", "__row_total__": "#,##0.00"}
#: Одит (04.10.2026, X3): сума в евро като ИСТИНСКО число (редът TOTAL на
#: фактурата, превозната цена на товарителницата) — досега „123.45 €“ беше
#: текст и не влизаше в =SUM().
_EUR_NUMBER_FORMAT = '#,##0.00 "€"'
#: Одит (04.10.2026, X3): датите — истински дати на Excel, показвани ДД.ММ.ГГГГ.
_DATE_NUMBER_FORMAT = "dd.mm.yyyy"

#: Одит (03.09.2026, находка №5): маската за КОЛИЧЕСТВА/ТЕГЛА/ОБЕМИ. Беше
#: „0.###“ — три знака — и режеше точно това, което поправките от 31.08 и
#: 01.09 оправиха при парите: тегло 0.0875 се ПОКАЗВАШЕ като „0.088“, а
#: 0.087135 като „0.087“, докато бланката и PDF-ът (fmt_num пази въведената
#: точност) казват пълната стойност. Не е хипотетично: справочникът
#: материали записва теглата с „%.6f“ (materials._weight_cell), тоест 4–6
#: знака са норма, а самите те влизат във фактурата през търсенето по код.
#: Получателят на Excel файла смята 100 × 0.088 = 8.8 кг, а колоната „Общо
#: тегло“ и хартията казват 8.75. Шест знака покриват реалната точност на
#: източника; излишните нули пак се крият (5 остава „5“, не „5.000000“).
_QUANTITY_NUMBER_FORMAT = "0.######"
#: Одит (04.10.2026, X3): „0.######“ показваше цяло число като „10.“ (Excel
#: винаги рисува десетичната точка, когато маската има такава), без разделител
#: за хиляди. Маската вече е ПО КЛЕТКА: точно толкова знака след запетаята,
#: колкото има въведеното (до 6 — същата точност като fmt_num на бланката),
#: цяло число — без точка: „#,##0“, „#,##0.0“, „#,##0.0875“ → „#,##0.0000“.
_QUANTITY_MAX_DECIMALS = 6
#: Одит (04.10.2026, R6): над 15 значещи цифри Excel вече не пази числото
#: точно (а над ~308 цифри float става inf и клетката излизаше ПРАЗНА) —
#: такава стойност остава текст, точно както е въведена.
_XLSX_MAX_EXACT = 1e15


def _quantity_number_format(text):
    """Маска за количество/тегло/обем по броя знаци след запетаята във
    въведения текст (виж _QUANTITY_MAX_DECIMALS)."""
    raw = re.sub(r"\s+", "", str(text or ""))
    decimals = 0
    m = re.search(r"[.,](\d+)$", raw)
    if m:
        # „1.20“ — въведената точност се пази (както fmt_num на бланката).
        decimals = min(len(m.group(1)), _QUANTITY_MAX_DECIMALS)
    return "#,##0" if decimals == 0 else "#,##0." + "0" * decimals


def _xlsx_number(value):
    """Одит (04.10.2026, R6): float за Excel клетка или None — само КРАЙНО,
    неотрицателно число, което Excel може да пази точно. `_parse_decimal`
    (appcore) връща inf за стойност с над 308 цифри; openpyxl записва inf
    като празна клетка, тоест числото изчезваше от износа без следа."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
    else:
        num = _parse_decimal(value)
    if num is None or not math.isfinite(num) or num < 0 or num >= _XLSX_MAX_EXACT:
        return None
    return num


def _xlsx_date(value):
    """ISO дата (или дата-час) → datetime.date; иначе None (свободен текст
    остава текст)."""
    text = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?$", text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _pdf_normalized_numbers(fields, items, cols, totals_row, doc_type):
    """Одит (31.08.2026, находка №8): копие на `fields`/`items`/`totals_row`,
    в което ЧИСЛОВИТЕ стойности минават през `fmt_num` (запетая→точка).

    Работи върху КОПИЯ — самите данни на документа не се пипат. Паричните
    полета (вече форматирани с „€“ от `_export_fields_and_items`) и датите
    се пропускат: те са текст по предназначение, а `fmt_num` така или иначе
    би върнал неразчетимата стойност непроменена."""
    money_keys = _MONEY_FIELDS.get(doc_type, ())
    date_keys = _DATE_FIELDS.get(doc_type, ())
    field_keys = [key for _label, key in _XLSX_FIELDS.get(doc_type, [])]

    out_fields = []
    for (label, value), key in zip(fields, field_keys):
        numeric = key in _NUMERIC_FIELD_KEYS and key not in money_keys and key not in date_keys
        out_fields.append((label, fmt_num(value) if numeric else value))

    numeric_col_keys = {key for key, _label in cols
                        if key in _NUMERIC_ITEM_COLUMN_KEYS}
    out_items = []
    for it in items:
        row = dict(it) if isinstance(it, dict) else {}
        for key in numeric_col_keys:
            if key in row:
                row[key] = fmt_num(row[key])
        out_items.append(row)

    out_totals = totals_row
    if totals_row is not None:
        # Одит (01.09.2026, доуточнение на находка №8, установено при
        # преглед на v3.69.0): редът на проверката е обърнат — `and` НЕ
        # пренарежда операндите, значи `cols[i][0]` се оценяваше ПРЕДИ
        # `i < len(cols)`. В момента е безобидно, защото
        # `_invoice_export_totals_row` винаги строи `totals_row` с точно
        # `len(cols)` елемента, но кодът е писан отбранително, сякаш това
        # НЕ е гарантирано — при по-дълъг `totals_row` в бъдеще би гърмяло с
        # `IndexError` точно вътре в защитата, вместо да прескочи елемента.
        out_totals = [
            fmt_num(v) if (i < len(cols) and cols[i][0] in numeric_col_keys) else v
            for i, v in enumerate(totals_row)
        ]
    return out_fields, out_items, out_totals

#: Одит (25.08.2026, находка №11): заглавни полета със стойност по
#: подразбиране при ПРАЗНА стойност — за да не се разминават износът и
#: бланката. Печатната бланка на фактурата показва „Currency: <b>{{ d.currency
#: or 'EURO' }}</b>“, а Excel/PDF износът пишеше суровото data.get('currency')
#: — за фактура без изрична валута (изчистено поле или стара фактура отпреди
#: полето) бланката казваше „EURO“, а таблицата в Excel — празно. Всички
#: суми са фиксирано в евро („Единична цена, EUR“, format_eur), затова
#: подразбиращата се валута е EURO навсякъде, единно.
#:
#: Одит (26.09.2026, находка №8): фактурите се издават САМО в евро (решение
#: на потребителя) — „Валута“ вече не е свободен текст. Стойността е
#: ФИКСИРАНА навсякъде (запис, бланка, Excel, PDF), независимо какво пише в
#: по-стари записи, иначе „Currency: USD“ стоеше до „Unit Price (EURO)“.
_FIELD_EXPORT_FIXED = {"currency": "EURO"}
INVOICE_CURRENCY = "EURO"


def _apply_fixed_fields(doc_type, data):
    """Налага фиксираните полета при запис/преглед (виж _FIELD_EXPORT_FIXED)."""
    if DOCUMENT_FLOWS[doc_type]["invoice_clients"]:
        data["currency"] = INVOICE_CURRENCY
    return data


def _normalize_invoice_items(doc_type, items):
    """Одит (04.10.2026, UX-4): кодът на материала във фактурите се записва
    с ГЛАВНИ букви (справочникът материали и печатът го ползват така —
    „mat-1“ и „MAT-1“ иначе изглеждат като два различни кода)."""
    if not DOCUMENT_FLOWS[doc_type]["invoice_clients"]:
        return items
    for it in items or []:
        if isinstance(it, dict) and isinstance(it.get("material_code"), str):
            it["material_code"] = it["material_code"].strip().upper()
    return items

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


def stamp_client_alias(con, doc_type, data):
    """Одит (04.10.2026, F10): записва псевдонима на клиента в данните на
    палетна карта при издаване/редакция — името на изнесения файл вече не се
    мени, ако клиентът по-късно бъде изтрит/преименуван. Публична, за да я
    ползва и груповото издаване (routes_pallet_extra)."""
    if doc_type != "pallet":
        return data
    alias = client_export.resolve_client_alias(con, data)
    if alias:
        data[client_export.STORED_ALIAS_KEY] = alias
    else:
        data.pop(client_export.STORED_ALIAS_KEY, None)
    return data


def _remember_client_alias(con, doc_id, alias):
    """Допълва псевдонима в записания документ (точково, с json_set — без да
    пипа останалите данни и версията; редакцията го пази, защото тръгва от
    записаните данни). Грешка тук не бива да проваля износа."""
    try:
        con.execute("UPDATE documents SET data = json_set(data, '$.%s', ?)"
                    " WHERE id = ? AND json_valid(data)" % client_export.STORED_ALIAS_KEY,  # nosec B608 -- константа
                    (alias, doc_id))
        con.commit()
    except sqlite3.Error:
        con.rollback()
        applog.log_exception("routes_documents: псевдонимът не е записан в документ id=%s" % doc_id)


def _export_filename(con, doc_type, row, data, ext):
    """Име на PDF/Excel файла при износ — заявка: „наименованието на файла
    палетната карта, която се запаметява като pdf или xlsx да е псевдонима
    на клиента [и] номер палетна карта, на английски език“. Само за
    палетни карти (единствения тип, за който бе поискано) и само когато
    клиентът има зададен псевдоним в адресната книга (client_export.
    resolve_client_alias) — иначе пада обратно към досегашния модел
    „<тип>_<номер>.<разширение>“, който важи за всички останали типове
    документи непроменено."""
    # В18: sanitize_number_stub заменя ВСИЧКИ Windows-забранени знаци
    # (не само „/“) — вижте client_export.sanitize_number_stub за
    # пълното обяснение (риск: ръчно въведен номер на фактура).
    number_stub = client_export.sanitize_number_stub(row["number"])
    if doc_type == "pallet":
        # Одит (04.10.2026, F10): от записаните данни на документа (виж
        # client_export.document_client_alias); документ отпреди поправката
        # го получава при първия износ (_remember_client_alias), за да не
        # зависи повече от адресната книга.
        alias = client_export.document_client_alias(con, data)
        if alias and not str(data.get(client_export.STORED_ALIAS_KEY) or "").strip():
            _remember_client_alias(con, row["id"], alias)
        if alias:
            stub = client_export.sanitize_filename_stub(alias)
            if stub:
                return "%s_%s.%s" % (stub, number_stub, ext)
    return "%s_%s.%s" % (doc_type, number_stub, ext)


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
        # Одит (25.08.2026, находка №11 / 26.09.2026, находка №8): валутата
        # е фиксирана — съвпада с печатната бланка и за стари записи.
        if key in _FIELD_EXPORT_FIXED:
            value = _FIELD_EXPORT_FIXED[key]
        if key in money_keys and value:
            value = format_eur_amount(value)
        elif key in date_keys and value:
            value = format_bg_date(value)
        fields.append((label, value))

    # Одит (29.08.2026, находка №3): втора защита за ВЕЧЕ ЗАПИСАНИ документи с
    # развален ред (записани преди филтъра в appcore.parse_items, или от
    # ръчна намеса в базата). Без нея износът на такъв документ падаше с
    # `AttributeError: 'str' object has no attribute 'get'` — Excel с необработен
    # 500, PDF с „PDF генерирането е неуспешно“ — и оставаше НЕВЪЗМОЖЕН
    # завинаги. Тази функция е общата точка на ДВАТА износа (виж
    # export_document_xlsx/export_document_pdf), затова филтърът тук покрива и
    # двата. Останалият код (сумите, шаблоните) вече пази isinstance(it, dict).
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
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


def _invoice_export_totals_row(doc_type, items, cols):
    """Одит (находка С2, среден риск): Excel/PDF износът на фактура
    показваше редовете артикули, но НЕ и обобщаващия ред TOTAL — за
    разлика от печатната бланка (invoice_dubai_print.html/
    invoice_br_print.html/invoice_no_print.html), която винаги го показва
    (виж appcore.invoice_totals). За търговска фактура, изпращана към
    счетоводство/митница, износ без общата сума е съществена липса —
    получателят трябва сам да сумира ръчно колоната.

    Връща списък стойности, подравнени 1:1 по `cols` (същия ред колони
    като редовете артикули, за да легне направо като поредния ред в
    таблицата), или None ако документният тип не е фактура, или няма
    редове/колони изобщо (нищо за сумиране — печатните бланки също не
    показват TOTAL ред без нито един артикул)."""
    if doc_type not in db.INVOICE_DOC_TYPES or not items or not cols:
        return None
    keys = [key for key, _label in cols]
    if "qty" not in keys and "__row_total__" not in keys:
        return None
    totals = invoice_totals(items)
    row = ["" for _ in cols]
    row[0] = "TOTAL"
    if "qty" in keys:
        row[keys.index("qty")] = totals["qty"]
    if "__row_total__" in keys:
        # Символ за евро на общата сума — СЪЩОТО поведение като на
        # печатната бланка (виж invoice_br_print.html/
        # invoice_dubai_print.html: "{{ (t.price ~ ' €') if t.price else '—' }}").
        row[keys.index("__row_total__")] = ("%s €" % totals["price"]) if totals["price"] else ""
    # Одит (19.08.2026, находка №32): и общото ТЕГЛО. Колоната „Общо тегло,
    # кг“ вече се пълни по редовете, `invoice_totals` вече го е сметнало,
    # но клетката в реда TOTAL оставаше празна — получателят (счетоводство/
    # митница) трябваше да сумира на ръка точно колоната, заради която
    # редът TOTAL изобщо беше добавен (находка С2).
    if "__row_weight__" in keys:
        row[keys.index("__row_weight__")] = totals["weight"]
    return row


def _export_totals_row(doc_type, data, items, cols):
    """Одит (04.10.2026, X5): обобщаващият ред под таблицата за ВСИЧКИ типове,
    при които бланката има такъв — фактурите (_invoice_export_totals_row),
    опаковъчният лист („ОБЩО / TOTAL · N колета“ + общо обем/нето/бруто,
    точно както packing_print.html — ВЪВЕДЕНИТЕ обобщения, не преизчислени)
    и палетната карта (общото количество, същото като полето „Общ брой“).
    None за останалите или без редове."""
    if doc_type in db.INVOICE_DOC_TYPES:
        return _invoice_export_totals_row(doc_type, items, cols)
    if not items or not cols:
        return None
    keys = [key for key, _label in cols]
    row = ["" for _ in cols]
    row[0] = "ОБЩО / TOTAL"
    if doc_type == "packing":
        packages = str(data.get("total_packages") or "").strip()
        if packages and len(keys) > 1:
            row[1] = "%s колета/packages" % fmt_num(packages)
        # Общото количество — винаги сборът на редовете; обем/нето/бруто —
        # въведеното, а при празно — сборът (както packing_print.html, F9).
        if "qty" in keys:
            row[keys.index("qty")] = packing_sum(items, "qty")
        for total_key, col_key in (("total_volume", "volume"), ("total_net", "net"),
                                   ("total_gross", "gross")):
            if col_key in keys:
                typed = str(data.get(total_key) or "").strip()
                row[keys.index(col_key)] = typed or packing_sum(items, col_key)
        return row
    if doc_type == "pallet":
        if "qty" in keys:
            row[keys.index("qty")] = pallet_total_qty(items)
        return row
    return None


def _append_xlsx_item_row(ws, values, cols):
    """Одит (16.08.2026, находка №19): добавя РЕД от items/totals_row към
    работния лист — за колони от _NUMERIC_ITEM_COLUMN_KEYS ЗАПИСВА РЕАЛНО
    ЧИСЛО (float) с number_format вместо суровия текст, ако стойността
    изобщо се разпознава като число (appcore._parse_decimal — същата
    строга валидация като навсякъде другаде в проекта). Неразпознаваема/
    празна стойност пада обратно към стария текстов запис — без загуба,
    само без числово форматиране за тази конкретна клетка."""
    row_values = list(values)
    numeric_cols = []
    for c, (key, _label) in enumerate(cols, start=1):
        if key not in _NUMERIC_ITEM_COLUMN_KEYS:
            continue
        idx = c - 1
        if idx >= len(row_values):
            continue
        raw = row_values[idx]
        # Одит (04.10.2026, X3): общата сума в евро на реда TOTAL („123.45 €“)
        # вече е ЧИСЛО с формат „€“, не текст.
        euro = (key == "__row_total__" and isinstance(raw, str) and raw.strip().endswith("€"))
        text = raw.strip()[:-1].strip() if euro else raw
        # Одит (25.08.2026, находка №12): отрицателна стойност НЕ се записва
        # като истинско число — всички суми в проекта (invoice_totals,
        # pallet_total_qty, packing_sum) я изключват (находка С1), значи и
        # =SUM() по колоната не бива да я брои; остава видима като текст.
        # Одит (04.10.2026, R6): същото за безкрайност/над 15 цифри (_xlsx_number).
        num = _xlsx_number(text)
        if num is not None:
            row_values[idx] = num
            numeric_cols.append((c, key, text, euro))
    row = _xlsx_append(ws, row_values)
    for c, key, text, euro in numeric_cols:
        # Одит (31.08.2026, находка №10 / 01.09.2026, №9): парите — собствена
        # маска по ключ (_MONEY_ITEM_COLUMN_FORMATS); количествата — по
        # въведената точност (_quantity_number_format, одит 04.10.2026, X3).
        if euro:
            fmt = _EUR_NUMBER_FORMAT
        else:
            fmt = _MONEY_ITEM_COLUMN_FORMATS.get(key) or _quantity_number_format(text)
        ws.cell(row=row, column=c).number_format = fmt
    return row


#: Одит (19.08.2026, информативна находка): твърдият таван на .xlsx за
#: дължина на текстова клетка (32 767 знака) и видимият маркер, който
#: слагаме на мястото на отрязаното — виж _xlsx_safe_value.
_XLSX_MAX_CELL_LEN = 32767
_XLSX_TRUNCATED_MARK = " […ТЕКСТЪТ Е ОТРЯЗАН ПРИ ИЗНОСА — над 32767 знака]"


_XML_NONCHARS_RE = re.compile("[\ufffe\uffff\ud800-\udfff]")


def _xlsx_safe_value(value):
    """Одит (находка В2, висок риск): openpyxl хвърля некоригируем
    ``IllegalCharacterError`` при опит да запише низ, съдържащ т.нар.
    "control characters" (напр. вертикален таб \\x0b — точно това вмъква
    Word при "Shift+Enter"/"мек нов ред", ако потребител копира текст
    оттам в свободно текстово поле като бележки/адрес). Грешката гърми
    ПРИ САМОТО ЗАПИСВАНЕ (buf.getvalue()/wb.save по-долу), т.е. целият
    износ пада с 500 — практически неоткриваем за потребителя проблем,
    защото самите полета изглеждат съвсем нормално в интерфейса.

    Тук изчистваме забранените контролни символи (същият регулярен израз,
    който openpyxl вътрешно ползва, за да open ги открие) ПРЕДИ да ги
    подадем на клетката — вместо да гърми, износът просто показва текста
    без невидимите символи.

    Одит (19.08.2026, информативна находка): низ над 32 767 знака (таванът
    на самия формат .xlsx) openpyxl реже МЪЛЧАЛИВО още при присвояването на
    стойността — проверено: 40 000 знака влизат в клетката като 32 767, без
    изключение и без предупреждение. Такъв низ е напълно достижим през
    Excel импорт (`/pallet/bulk-import`, `/invoice/import-items`,
    `/materials/import` приемат клетки с такъв размер) и после отива в
    изнесения файл при клиента/счетоводството НЕПЪЛЕН, без никой да
    забележи. Сега рязането е ЯВНО: остава видим маркер в самата клетка
    (получателят вижда, че текстът е отрязан, вместо да мисли, че това е
    всичко) и се записва ред в лога за диагностика."""
    if isinstance(value, str) and ILLEGAL_CHARACTERS_RE.search(value):
        value = ILLEGAL_CHARACTERS_RE.sub(" ", value)
    # Одит (26.09.2026, находка №27): lxml отказва и XML „не-символите“
    # U+FFFE/U+FFFF и самотните surrogate-и („All strings must be XML
    # compatible“) — целият износ падаше, макар печатът и PDF да работят.
    if isinstance(value, str) and _XML_NONCHARS_RE.search(value):
        value = _XML_NONCHARS_RE.sub(" ", value)
    if isinstance(value, str) and len(value) > _XLSX_MAX_CELL_LEN:
        applog.log_warning(
            "routes_documents._xlsx_safe_value",
            "текст от %d знака е отрязан до %d при износа в Excel (ограничение "
            "на самия .xlsx формат) — първите знаци: %r"
            % (len(value), _XLSX_MAX_CELL_LEN, value[:60]))
        return value[:_XLSX_MAX_CELL_LEN - len(_XLSX_TRUNCATED_MARK)] + _XLSX_TRUNCATED_MARK
    return value


def _xlsx_safe_row(values):
    return [_xlsx_safe_value(v) for v in values]


# Одит (19.08.2026, находка №1, КРИТИЧНА): водещи символи, които Excel
# тълкува като начало на ФОРМУЛА, а не като текст. `=` е реалният вектор
# при .xlsx (потвърдено: openpyxl записва такава клетка с data_type='f',
# т.е. истинска формула); `+`, `-`, `@`, табулация и CR се добавят като
# защита в дълбочина — същият низ, отворен като CSV или в друга програма
# за електронни таблици, се изпълнява и с тях.
_XLSX_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _xlsx_append(ws, values):
    """Одит (19.08.2026, находка №1, КРИТИЧНА — инжекция на формули):
    ЕДИНСТВЕНАТА точка, през която този модул добавя ред в изнесен
    .xlsx файл. Освен вече съществуващото чистене на контролни символи
    (`_xlsx_safe_value`, находка В2), тук неутрализираме и клетки,
    започващи със символ, който Excel тълкува като ФОРМУЛА.

    Защо е критично: свободните текстови полета се пълнят и от Excel
    ИМПОРТ (поръчки от доставчик, редове на фактура, справочник
    материали) — тоест съдържанието може да идва отвън. Преди тази
    поправка низ като `=cmd|'/c calc'!A0` (DDE) или
    `=HYPERLINK("http://…"&A1,"виж")` (изнася съседните клетки към чужд
    сървър) се записваше като ИСТИНСКА формула и се изпълняваше на
    машината на ПОЛУЧАТЕЛЯ — счетоводството, клиента или митническия
    агент, отворил файла, който сме им изпратили. Същите байтове се
    копират и в клиентските папки на споделения диск
    (`client_export.save_client_export_copy`).

    Поправката НЕ променя видимото съдържание: `quotePrefix` е точно
    механизмът, който Excel ползва за „това е текст, не формула“ —
    апострофът не се показва в клетката и не влиза в стойността при
    копиране. Задаваме и `data_type = "s"`, защото openpyxl определя
    типа при присвояването на стойността (преди да стигнем дотук).

    Връща номера на записания ред."""
    safe = _xlsx_safe_row(values)
    ws.append(safe)
    # Одит (01.10.2026, F3): ws.max_row/ws[ред] обхождат ВСИЧКИ клетки при
    # всяко извикване (квадратично при стотици редове); _current_row е редът,
    # който самият append току-що е записал.
    row = ws._current_row
    for col, value in enumerate(safe, start=1):
        if isinstance(value, str) and value.startswith(_XLSX_FORMULA_PREFIXES):
            cell = ws.cell(row=row, column=col)
            cell.data_type = "s"
            cell.quotePrefix = True
    return row


def _warn_if_client_copy_failed(status):
    """Одит (19.08.2026, находка №26, средна): провален запис на копие в
    клиентската папка беше НЕВИДИМ за оператора — върнатата стойност се
    игнорираше и на двете места, а единствената следа беше ред в лог файла,
    който потребител на .exe никога не отваря. Свалянето през браузъра при
    това УСПЯВА, така че операторът остава убеден, че копието е и на общия
    диск (докато мрежовият път е бил недостъпен, дискът пълен или името на
    папката отказано от Windows).

    Самото сваляне НЕ се пипа — flash съобщението се показва при следващото
    зареждане на страница, точно както всички останали известия (отговорът
    тук е файл, не HTML страница, така че по-рано няма как)."""
    if status == client_export.EXPORT_FAILED:
        flash(_("Файлът се свали успешно, но копието в клиентската папка НЕ беше "
                "записано (недостъпна папка, пълен диск или отказано от системата "
                "име). Проверете настройката „Папка за клиентски копия“ в "
                "системните настройки."), "warning")


#: Одит (04.10.2026, X4): над толкова колони в таблицата с редовете листът
#: се отпечатва хоризонтално.
_XLSX_LANDSCAPE_MIN_COLS = 7
#: Одит (04.10.2026, X4): заглавният ред на таблицата се замразява само ако
#: над него няма повече от толкова реда — иначе замразената част заема
#: целия екран и нищо не се превърта (заглавните полета са 15–35 реда).
_XLSX_FREEZE_MAX_ROW = 12
_XLSX_MAX_COL_WIDTH = 50


def _xlsx_styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    thin = Side(style="thin", color="999999")
    return {
        "bold": Font(bold=True),
        "title": Font(bold=True, size=14),
        "border": Border(left=thin, right=thin, top=thin, bottom=thin),
        "head_fill": PatternFill("solid", fgColor="E6E6E6"),
        "label_fill": PatternFill("solid", fgColor="F2F2F2"),
        "wrap": Alignment(wrap_text=True, vertical="top"),
        "top": Alignment(vertical="top"),
    }


def _xlsx_field_cell_value(doc_type, key, value, data):
    """(стойност, number_format) за клетката на заглавно поле.

    Одит (19.08.2026, находка №31): числата — истински числа. Одит
    (04.10.2026, X3): и паричните полета (число с „€“ формат вместо текста
    „450.00 €“), и датите (истинска дата, ДД.ММ.ГГГГ), и маската на числата
    по въведената точност, с разделител за хиляди."""
    if key in _MONEY_FIELDS.get(doc_type, ()):
        num = _xlsx_number(data.get(key))
        if num is not None:
            return num, _EUR_NUMBER_FORMAT
        return value, None
    if key in _DATE_FIELDS.get(doc_type, ()):
        day = _xlsx_date(data.get(key))
        if day is not None:
            return day, _DATE_NUMBER_FORMAT
        return value, None
    if key in _NUMERIC_FIELD_KEYS:
        # Одит (25.08.2026, находка №12): отрицателните остават текст — същото
        # като редовете и всички суми в проекта.
        num = _xlsx_number(value)
        if num is not None:
            return num, _quantity_number_format(value)
    return value, None


@login_required
def export_document_xlsx(doc_id):
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    con = get_db()
    row, data = fetch_document(con, doc_id)
    doc_type = row["doc_type"]
    title = db.DOC_TYPES.get(doc_type, {}).get("title", doc_type)
    fields, items, cols = _export_fields_and_items(doc_type, data)
    st = _xlsx_styles()

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31] or "Документ"

    _xlsx_append(ws, ["%s № %s" % (title, row["number"])])
    ws.cell(row=1, column=1).font = st["title"]
    _xlsx_append(ws, ["Баркод", row["barcode"]])
    ws.cell(row=2, column=1).font = st["bold"]
    ws.append([])

    # Одит (19.08.2026, находка №31): `fields` се строи 1:1 от
    # _XLSX_FIELDS[doc_type], затова ключът се възстановява с zip.
    field_keys = [key for _label, key in _XLSX_FIELDS.get(doc_type, [])]
    for (label, value), key in zip(fields, field_keys):
        cell_value, number_format = _xlsx_field_cell_value(doc_type, key, value, data)
        field_row = _xlsx_append(ws, [label, cell_value])
        label_cell = ws.cell(row=field_row, column=1)
        value_cell = ws.cell(row=field_row, column=2)
        label_cell.font = st["bold"]
        label_cell.fill = st["label_fill"]
        label_cell.alignment = st["top"]
        # Одит (04.10.2026, X4): рамки и пренос на дългия текст (адреси,
        # бележки) вместо безкрайно дълга клетка.
        label_cell.border = value_cell.border = st["border"]
        value_cell.alignment = st["wrap"] if isinstance(cell_value, str) else st["top"]
        if number_format:
            value_cell.number_format = number_format

    header_row = None
    if items and cols:
        ws.append([])
        # Одит (01.10.2026, F3): `ws.max_row + 1` сочеше ПРАЗНИЯ ред (append([])
        # не създава клетки) — удебеляваше се той, а не заглавният ред.
        header_row = _xlsx_append(ws, [label for _key, label in cols])
        for c in range(1, len(cols) + 1):
            cell = ws.cell(row=header_row, column=c)
            cell.font = st["bold"]
            cell.fill = st["head_fill"]
            cell.border = st["border"]
            cell.alignment = st["wrap"]
        last = header_row
        for it in items:
            last = _append_xlsx_item_row(ws, [it.get(key, "") for key, _label in cols], cols)
            for c, (key, _label) in enumerate(cols, start=1):
                cell = ws.cell(row=last, column=c)
                cell.border = st["border"]
                cell.alignment = st["wrap"] if isinstance(cell.value, str) else st["top"]
        totals_row = _export_totals_row(doc_type, data, items, cols)
        if totals_row is not None:
            last = _append_xlsx_item_row(ws, totals_row, cols)
            for c in range(1, len(cols) + 1):
                cell = ws.cell(row=last, column=c)
                cell.font = st["bold"]
                cell.fill = st["label_fill"]
                cell.border = st["border"]
        # Одит (04.10.2026, X4): заглавният ред се повтаря на всеки печатен
        # лист, а таблицата има филтър; замразяване — виж _XLSX_FREEZE_MAX_ROW.
        ws.print_title_rows = "%d:%d" % (header_row, header_row)
        ws.auto_filter.ref = "A%d:%s%d" % (header_row, get_column_letter(len(cols)), last)
        if header_row <= _XLSX_FREEZE_MAX_ROW:
            ws.freeze_panes = "A%d" % (header_row + 1)

    # Ширини: по най-дългия РЕД на клетката (не по целия текст с новите
    # редове), без заглавието в A1, което и без това прелива надясно.
    for col_cells in ws.columns:
        lengths = []
        for c in col_cells:
            if c.value is None or c.row == 1:
                continue
            if isinstance(c.value, (date, datetime)):
                lengths.append(10)
            elif isinstance(c.value, float):
                lengths.append(len("{:,.2f}".format(c.value)) + 2)
            else:
                lengths.append(max(len(part) for part in str(c.value).split("\n")))
        width = max(lengths) + 2 if lengths else 10
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(width, 10),
                                                                     _XLSX_MAX_COL_WIDTH)

    # Одит (04.10.2026, X4): настройки за печат — A4, по ширината на един
    # лист (височината — колкото трябва), хоризонтално при широка таблица.
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = ("landscape" if len(cols) >= _XLSX_LANDSCAPE_MIN_COLS
                                 else "portrait")
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.5

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = _export_filename(con, doc_type, row, data, "xlsx")

    # Клиентски папки (виж client_export.py) — best-effort копие в
    # <базова папка>/<клиент>/, ако е включено в системните настройки.
    # НЕ бива да провали свалянето на файла за потребителя при грешка.
    _warn_if_client_copy_failed(
        client_export.save_client_export_status(db.get_settings(con), doc_type, data,
                                                filename, buf.getvalue()))

    return send_file(buf, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet")


#: Одит (07.10.2026): сигналът „PDF-ът е готов“ (виж края на
#: export_document_pdf и static/app.js); до v3.79 — pacho_pdf_ready.
PDF_READY_COOKIE = "ph_pdf_ready"
LEGACY_PDF_READY_COOKIE = "pacho_pdf_ready"


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
    # Одит (04.10.2026, X5): и редът ОБЩО/TOTAL на опаковъчния лист и палетната карта.
    totals_row = _export_totals_row(doc_type, data, items, cols)
    # Одит (31.08.2026, находка №8, средна): PDF износът вече показва
    # числата с ТОЧКА за десетичен знак, както печатната бланка и Excel.
    #
    # Поправката на находка №4 от седмия одит приложи fmt_num на всички
    # ПЕЧАТНИ шаблони, но пропусна pdf_export.html — там стоеше суровият
    # `{{ value }}` / `{{ it.get(key, "") }}`. Резултат за ЕДИН И СЪЩ
    # документ в три носителя: PDF показваше „2,5 / 0,144 / 1,25“, бланката
    # „2.5 / 0.144 / 1.25“, а Excel клетките числово 2.5 / 0.144 / 1.25.
    # Нормализацията става ТУК (а не в шаблона), защото точно този модул
    # знае кои ключове са числови — шаблонът е генеричен за 9 типа документи.
    fields, items, totals_row = _pdf_normalized_numbers(
        fields, items, cols, totals_row, doc_type)

    try:
        pdf_bytes = pdf_export.generate_document_pdf(
            title, row["number"], row["barcode"], fields, items, cols,
            totals_row=totals_row)
    except pdf_export.PdfBusyError:
        # Одит (25.08.2026, находка №5): „заета опашка“ е ВРЕМЕННО състояние,
        # не срив — спокойно съобщение „изчакайте и опитайте пак“, без да
        # тревожим оператора да „съобщи на администратор“ и без да е логнато
        # като грешка (PdfBusyError не минава през log_exception).
        flash(_("Точно сега се генерират други PDF файлове. Изчакайте няколко "
                "секунди и опитайте пак."), "warning")
        return redirect(url_for("view_document", doc_id=doc_id))
    except RuntimeError:
        # generate_document_pdf вече логна пълния traceback (applog, вижте
        # там) — тук потребителят вижда ясно съобщение и се връща към
        # документа вместо суров "Internal Server Error" на бял екран,
        # без никакво обяснение какво е станало или какво да опита.
        flash(_("PDF файлът не можа да се генерира за този документ. "
                "Опитайте пак — ако продължава, съобщете на администратор."),
              "error")
        return redirect(url_for("view_document", doc_id=doc_id))
    filename = _export_filename(con, doc_type, row, data, "pdf")

    # Клиентски папки (виж client_export.py) — best-effort копие, СЪЩИЯТ
    # механизъм като при Excel износа по-горе (заявка: "И двете" — важи за
    # ВСИЧКИ износи, не само Excel).
    _warn_if_client_copy_failed(
        client_export.save_client_export_status(db.get_settings(con), doc_type, data,
                                                filename, pdf_bytes))

    resp = send_file(io.BytesIO(pdf_bytes), as_attachment=True, download_name=filename,
                     mimetype="application/pdf")
    # Одит (22.09.2026, находка №2): сигнал към браузъра, че ТОЧНО тази
    # заявка за PDF е готова.
    #
    # Изтеглянето е обикновен <a href> — няма събитие „готово“, на което JS
    # да се закачи (страницата не се презарежда). Затова връщаме кратко
    # живеещо бисквитче с токена, подаден от клиента (?dl=<токен>): щом то
    # се появи, индикаторът „Подготвя се…“ върху бутона се маха. Измерено
    # при опаковъчен лист: 600 реда = ~13.7 сек — без индикатор операторът
    # не вижда НИЩО да се случва и натиска бутона пак, а всяко натискане
    # застава на опашката зад предишното (PDF рендирането е сериализирано
    # с _render_lock, виж pdf_export.py) и удължава собственото си чакане.
    #
    # Токенът идва от клиента и се връща само на него, в бисквитче за
    # текущата сесия, без да влиза в базата или в лога — затова се
    # ограничава до безобиден кратък низ (по-долу), за да не може да се
    # вгради нищо чуждо в заглавната част на отговора.
    token = (request.args.get("dl") or "")[:32]
    if token and token.isalnum():
        resp.set_cookie(PDF_READY_COOKIE, token, max_age=120, samesite="Lax",
                        secure=request.is_secure)
    # Одит (07.10.2026): бисквитката до v3.79 — изчиства се, ако е останала.
    if LEGACY_PDF_READY_COOKIE in request.cookies:
        resp.delete_cookie(LEGACY_PDF_READY_COOKIE, samesite="Lax",
                           secure=request.is_secure)
    return resp


@admin_required
def delete_document(doc_id):
    con = get_db()
    row = con.execute("SELECT doc_type, number FROM documents WHERE id = ?", (doc_id,)).fetchone()
    # Одит (12.08.2026, находка №22): преди тази поправка изтриване на
    # НЕСЪЩЕСТВУВАЩ/вече изтрит (напр. двоен клик, стар отворен таб) ID
    # показваше подвеждащото "Документът е изтрит" — все едно наистина е
    # свършило нещо. DELETE FROM ... WHERE id=? за несъществуващ ред е
    # no-op (0 засегнати реда), затова проверката е нужна изрично.
    if row is None:
        abort(404)
    con.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    con.commit()
    # Одит (находка С9): ON DELETE CASCADE изчиства document_attachments,
    # но не пипа файловете на диска — трябва изрично да ги изтрием, иначе
    # остават осиротели (виж attachments.delete_all_attachments_dir).
    attachments.delete_all_attachments_dir(doc_id)
    applog.log_audit("изтрит документ",
                     "id=%s %s №%s" % (doc_id, row["doc_type"], row["number"]))  # находка №51
    flash(_("%(title)s № %(number)s е изтрит(а).")
          % {"title": _(db.DOC_TYPES.get(row["doc_type"], {}).get("title", row["doc_type"])),
             "number": row["number"]}, "success")
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

_SENDER_LANG_FIELDS = ("sender_name", "sender_address", "sender_postcode", "sender_city",
                       "sender_country")


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


#: Одит (04.10.2026, F2): скрито поле/отметка, с което операторът потвърждава
#: изрично, че номерът наистина трябва да съвпада с фактура от друг тип
#: (стойността е самият номер). Не се записва в документа.
CONFIRM_NUMBER_REUSE_FIELD = "confirm_number_reuse"
_CONFIRM_SESSION_KEY = "invoice_number_reuse_confirm"


def _number_reuse_confirmed(doc_type, number, year, form_value):
    """Одит (04.10.2026, F2): дали операторът е потвърдил повторното ползване
    на номера от фактура от ДРУГ тип — или с отметката (поле
    CONFIRM_NUMBER_REUSE_FIELD със същия номер), или като изпрати формата
    ВТОРИ път със същия номер след съобщението за блокиране (запомнено в
    сесията само за тази комбинация тип/година/номер)."""
    if str(form_value or "").strip() == number:
        return True
    return session.get(_CONFIRM_SESSION_KEY) == [doc_type, year, number]


def _number_taken_by_same_type(con, doc_type, number, year=None,
                               exclude_doc_id=None, confirmed_value=None,
                               action_label=None):
    """Проверка на ръчно въведен номер на фактура.

    Връща True (и показва ЕДНА грешка), ако номерът вече е зает от същия тип
    през същата година — уникалният индекс (doc_type, year, number) от
    `_m005` така или иначе би отказал записа. Одит (01.10.2026, U5): досега
    тук излизаше предупреждение, а после и грешката от IntegrityError — две
    съобщения за една грешка.

    Одит (04.10.2026, F2): номер, носен от фактура от ДРУГ тип (напр. Дубай
    0000012957 → нова Бразилия със същия номер), вече БЛОКИРА, докато
    операторът не потвърди изрично (виж _number_reuse_confirmed). Досега
    само предупреждаваше — СЛЕД издаването, когато вече е късно, а
    предложеният номер (_suggest_invoice_number) сам водеше точно дотам.

    Годината е тази на самия документ при редакция (находка №20 от
    31.08.2026), а `exclude_doc_id` изключва самия редактиран документ."""
    if not number:
        return False
    if year is None:
        year = date.today().year
    types = tuple(db.INVOICE_DOC_TYPES) + (doc_type,)
    sql = ("SELECT DISTINCT doc_type FROM documents WHERE year = ? AND number = ?"
           " AND doc_type IN (%s)" % ",".join("?" for _t in types))  # nosec B608 -- само „?“ плейсхолдъри
    params = [year, number] + list(types)
    if exclude_doc_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_doc_id)
    used = {r["doc_type"] for r in con.execute(sql, params)}
    if doc_type in used:
        flash(_("Не е записано: вече има издаден документ с номер %(number)s през "
                "%(year)s г. — номерът вече е зает. Въведеното е запазено — "
                "променете номера и опитайте пак.")
              % {"number": number, "year": year}, "error")
        return True
    titles = [db.DOC_TYPES[t]["title"] for t in db.INVOICE_DOC_TYPES if t in used]
    others = [_(title) for title in titles]
    if not others:
        return False
    if _number_reuse_confirmed(doc_type, number, year, confirmed_value):
        session.pop(_CONFIRM_SESSION_KEY, None)
        flash(_("Внимание: номер %(number)s вече е използван през %(year)s г. за "
                "%(types)s. Записано е след Вашето потвърждение.")
              % {"number": number, "year": year, "types": ", ".join(others)}, "warning")
        return False
    session[_CONFIRM_SESSION_KEY] = [doc_type, year, number]
    flash(_("Не е записано: номер %(number)s вече е използван през %(year)s г. за "
            "%(types)s. Въведеното е запазено. Проверете номера — ако наистина е "
            "верен, натиснете „%(action)s“ още веднъж, за да потвърдите.")
          % {"number": number, "year": year, "types": ", ".join(others),
             "action": action_label or _("Издай")}, "error")
    return True


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


def _warn_if_negative_values(items):
    """Одит (12.08.2026, находка №3): вижте appcore.negative_item_rows —
    отрицателно количество/цена/тегло на ред се показва СУРОВО на
    бланката, но мълчаливо изчезва от изчислените суми под таблицата.
    Предупреждава (без да блокира — оператор с легитимна причина, напр.
    сторниращ ред, все пак може да продължи), за да забележи проблема
    ПРЕДИ да раздаде/изпрати документа."""
    rows = negative_item_rows(items)
    if rows:
        flash(_("Внимание: ред(ове) №%(rows)s съдържат отрицателно количество/цена/"
                "тегло. Такъв ред НЕ участва в сборовете под таблицата, но стойността "
                "му се вижда суровa на бланката — проверете дали не е печатна грешка.")
              % {"rows": ", ".join(str(r) for r in rows)}, "warning")
    # Одит (19.08.2026, находка №7, висока): вторият, по-коварен начин ред
    # да изчезне от сборовете — попълнена, но НЕразчитаема стойност (напр.
    # „1.234,56“ с разделител за хиляди). Виж appcore.unparsable_item_rows.
    unparsable = unparsable_item_rows(items)
    if unparsable:
        flash(_("Внимание: ред(ове) №%(rows)s съдържат количество/цена/тегло, което "
                "не може да бъде разчетено като число (допустими са само цифри с "
                "точка или запетая, напр. 1234.56). Такъв ред се ВИЖДА на бланката, "
                "но НЕ участва в общата сума — проверете стойностите.")
              % {"rows": ", ".join(str(r) for r in unparsable)}, "warning")


def _warn_if_suspicious_header_numbers(doc_type, data):
    """Одит (03.09.2026, находка №6): същите две проверки като за редовете,
    но върху ЗАГЛАВНИТЕ числови полета — виж appcore.
    suspicious_header_numbers. Важи за ВСИЧКИ типове, включително тези без
    редове (ЧМР), където досега нищо не проверяваше кутии 11 и 12."""
    # Одит (04.10.2026, I5): етикетите — на езика на интерфейса.
    labels = {key: _label(label) for label, key in _XLSX_FIELDS.get(doc_type, [])}
    money_keys = _MONEY_FIELDS.get(doc_type, ())
    keys = [key for key in labels if key in _NUMERIC_FIELD_KEYS and key not in money_keys]
    negative, unparsable = suspicious_header_numbers(data, keys, labels)
    if negative:
        flash(_("Внимание: полето/полетата %(fields)s съдържат отрицателна "
                "стойност. Тя се печата суровa на бланката и НЕ участва в "
                "сборовете — проверете дали не е печатна грешка.")
              % {"fields": ", ".join("„%s“" % f for f in negative)}, "warning")
    if unparsable:
        flash(_("Внимание: полето/полетата %(fields)s съдържат стойност, която "
                "не може да бъде разчетена като число (допустими са само цифри "
                "с точка или запетая, напр. 1234.56). Тя се печата буквално на "
                "бланката, а в Excel износа влиза като текст, не като число.")
              % {"fields": ", ".join("„%s“" % f for f in unparsable)}, "warning")


def _warn_if_packing_totals_mismatch(data):
    """Одит (19.08.2026, находка №8, висока): вижте
    appcore.packing_total_mismatches — четирите обобщаващи полета на
    опаковъчния лист се преписват на ръка и се печатат буквално в реда
    ОБЩО/TOTAL, без никаква проверка срещу сбора на редовете. Само
    предупреждава (общото легитимно може да включва тара)."""
    for label, typed, computed in packing_total_mismatches(data):
        flash(_("Внимание: „%(label)s“ е въведено %(typed)s, а сборът на редовете "
                "дава %(computed)s. Проверете дали не е печатна грешка — на "
                "бланката се отпечатва въведената стойност.")
              % {"label": _label(label), "typed": typed, "computed": computed}, "warning")


def _has_invoice_item(items):
    """Поне един ред с попълнено нещо освен подразбиращия се HS код."""
    for it in items or []:
        if isinstance(it, dict) and any(
                str(v or "").strip() for k, v in it.items() if k != "hs_code"):
            return True
    return False


_TRAILING_DIGITS_RE = re.compile(r"(\d+)(\D*)$")


def _suggest_invoice_number(con, doc_type, year=None):
    """Одит (01.10.2026, P4): предложение за следващ ръчен номер на фактура —
    най-големият номер + 1, със същия вид („2026-0042“ → „2026-0043“).
    Автоматичните вътрешни номера („0001/2026“) не се броят. Само стойност по
    подразбиране — операторът може да я смени.

    Одит (04.10.2026, F2): по ВСИЧКИ типове фактури, не само по този —
    номерацията на фактурите е обща. Досега след Дубай 0000012957 новата
    фактура за Бразилия/Норвегия предлагаше пак 0000012957 (последния
    СОБСТВЕН номер + 1) и се издаваше без спиране."""
    if year is None:
        year = date.today().year
    auto_re = re.compile(r"^\d+/%d$" % year)
    best = None
    taken = set()
    types = tuple(db.INVOICE_DOC_TYPES) + ((doc_type,) if doc_type not in db.INVOICE_DOC_TYPES else ())
    for r in con.execute("SELECT number FROM documents WHERE year = ? AND doc_type IN (%s)"
                         % ",".join("?" for _t in types),  # nosec B608 -- само „?“ плейсхолдъри
                         (year,) + types):
        number = (r["number"] or "").strip()
        taken.add(number)
        m = _TRAILING_DIGITS_RE.search(number)
        if not m or auto_re.match(number) or len(m.group(1)) > 18:
            continue
        if best is None or int(m.group(1)) > int(best.group(1)):
            best = m
    if best is None:
        return ""
    head, digits, tail = best.string[:best.start(1)], best.group(1), best.group(2)
    value = int(digits)
    for _attempt in range(100):
        value += 1
        candidate = "%s%0*d%s" % (head, len(digits), value, tail)
        if candidate not in taken:
            return candidate
    return ""


#: Одит (04.10.2026, R5): задължителните полета на формите (атрибутът
#: `required` в шаблоните *_form.html) — проверени и на сървъра. Празен POST
#: (изключен JavaScript, стар таб, скрипт, „Издай“ от преглед на непълна
#: форма) издаваше НОМЕРИРАН документ без получател за всичките шест типа.
#: Етикетът идва от _XLSX_FIELDS (същият текст като в износа/предупрежденията).
#: Одит (04.10.2026, UX-Б4): декларацията за двойна употреба изисква и
#: държавата на износ и декларатора — бланката иначе гласи „износ за ,“.
_REQUIRED_FIELDS = {
    "cmr": ("consignee_name",),
    "packing": ("receiver_name",),
    "pallet": ("client_name",),
    "waybill": ("consignee_name",),
    "dualuse": ("invoice_numbers", "destination_country", "declarant_name"),
    "export_it": ("invoice_no",),
}


def _apply_required_defaults(con, doc_type, data):
    """Одит (04.10.2026, UX-Б4): празен декларатор на декларацията за двойна
    употреба се попълва от „Лице за контакт“ в Настройки (sender_person) —
    същата стойност, с която формата го предлага."""
    if doc_type == "dualuse" and not str(data.get("declarant_name") or "").strip():
        person = str(db.get_settings(con).get("sender_person") or "").strip()
        if person:
            data["declarant_name"] = person


def _missing_required_fields(doc_type, data):
    """Етикетите (преведени) на празните задължителни полета."""
    labels = {key: label for label, key in _XLSX_FIELDS.get(doc_type, [])}
    return [_label(labels.get(key, key)) for key in _REQUIRED_FIELDS.get(doc_type, ())
            if not str(data.get(key) or "").strip()]


def _is_disk_full_error(exc):
    """Одит (04.10.2026, R7): пълен диск — SQLITE_FULL („database or disk is
    full“) или ENOSPC от файловата система."""
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return True
    if isinstance(exc, sqlite3.Error):
        if getattr(exc, "sqlite_errorcode", None) == getattr(sqlite3, "SQLITE_FULL", 13):
            return True
        return "disk is full" in str(exc).lower()
    return False


def _issue_new_document(con, doc_type, data):
    """Издава НОВ документ от вече събраните данни на формата (POST от
    формата или „Издай“ от предварителния преглед) — едни и същи проверки и
    предупреждения и в двата пътя. При грешка въведеното се пази през
    ?restore= към формата за същия тип."""
    flow = DOCUMENT_FLOWS[doc_type]
    form_url = url_for(doc_type + "_new")
    # Одит (04.10.2026, F2): потвърждението не е част от документа.
    confirm_reuse = data.pop(CONFIRM_NUMBER_REUSE_FIELD, None)
    _apply_required_defaults(con, doc_type, data)
    missing = _missing_required_fields(doc_type, data)
    if missing:
        flash(_("Документът НЕ е издаден: попълнете задължителните полета %(fields)s. "
                "Въведеното е запазено.")
              % {"fields": ", ".join("„%s“" % f for f in missing)}, "error")
        token = _store_preview("doc", (doc_type, data, None, None))
        return redirect("%s?restore=%s" % (form_url, token)), None
    # Одит (01.10.2026, U5): фактура без нито един ред се издаваше без дума.
    if flow["invoice_clients"] and not _has_invoice_item(data.get("items")):
        flash(_("Фактурата няма нито един ред със стока. Добавете поне един ред "
                "и опитайте пак — въведеното е запазено."), "error")
        token = _store_preview("doc", (doc_type, data, None, None))
        return redirect("%s?restore=%s" % (form_url, token)), None
    if flow["needs_items"]:
        # Одит (12.08.2026, находка №3): за ВСИЧКИ типове документи с
        # редове (не само фактурите с ръчен номер по-долу) — вижте
        # _warn_if_negative_values.
        _warn_if_negative_values(data["items"])
        if doc_type == "packing":
            _warn_if_packing_totals_mismatch(data)  # находка №8
    # Одит (03.09.2026, находка №6): заглавните числа — за ВСИЧКИ типове,
    # включително ЧМР, който изобщо няма редове и досега не се проверяваше
    # от нищо (кутия 11 „Бруто тегло“ и кутия 12 „Обем“).
    _warn_if_suspicious_header_numbers(doc_type, data)
    manual_number = None
    if flow["manual_number_field"]:
        manual_number = (data.get(flow["manual_number_field"]) or "").strip()
        # Одит (19.08.2026, находка №41): празен/само-интервален ръчен
        # номер пада обратно към АВТОМАТИЧНИЯ логистичен номер
        # („0001/2026“). Самият fallback е СЪЗНАТЕЛНО решение от
        # предишен кръг (документ без номер изобщо е по-лошо от
        # документ с вътрешен номер — виж appcore.save_document и
        # test_invoice_number_falls_back_to_generated_when_left_empty),
        # затова НЕ го променяме. Проблемът беше МЪЛЧАНИЕТО: търговска
        # фактура излизаше с вътрешен логистичен номер към клиент и
        # митница, без операторът да разбере (`required` в шаблона е
        # само браузърна проверка и „   “ я минава). Сега казваме ясно
        # какво се е случило, за да може да се поправи с редакция.
        if not manual_number:
            flash(_("Не е въведен номер на фактурата — документът получи "
                    "автоматичен вътрешен номер. Ако клиентът очаква Ваш "
                    "фактурен номер, редактирайте документа и го въведете."),
                 "warning")
        elif _number_taken_by_same_type(con, doc_type, manual_number,
                                        confirmed_value=confirm_reuse):
            token = _store_preview("doc", (doc_type, data, None, None))
            return redirect("%s?restore=%s" % (form_url, token)), None
        _warn_if_mixed_orders(data.get("items"))
    stamp_client_alias(con, doc_type, data)  # Одит (04.10.2026, F10)
    try:
        doc_id = save_document(con, doc_type, data, manual_number=manual_number)
    except db.NumberingExhaustedError as exc:
        # Одит (22.08.2026, находка №4): съобщението на самото изключение
        # обяснява ТОЧНО какво се е случило и какво да направи операторът
        # („първите 1000 поредни номера са заети — вероятно от ръчно
        # въведени номера във формата на автоматичните“). Преди това то
        # минаваше по общия клон на _handle_unexpected_error и потребителят
        # виждаше само „Възникна неочаквана грешка“, а въведеният документ
        # се губеше — с restore токена по-долу вече не се губи.
        con.rollback()
        flash(str(exc), "error")
        token = _store_preview("doc", (doc_type, data, None, None))
        return redirect("%s?restore=%s" % (form_url, token)), None
    except sqlite3.IntegrityError:
        # Одит (12.08.2026, находка №13): вижте db._m004_document_number_
        # unique — при вече заета база с УНИКАЛЕН индекс на
        # (doc_type, number), два едновременни опита със СЪЩИЯ ръчен
        # номер вече не могат и двата да минат тихо (предупреждението
        # по-горе само предупреждава, не блокира) — вторият гърми тук с
        # ясна грешка вместо необясним 500. con.rollback() е нужен, за
        # да не остане отворената транзакция от next_number() заклещена.
        con.rollback()
        # Одит (31.08.2026, находка №4, ВИСОКА): въведеното се ЗАПАЗВА,
        # точно както прави съседният блок за изчерпана номерация.
        #
        # Досега тук стоеше само flash + redirect(request.path) — формата
        # се връщаше ПРАЗНА. Проверено с изпълнение: издаване на втора
        # фактура със същия ръчен номер връщаше 302 без `restore=` токен,
        # а въведеното (бележки, всички редове) го нямаше в новата форма.
        # Операторът губеше напълно въведена търговска фактура заради
        # една сгрешена цифра. Този клон е ДАЛЕЧ по-честият от съседния:
        # предупреждението за зает номер само предупреждава и не блокира
        # изпращането.
        #
        # Съобщението вече не твърди, че номерът е зает „междувременно от
        # друг потребител“ — в почти всички реални случаи причината е
        # собствената повторена/сгрешена стойност, а старият текст
        # насочваше оператора да търси несъществуващ виновник.
        flash(_("Номер %s вече е зает от друг документ от същата година. "
                "Въведеното е запазено — променете номера и опитайте пак.")
              % (manual_number or data.get("number", "")), "error")
        # Всеки doc_type endpoint обработва И GET (форма), И POST
        # (запис) на СЪЩИЯ адрес (виж register() по-долу) — request.path
        # връща операторa обратно към формата за същия тип документ.
        token = _store_preview("doc", (doc_type, data, None, None))
        return redirect("%s?restore=%s" % (form_url, token)), None
    except Exception as exc:
        # Одит (03.09.2026, находка №13): последна мрежа — ВСЯКА друга
        # грешка при записа също запазва въведеното, вместо да го
        # изхвърли през общия обработчик. Точният повод: при трайно
        # заета база `db.next_number` изчерпва опитите си и хвърля
        # `RuntimeError` с полезно съобщение („базата е заета от друг
        # едновременен запис — опитайте отново“). То не е `sqlite3.*`,
        # затова не се разпознаваше нито тук, нито от
        # `_is_db_unavailable_error`, и заявката падаше в общия клон:
        # „Възникна неочаквана грешка“ + пренасочване, а въведеното
        # изчезваше. Проверено с изпълнение: чужд писателски катинар,
        # държан над две минути (миграции на друга машина, антивирус
        # върху мрежовия дял) → 302 без `restore=`, 0 записани
        # документа, попълнено ЧМР загубено. Груповото издаване
        # (pallet_bulk_issue) отдавна има точно такъв клон; единичното
        # издаване — не.
        con.rollback()
        applog.log_exception(
            "routes_documents: неуспешен запис на %s — въведеното е запазено"
            % doc_type)
        if _is_disk_full_error(exc):
            # Одит (04.10.2026, R7): „опитайте след няколко секунди“ е грешен
            # съвет при пълен диск — няколко секунди по-късно дискът е пак
            # пълен. Казваме истинската причина и какво да се направи.
            flash(_("Документът НЕ можа да бъде записан: дискът с базата данни е "
                    "пълен. Въведеното е запазено. Освободете място на диска (или "
                    "се обърнете към администратора) и опитайте пак."), "error")
        else:
            flash(_("Документът НЕ можа да бъде записан (%(reason)s). Въведеното "
                    "е запазено — опитайте отново след няколко секунди.")
                  % {"reason": db.error_text(exc)[:200]}, "error")
        token = _store_preview("doc", (doc_type, data, None, None))
        return redirect("%s?restore=%s" % (form_url, token)), None
    # Одит (19.08.2026, находка №13): шаблонът се вади в променлива,
    # преди да влезе в _(). Ако литералът "success_message" стои
    # директно вътре в _(...), `pybabel extract` го приема за
    # преводим низ и в каталозите се появява безсмислен msgid
    # "success_message". Самите текстове се извличат от appcore чрез
    # N_() маркера (виж DOCUMENT_FLOWS там).
    success_template = flow["success_message"]
    flash(_(success_template) % data["number"], "success")
    return redirect(url_for("view_document", doc_id=doc_id)), doc_id


def _document_new(doc_type):
    flow = DOCUMENT_FLOWS[doc_type]
    con = get_db()
    if request.method == "POST":
        data = _apply_fixed_fields(doc_type, form_data())
        if flow["needs_items"]:
            data["items"] = _normalize_invoice_items(doc_type, parse_items())
        return _issue_new_document(con, doc_type, data)[0]
    # Одит (19.08.2026, находка №25): вграждат се най-много CLIENT_EMBED_LIMIT
    # клиента (при типична адресна книга — тоест всички); над този праг
    # останалите се намират през сървърното търсене /clients/lookup, вместо
    # формата да носи ~2 MB HTML при всяко отваряне.
    clients = load_clients(con, CLIENT_EMBED_LIMIT)
    clients_total = count_clients(con)
    settings = db.get_settings(con)
    # Подразбирането зависи от типа документ (appcore.DOCUMENT_FLOWS
    # ["default_sender_lang"]) — "bg" за повечето документи, но "en" за
    # трите фактури (заявка: „опция за изпращач Bg/EN, подразбиране да е
    # английски“). ?sender_lang=bg|en от бутона sender_lang_toggle надделява
    # над подразбирането, каквото и да е то.
    requested_lang = request.args.get("sender_lang")
    sender_lang = requested_lang if requested_lang in ("bg", "en") else flow["default_sender_lang"]
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
        else:
            # Одит (03.09.2026, находка №7): огледално на edit_document
            # по-горе. Поправката на находка №31 от 16.08 стигна само до
            # РЕДАКЦИЯТА — при издаване на НОВ документ клонът беше без
            # `else` и формата се рендираше празна, без нито дума. Токенът
            # изчезва по три напълно битови причини: изтичане (30 мин.),
            # рестарт на процеса (хранилището е в паметта — при
            # автоматично обновяване това става само́) и изхвърляне по брой
            # (_PREVIEW_MAX_ENTRIES). Тоест цяла попълнена фактура с
            # десетки редове изчезваше безмълвно по препоръчания поток
            # „попълни → преглед → назад → издай“.
            flash(_("Данните от предварителния преглед вече не са налични "
                    "(изтекъл линк) — формата е празна, въведете ги наново."),
                  "warning")

    if flow["embed_unload_points"]:
        cj = clients_json(clients, con)  # con все още отворен — вгражда unload_points
        ctx = {"clients": clients, "clients_json": cj, "s": settings, "sender_lang": sender_lang}
    else:
        ctx = {"clients": clients, "clients_json": clients_json(clients), "s": settings,
               "sender_lang": sender_lang}
    # Одит (19.08.2026, находка №25): общият брой клиенти отива към
    # формата, за да знае JavaScript-ът дали вграденият списък е ПЪЛЕН, или
    # трябва да предложи сървърно търсене — виж bindClientSelect в app.js и
    # appcore.CLIENT_EMBED_LIMIT.
    ctx["clients_total"] = clients_total
    if flow["needs_items"]:
        ctx["items"] = restore_data.get("items", []) if restore_data else []
    if flow["invoice_clients"]:
        # Одит (05.09.2026, находка №12): вграждат се първите EMBED_LIMIT
        # записа (при типична инсталация — всичките), а останалите се
        # намират през /invoices/clients/lookup.
        ctx["invoice_clients"] = invoice_clients_module.load_all(
            con, limit=invoice_clients_module.EMBED_LIMIT)
        ctx["invoice_clients_total"] = invoice_clients_module.count_all(con)
        ctx["invoice_clients_json"] = invoice_clients_module.as_json(con)
    if flow["manual_number_field"]:
        ctx["suggested_invoice_number"] = _suggest_invoice_number(con, doc_type)
    if restore_data is not None:
        ctx["edit_data"] = restore_data
    return render_template(flow["form_template"], **ctx)


def _document_preview(doc_type):
    flow = DOCUMENT_FLOWS[doc_type]
    # Заявка: „при връщане назад от преглед за печат въведената информация
    # се губи“ — виж appcore.render_preview за пълното обяснение. Скритото
    # поле „edit_doc_id“ (само в edit_doc_id ветвите на формите) идва
    # ПРАЗНО при издаване на нов документ.
    edit_doc_id_raw = (request.form.get("edit_doc_id") or "").strip()
    edit_doc_id = int(edit_doc_id_raw) if edit_doc_id_raw.isdecimal() else None  # находка №26: „²“.isdigit() е True
    # Одит (19.08.2026, находка №10): версията пътува през прегледа, за да
    # не се „презарежда“ оптимистичното заключване при връщане към формата.
    version_raw = (request.form.get("edit_doc_version") or "").strip()
    edit_doc_version = int(version_raw) if version_raw.isdecimal() else None
    data = _apply_fixed_fields(doc_type, form_data())
    if flow["needs_items"]:
        data["items"] = _normalize_invoice_items(doc_type, parse_items())
    return render_preview(doc_type, data, edit_doc_id=edit_doc_id,
                          edit_doc_version=edit_doc_version)


#: Одит (01.10.2026, P5): прегледи, от които вече е издаден/записан документ
#: (токен → id). Второ „Издай“ (двоен клик, F5, „Назад“) отвежда към вече
#: издадения документ, вместо да издаде втори със същото съдържание.
_issued_previews = OrderedDict()
_issued_previews_lock = threading.Lock()
_ISSUED_PREVIEWS_MAX = 500
#: Одит (04.10.2026, F6): катинар ЗА ВСЕКИ токен на преглед. Проверката
#: „вече издаден ли е“ и самото издаване бяха две отделни стъпки — пет
#: едновременни „Издай“ (двоен клик, мрежа, която повтаря заявката, два
#: таба) минаваха проверката, преди първото да е записало, и даваха пет
#: еднакви ЧМР с поредни номера. Сега само първата заявка издава; останалите
#: изчакват нея и отиват към вече издадения документ.
#: Катинарите не се трият след употреба (иначе закъсняла заявка би взела нов
#: катинар, докато чакащата още държи стария) — пазят се последните
#: _ISSUED_PREVIEWS_MAX, както и самите издадени прегледи.
_preview_issue_locks = OrderedDict()


def _preview_issue_lock(token):
    with _issued_previews_lock:
        lock = _preview_issue_locks.get(token)
        if lock is None:
            lock = _preview_issue_locks[token] = threading.Lock()
            while len(_preview_issue_locks) > _ISSUED_PREVIEWS_MAX:
                _preview_issue_locks.popitem(last=False)
        return lock


@login_required
def issue_from_preview(token):
    """„Издай“ / „Запази промените“ директно от предварителния преглед —
    същите проверки като при подаване на формата (_issue_new_document /
    _save_document_edit, вкл. версията при редакция)."""
    with _preview_issue_lock(token):
        return _issue_from_preview_locked(token)


def _issue_from_preview_locked(token):
    with _issued_previews_lock:
        done_id = _issued_previews.get(token)
    if done_id is not None:
        flash(_("Документът от този преглед вече е издаден/записан."), "info")
        return redirect(url_for("view_document", doc_id=done_id))
    payload = _get_preview(token, "doc")
    if payload is None or payload[0] not in DOCUMENT_FLOWS:
        flash(_("Прегледът е изтекъл — генерирайте го отново от формата."), "warning")
        return redirect(url_for("dashboard"))
    doc_type, data, edit_doc_id = payload[0], deepcopy(payload[1]), payload[2]
    con = get_db()
    if edit_doc_id:
        row, saved = fetch_document(con, edit_doc_id)
        if row["doc_type"] != doc_type:
            abort(404)
        version = payload[3] if len(payload) > 3 else None
        resp, doc_id = _save_document_edit(con, row, saved, data, version)
    else:
        resp, doc_id = _issue_new_document(con, doc_type, data)
    if doc_id is not None:
        with _issued_previews_lock:
            _issued_previews[token] = doc_id
            while len(_issued_previews) > _ISSUED_PREVIEWS_MAX:
                _issued_previews.popitem(last=False)
    return resp


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


# Публични имена за другите route модули (routes_invoices).
document_new = _document_new
document_preview = _document_preview
