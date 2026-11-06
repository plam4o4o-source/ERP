# -*- coding: utf-8 -*-
"""Износ на фактурите към счетоводството, е-фактура и имейл — маршрутите.

Заявка (06.11.2026, FIN):
  1. „Издадени фактури“ → CSV / Excel (по фактура и по желание — по ред) и
     файл за импорт в Microinvest Delta Pro; точно филтрираният списък
     (routes_invoices._invoices_list_filter). Само сваляне, без мрежа.
  2. Е-фактура UBL 2.1 / Peppol BIS 3.0 (einvoice.py) — за една фактура и
     ZIP за филтрирания списък.
  3. „Отвори като имейл“ (.eml чернова) и изрично SMTP изпращане
     (mail_export.py) — от документа и от печата на пратката.
  4. SAF-T (леко): CSV/Excel съдържат ДДС номерата, държавите, вида на
     документа, валутата и сумите — БЕЗ генериране на SAF-T XML.
Настройките (SMTP, шаблони на имейла, колоните за Microinvest) — само за
администратор, /settings/finance. Теглото не се изнася никъде."""
import csv
import io
import zipfile
from datetime import date

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_babel import gettext as _
from flask_babel import gettext as _dyn  # превод на готов msgid (не се извлича от pybabel)

import applog
import db
import einvoice
import fin_data
import mail_export
from appcore import admin_required, fetch_document, get_db, login_required, safe_json_data

#: Версията на спецификацията на Microinvest, по която е подреден файлът.
#: Заявка (06.11.2026, FIN): manual_importfilters.pdf (microinvest.net) НЕ
#: можа да се изтегли от средата на разработка (мрежата го блокира) —
#: подредбата е по общото описание на импорта от Excel в Delta Pro и се
#: настройва от Настройки → Счетоводство и имейл.
MICROINVEST_SPEC = "manual_importfilters.pdf — импорт от Excel, Delta Pro (непроверена версия)"

#: Полетата, които могат да се изберат за колона на файла за Microinvest.
MI_FIELDS = (
    "date", "number", "doc_code", "doc_kind", "buyer_name", "buyer_vat", "buyer_eik",
    "buyer_country", "buyer_address", "delivery_country", "debit_account", "credit_account",
    "net", "vat", "total", "currency", "vat_rate", "vat_category", "vat_basis", "description",
    "terms_payment", "due_date", "orders", "seller_vat",
)
MI_DEFAULT_COLUMNS = "\n".join((
    "Дата=date", "Номер на документ=number", "Вид документ=doc_code",
    "Контрагент=buyer_name", "ДДС номер=buyer_vat", "Булстат=buyer_eik",
    "Държава=buyer_country", "Дебит=debit_account", "Кредит=credit_account",
    "Сума без ДДС=net", "ДДС=vat", "Сума общо=total", "Валута=currency",
    "ДДС ставка=vat_rate", "Основание=vat_basis", "Описание=description",
))

FIN_DEFAULTS = dict(mail_export.DEFAULT_TEMPLATES, **{
    "fin_smtp_host": "", "fin_smtp_port": "", "fin_smtp_security": "starttls",
    "fin_smtp_user": "", "fin_smtp_from": "", "fin_smtp_timeout": "20",
    "fin_mi_columns": MI_DEFAULT_COLUMNS, "fin_mi_debit": "411", "fin_mi_credit": "702",
    "fin_ei_endpoint_scheme": "",
})
FIN_KEYS = tuple(FIN_DEFAULTS)


def fin_settings(con):
    s = db.get_settings(con)
    merged = dict(FIN_DEFAULTS)
    merged.update({k: v for k, v in s.items() if k in FIN_DEFAULTS and str(v or "").strip()})
    return s, merged


def register(app):
    app.add_url_rule("/invoices/export.csv", "invoices_export_csv", invoices_export_csv)
    app.add_url_rule("/invoices/export-accounting.xlsx", "invoices_export_accounting",
                     invoices_export_accounting)
    app.add_url_rule("/invoices/export-microinvest.xlsx", "invoices_export_microinvest",
                     invoices_export_microinvest)
    app.add_url_rule("/invoices/einvoice.zip", "invoices_einvoice_zip", invoices_einvoice_zip)
    app.add_url_rule("/doc/<int:doc_id>/einvoice.xml", "document_einvoice_xml",
                     document_einvoice_xml)
    app.add_url_rule("/email/compose", "email_compose", email_compose)
    app.add_url_rule("/email/draft.eml", "email_draft", email_draft, methods=["GET", "POST"])
    app.add_url_rule("/email/send", "email_send", email_send, methods=["POST"])
    app.add_url_rule("/settings/finance", "finance_settings", finance_settings,
                     methods=["GET", "POST"])
    app.add_template_global(_smtp_configured, name="finance_smtp_configured")


# ---------------------------------------------------------------- данни на списъка

def _filtered_rows(con):
    """Фактурите от ТЕКУЩИЯ филтър на „Издадени фактури“, хронологично."""
    from routes_documents import _LIST_EXPORT_MAX_ROWS
    from routes_invoices import _invoices_list_filter
    f = _invoices_list_filter()
    rows = con.execute(
        "SELECT d.*, u.full_name AS author FROM documents d"
        " LEFT JOIN users u ON u.id = d.created_by %s"
        " ORDER BY d.id DESC LIMIT ?" % f["where"],  # nosec B608 -- where е с „?“ плейсхолдъри
        list(f["params"]) + [_LIST_EXPORT_MAX_ROWS + 1]).fetchall()
    truncated = len(rows) > _LIST_EXPORT_MAX_ROWS
    rows = rows[:_LIST_EXPORT_MAX_ROWS]
    return rows, truncated


def _records(con):
    rows, truncated = _filtered_rows(con)
    settings = db.get_settings(con)
    cache = {}
    recs = [fin_data.invoice_record(con, r, settings, cache=cache) for r in rows]
    recs.sort(key=lambda r: (r["date"], str(r["number"])))
    return recs, truncated


def _due_date(rec):
    days = fin_data.payment_days(rec["terms_payment"])
    if days is None:
        return ""
    try:
        from datetime import timedelta
        return (date.fromisoformat(rec["date"]) + timedelta(days=days)).isoformat()
    except ValueError:
        return ""


def _invoice_columns():
    """(заглавие, функция) за един ред на фактура — общо за CSV и Excel.
    SAF-T (леко): ДДС номера, държави, вид документ, валута и суми."""
    return [
        (_("Дата"), lambda r: r["date"]),
        (_("Фактура №"), lambda r: r["number"]),
        (_("Код на документа"), lambda r: "01"),
        (_("Вид фактура"), lambda r: _dyn(r["type_title"])),
        (_("Купувач"), lambda r: r["buyer"]["name"]),
        (_("ДДС № на купувача"), lambda r: r["buyer"]["vat"]),
        (_("ЕИК на купувача"), lambda r: r["buyer"]["eik"]),
        (_("Държава на купувача"), lambda r: r["buyer"]["country"]),
        (_("Адрес на купувача"), lambda r: " ".join(r["buyer"]["address"].split())),
        (_("Държава на доставка"), lambda r: r["consignee"]["country"]),
        (_("Получател"), lambda r: r["consignee"]["name"]),
        (_("Продавач"), lambda r: r["seller"]["legal_name"]),
        (_("ДДС № на продавача"), lambda r: r["seller"]["vat"]),
        (_("Валута"), lambda r: r["currency"]),
        (_("Сума без ДДС"), lambda r: r["net"]),
        (_("ДДС"), lambda r: r["vat"]),
        (_("Сума общо"), lambda r: r["total"]),
        (_("ДДС категория"), lambda r: r["vat_category"]),
        (_("Основание за 0 % ДДС"), lambda r: r["vat_basis"]),
        (_("Условия на плащане"), lambda r: r["terms_payment"]),
        (_("Падеж"), _due_date),
        (_("Условия на доставка"), lambda r: r["terms_delivery"]),
        (_("Поръчка № (PO)"), lambda r: ", ".join(r["orders"])),
        (_("Брой редове"), lambda r: len(r["lines"])),
        (_("Издал"), lambda r: r["author"] or ""),
    ]


def _line_columns():
    return [
        (_("Фактура №"), lambda r, ln: r["number"]),
        (_("Дата"), lambda r, ln: r["date"]),
        (_("Купувач"), lambda r, ln: r["buyer"]["name"]),
        (_("ДДС № на купувача"), lambda r, ln: r["buyer"]["vat"]),
        (_("Държава на купувача"), lambda r, ln: r["buyer"]["country"]),
        (_("Ред №"), lambda r, ln: ln["no"]),
        (_("HS код"), lambda r, ln: ln["hs_code"]),
        (_("Код на материала"), lambda r, ln: ln["material_code"]),
        (_("Описание"), lambda r, ln: ln["description"]),
        (_("Поръчка № (PO)"), lambda r, ln: ln["po_no"]),
        (_("Позиция"), lambda r, ln: ln["pos"]),
        (_("Количество"), lambda r, ln: ln["qty"]),
        (_("Мярка (код)"), lambda r, ln: ln["unit_code"]),
        (_("Единична цена"), lambda r, ln: ln["price"]),
        (_("Стойност"), lambda r, ln: ln["total"]),
        (_("Валута"), lambda r, ln: r["currency"]),
        (_("ДДС категория"), lambda r, ln: r["vat_category"]),
    ]


_CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value):
    """Числата — с точка; текст, започващ като формула, се неутрализира."""
    import decimal
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value or "")
    if text.startswith(_CSV_FORMULA_START):
        return "'" + text
    return text


def _today_stub():
    return date.today().strftime("%Y-%m-%d")


def _empty_redirect():
    flash(_("Няма фактури за износ при този филтър."), "info")
    return redirect(url_for("invoices_list", **{k: v for k, v in request.args.items()
                                                if k in ("type", "q", "from", "to", "client")}))


@login_required
def invoices_export_csv():
    """CSV (UTF-8 с BOM, „;“) — по фактура, или по ред при ?lines=1."""
    con = get_db()
    recs, truncated = _records(con)
    if not recs:
        return _empty_redirect()
    by_lines = request.args.get("lines") == "1"
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    if by_lines:
        cols = _line_columns()
        w.writerow([h for h, _f in cols])
        for r in recs:
            for ln in r["lines"]:
                w.writerow([_csv_cell(f(r, ln)) for _h, f in cols])
    else:
        cols = _invoice_columns()
        w.writerow([h for h, _f in cols])
        for r in recs:
            w.writerow([_csv_cell(f(r)) for _h, f in cols])
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    applog.log_audit("износ на фактурите за счетоводството (CSV)",
                     "%d фактури%s%s" % (len(recs), ", по редове" if by_lines else "",
                                         ", съкратен" if truncated else ""))
    name = "invoices_%s_%s.csv" % ("lines" if by_lines else "accounting", _today_stub())
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                     mimetype="text/csv")


def _xlsx_write_sheet(ws, headers, rows_values, widths):
    from routes_documents import (_DATE_NUMBER_FORMAT, _list_row_styler, _list_sheet_finish,
                                  _list_sheet_header, _xlsx_append, _xlsx_date, _xlsx_styles)
    import decimal
    st = _xlsx_styles()
    last = _list_sheet_header(ws, headers, st)
    style_row = _list_row_styler(ws, st, len(headers))
    for values in rows_values:
        out, formats = [], {}
        for idx, v in enumerate(values, 1):
            if isinstance(v, decimal.Decimal):
                out.append(float(v))
                formats[idx] = "#,##0.00" if v == v.quantize(decimal.Decimal("0.01")) else "0.######"
            elif isinstance(v, str) and _xlsx_date(v) and len(v) == 10:
                out.append(_xlsx_date(v))
                formats[idx] = _DATE_NUMBER_FORMAT
            else:
                out.append(v)
        last = _xlsx_append(ws, out)
        style_row(last)
        for col, fmt in formats.items():
            ws.cell(row=last, column=col).number_format = fmt
    _list_sheet_finish(ws, len(headers), widths, last)


def _xlsx_response(wb, name):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@login_required
def invoices_export_accounting():
    """Excel за счетоводството: лист „Фактури“ (+ „Редове“ при ?lines=1)."""
    from openpyxl import Workbook
    con = get_db()
    recs, truncated = _records(con)
    if not recs:
        return _empty_redirect()
    wb = Workbook()
    ws = wb.active
    ws.title = _("Фактури")[:31]
    cols = _invoice_columns()
    _xlsx_write_sheet(ws, [h for h, _f in cols], ([f(r) for _h, f in cols] for r in recs),
                      [12, 16, 8, 22, 30, 16, 12, 8, 36, 8, 26, 26, 16, 7, 14, 10, 14, 8, 22,
                       16, 12, 10, 20, 8, 18])
    by_lines = request.args.get("lines") == "1"
    if by_lines:
        lws = wb.create_sheet(_("Редове")[:31])
        lcols = _line_columns()
        _xlsx_write_sheet(lws, [h for h, _f in lcols],
                          ([f(r, ln) for _h, f in lcols] for r in recs for ln in r["lines"]),
                          [16, 12, 28, 16, 8, 6, 12, 20, 30, 14, 8, 10, 8, 12, 14, 7, 8])
    if truncated:
        from routes_documents import _LIST_EXPORT_MAX_ROWS
        ws.append([_("Показани са първите %(count)d документа — стеснете филтъра, "
                     "за да изнесете останалите.") % {"count": _LIST_EXPORT_MAX_ROWS}])
    applog.log_audit("износ на фактурите за счетоводството (Excel)",
                     "%d фактури%s" % (len(recs), ", по редове" if by_lines else ""))
    return _xlsx_response(wb, "invoices_accounting_%s.xlsx" % _today_stub())


def parse_mi_columns(text):
    """„Заглавие=поле“ на ред → [(заглавие, поле)], грешни редове → errors."""
    cols, errors = [], []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header, sep, field = line.rpartition("=")
        header, field = header.strip(), field.strip()
        if not sep or not header or field not in MI_FIELDS:
            errors.append(line)
            continue
        cols.append((header, field))
    return cols, errors


def mi_value(rec, field, settings):
    return {
        "date": rec["date"], "number": rec["number"], "doc_code": "01",
        "doc_kind": "Фактура", "buyer_name": rec["buyer"]["name"],
        "buyer_vat": rec["buyer"]["vat"], "buyer_eik": rec["buyer"]["eik"],
        "buyer_country": rec["buyer"]["country"],
        "buyer_address": " ".join(rec["buyer"]["address"].split()),
        "delivery_country": rec["consignee"]["country"],
        "debit_account": settings.get("fin_mi_debit") or "411",
        "credit_account": settings.get("fin_mi_credit") or "702",
        "net": rec["net"], "vat": rec["vat"], "total": rec["total"],
        "currency": rec["currency"], "vat_rate": 0, "vat_category": rec["vat_category"],
        "vat_basis": rec["vat_basis"],
        "description": "Фактура № %s%s" % (rec["number"], (" / PO " + ", ".join(rec["orders"]))
                                           if rec["orders"] else ""),
        "terms_payment": rec["terms_payment"], "due_date": _due_date(rec),
        "orders": ", ".join(rec["orders"]), "seller_vat": rec["seller"]["vat"],
    }.get(field, "")


@login_required
def invoices_export_microinvest():
    """Файл за импорт в Microinvest Delta Pro (Excel). Колоните — от
    Настройки (fin_mi_columns); втори лист с бележка за проверка."""
    from openpyxl import Workbook
    con = get_db()
    recs, _truncated = _records(con)
    if not recs:
        return _empty_redirect()
    _raw, settings = fin_settings(con)
    cols, _errors = parse_mi_columns(settings["fin_mi_columns"])
    if not cols:
        cols, _errors = parse_mi_columns(MI_DEFAULT_COLUMNS)
    wb = Workbook()
    ws = wb.active
    ws.title = "Import"
    _xlsx_write_sheet(ws, [h for h, _f in cols],
                      ([mi_value(r, f, settings) for _h, f in cols] for r in recs),
                      [max(10, min(36, len(h) + 4)) for h, _f in cols])
    note = wb.create_sheet(_("Бележка")[:31])
    note.append([_("Проверете с вашия счетоводител; формат по документацията на Microinvest "
                   "(версия: %(version)s).") % {"version": MICROINVEST_SPEC}])
    note.append([_("Колоните и сметките (дебит/кредит) се настройват от администратор в "
                   "Настройки → Счетоводство и имейл.")])
    note.append([_("Сумите са в евро, без ДДС (износ или вътреобщностна доставка, 0 %).")])
    note.column_dimensions["A"].width = 120
    applog.log_audit("износ на фактурите за Microinvest", "%d фактури" % len(recs))
    return _xlsx_response(wb, "microinvest_import_%s.xlsx" % _today_stub())


# ---------------------------------------------------------------- е-фактура

def einvoice_error_text(exc):
    texts = {
        "no_lines": _("Фактурата няма нито един ред с количество и цена."),
        "seller_vat": _("Липсва ДДС № на фирмата изпращач (Настройки → Фирма изпращач)."),
        "seller_name": _("Липсва името на фирмата изпращач (Настройки → Фирма изпращач)."),
        "buyer_name": _("Липсва името на купувача."),
        "buyer_vat": _("Доставката е до държава от ЕС (%(country)s) — за вътреобщностна "
                       "доставка (категория K) е нужен ДДС № на купувача от ЕС. Добавете го в "
                       "адреса за фактуриране (напр. „VAT: IT01234567890“) или в адресната "
                       "книга.") % {"country": exc.detail or "?"},
        "domestic": _("Доставката е в България — е-фактура с ДДС 20 % не се поддържа "
                      "(фактурите в програмата са за износ и ВОД)."),
        "country": _("Не може да се определи държавата на купувача/получателя — допишете я като "
                     "последен ред на адреса."),
    }
    return texts.get(getattr(exc, "code", ""), str(exc))


def einvoice_warning_text(code):
    return {
        "buyer_endpoint": _("няма електронен адрес на купувача (имейл или ДДС №)"),
        "seller_iban": _("няма IBAN на фирмата изпращач"),
        "skipped_lines": _("редове без количество или цена са пропуснати"),
    }.get(code, code)


def _einvoice_bytes(con, row, settings, cache=None):
    rec = fin_data.invoice_record(con, row, settings, cache=cache)
    xml, warnings = einvoice.build_invoice(rec, settings.get("fin_ei_endpoint_scheme", ""))
    if warnings:
        # Предупрежденията — като XML коментар в началото (видими при отваряне).
        comment = "<!-- %s: %s -->\n" % ("Внимание / Warning", "; ".join(
            einvoice_warning_text(w) for w in warnings).replace("--", "—"))
        head, _sep, body = xml.decode("utf-8").partition("\n")
        xml = (head + "\n" + comment + body).encode("utf-8")
    return rec, xml, warnings


@login_required
def document_einvoice_xml(doc_id):
    con = get_db()
    row, _data = fetch_document(con, doc_id)
    if row["doc_type"] not in db.INVOICE_DOC_TYPES:
        abort(404)
    _raw, settings = fin_settings(con)
    settings = dict(_raw, **settings)
    try:
        rec, xml, warnings = _einvoice_bytes(con, row, settings)
    except einvoice.EInvoiceError as exc:
        flash(_("Е-фактурата не е създадена: %(reason)s") % {"reason": einvoice_error_text(exc)},
              "error")
        return redirect(url_for("view_document", doc_id=doc_id))
    applog.log_audit("износ на е-фактура (UBL)", "id=%s №%s" % (row["id"], row["number"]))
    return send_file(io.BytesIO(xml), as_attachment=True, download_name=einvoice.einvoice_filename(rec),
                     mimetype="application/xml")


@login_required
def invoices_einvoice_zip():
    """ZIP с е-фактурите от текущия филтър; неуспелите — в ГРЕШКИ.txt."""
    con = get_db()
    rows, _truncated = _filtered_rows(con)
    if not rows:
        return _empty_redirect()
    _raw, settings = fin_settings(con)
    settings = dict(_raw, **settings)
    buf = io.BytesIO()
    ok, failed = 0, []
    names, cache = set(), {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            try:
                rec, xml, _w = _einvoice_bytes(con, row, settings, cache)
            except einvoice.EInvoiceError as exc:
                failed.append("%s: %s" % (row["number"], einvoice_error_text(exc)))
                continue
            name = einvoice.einvoice_filename(rec)
            base, n = name, 2
            while name in names:
                name = base.replace(".xml", "_%d.xml" % n)
                n += 1
            names.add(name)
            zf.writestr(name, xml)
            ok += 1
        if failed:
            zf.writestr("ERRORS.txt", ("﻿" + "\r\n".join(failed) + "\r\n").encode("utf-8"))
    if not ok:
        flash(_("Нито една е-фактура не е създадена: %(reasons)s")
              % {"reasons": "; ".join(failed[:5])}, "error")
        return _empty_redirect_no_flash()
    applog.log_audit("износ на е-фактури (ZIP)", "%d файла, %d с грешка" % (ok, len(failed)))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="einvoices_%s.zip" % _today_stub(),
                     mimetype="application/zip")


def _empty_redirect_no_flash():
    return redirect(url_for("invoices_list", **{k: v for k, v in request.args.items()
                                                if k in ("type", "q", "from", "to", "client")}))


# ---------------------------------------------------------------- имейл

def _smtp_configured():
    try:
        return bool((db.get_settings(get_db()).get("fin_smtp_host") or "").strip())
    except Exception:  # pylint: disable=broad-except
        return False


def _email_docs(con):
    """Документите от ?ids= (формат на печата на пратката: „id:екз.,…“)."""
    from shipment import _parse_ids, _may_view
    wanted = _parse_ids(request.values.get("ids"))[:mail_export.MAX_EMAIL_DOCS]
    docs = []
    for doc_id, _copies in wanted:
        row = con.execute("SELECT d.*, u.full_name AS author FROM documents d"
                          " LEFT JOIN users u ON u.id = d.created_by WHERE d.id = ?",
                          (doc_id,)).fetchone()
        if row is None or not _may_view(row):
            continue
        docs.append({"row": row, "data": safe_json_data(row["data"])})
    return docs


def _mail_lang():
    lang = request.values.get("lang", "")
    return lang if lang in ("bg", "en") else "en"


def _mail_defaults(con, docs, lang):
    raw, settings = fin_settings(con)
    company = (raw.get("sender_name_en") if lang == "en" else "") or raw.get("sender_name") or ""
    values = mail_export.mail_values(docs, lang, company,
                                     session.get("full_name") or session.get("username"))
    subject = mail_export.render_template_text(settings["fin_mail_subject_" + lang], values)
    body = mail_export.render_template_text(settings["fin_mail_body_" + lang], values)
    to_addr = ""
    for d in docs:
        to_addr = fin_data.recipient_email(con, d["row"], d["data"])
        if to_addr:
            break
    return {"subject": subject, "body": body, "to": to_addr}, raw, settings


@login_required
def email_compose():
    con = get_db()
    docs = _email_docs(con)
    if not docs:
        abort(404)
    lang = _mail_lang()
    defaults, raw, settings = _mail_defaults(con, docs, lang)
    cfg = mail_export.smtp_config(settings)
    return render_template(
        "email_compose.html", docs=docs, lang=lang, ids=request.values.get("ids", ""),
        from_id=request.values.get("from", type=int) or docs[0]["row"]["id"],
        to_addr=defaults["to"], subject=defaults["subject"], body=defaults["body"],
        mode="merged" if request.values.get("mode") == "merged" else "separate",
        smtp=cfg, from_addr=cfg["from"] or raw.get("sender_email") or cfg["user"],
        max_docs=mail_export.MAX_EMAIL_DOCS,
        type_titles={k: v["title"] for k, v in db.DOC_TYPES.items()})


def _attachments(con, docs, mode):
    files = [mail_export.document_pdf(con, d["row"]) for d in docs]
    if mode == "merged" and len(files) > 1:
        import client_export
        first = client_export.sanitize_number_stub(docs[0]["row"]["number"])
        return [("shipment_%s.pdf" % first, mail_export.merge_pdfs([f[1] for f in files]))]
    seen, out = set(), []
    for name, data in files:
        base, n = name, 2
        while name in seen:
            name = base[:-4] + "_%d.pdf" % n
            n += 1
        seen.add(name)
        out.append((name, data))
    return out


def _compose_fields():
    return {"to": " ".join(request.values.get("to", "").split()),
            "subject": " ".join(request.values.get("subject", "").split())[:400],
            "body": request.values.get("body", "")[:20000],
            "mode": "merged" if request.values.get("mode") == "merged" else "separate"}


def _back_to_compose():
    return redirect(url_for("email_compose", ids=request.values.get("ids", ""),
                            lang=_mail_lang(), mode=request.values.get("mode", ""),
                            **{"from": request.values.get("from", "")}))


def _pdf_failure(exc):
    import pdf_export
    if isinstance(exc, pdf_export.PdfBusyError):
        flash(_("Точно сега се генерират други PDF файлове. Изчакайте няколко "
                "секунди и опитайте пак."), "warning")
    else:
        flash(_("PDF файлът не можа да се генерира за този документ. "
                "Опитайте пак — ако продължава, съобщете на администратор."), "error")


@login_required
def email_draft():
    """.eml чернова (X-Unsent: 1) — сваля се, Outlook я отваря за изпращане."""
    import pdf_export
    con = get_db()
    docs = _email_docs(con)
    if not docs:
        abort(404)
    f = _compose_fields()
    if not f["subject"] or not f["body"]:
        defaults, _raw, _s = _mail_defaults(con, docs, _mail_lang())
        f["subject"] = f["subject"] or defaults["subject"]
        f["body"] = f["body"] or defaults["body"]
        f["to"] = f["to"] if "to" in request.values else defaults["to"]
    if f["to"] and not all(fin_data.valid_email(a.strip()) for a in f["to"].split(",") if a.strip()):
        flash(_("Невалиден имейл адрес на получателя: %(to)s") % {"to": f["to"]}, "error")
        return _back_to_compose()
    try:
        files = _attachments(con, docs, f["mode"])
    except (pdf_export.PdfBusyError, RuntimeError) as exc:
        _pdf_failure(exc)
        return _back_to_compose()
    msg = mail_export.build_message(f["to"], f["subject"], f["body"], files, draft=True)
    applog.log_audit("имейл чернова (.eml)", "документи=%s, PDF=%d"
                     % (",".join(str(d["row"]["id"]) for d in docs), len(files)))
    import client_export
    name = "email_%s.eml" % client_export.sanitize_number_stub(docs[0]["row"]["number"])
    return send_file(io.BytesIO(mail_export.eml_bytes(msg)), as_attachment=True,
                     download_name=name, mimetype="message/rfc822")


def mail_error_text(exc):
    return {
        "not_configured": _("SMTP сървърът не е настроен (Настройки → Счетоводство и имейл)."),
        "auth": _("Сървърът отказа потребителя или паролата."),
        "recipient": _("Сървърът отказа получателя: %(detail)s"),
        "sender": _("Сървърът отказа подателя: %(detail)s"),
        "not_supported": _("Сървърът не поддържа избраната защита (STARTTLS/вход)."),
        "timeout": _("Сървърът не отговори за %(detail)s секунди."),
        "host": _("Сървърът „%(detail)s“ не е намерен — проверете адреса и мрежата."),
        "tls": _("Грешка в защитената връзка (TLS/SSL): %(detail)s"),
        "refused": _("Връзката към %(detail)s е отказана — проверете порта и защитата."),
        "smtp": _("Грешка от SMTP сървъра: %(detail)s"),
        "network": _("Мрежова грешка: %(detail)s"),
    }.get(exc.code, _("Грешка: %(detail)s")) % {"detail": exc.detail}


@login_required
def email_send():
    """Изпращане през SMTP — само при изрично натискане на бутона (POST с
    CSRF). Паролата идва от формата и не се записва никъде."""
    import pdf_export
    con = get_db()
    docs = _email_docs(con)
    if not docs:
        abort(404)
    _raw, settings = fin_settings(con)
    cfg = mail_export.smtp_config(settings)
    f = _compose_fields()
    recipients = [a.strip() for a in f["to"].split(",") if a.strip()]
    if not recipients or not all(fin_data.valid_email(a) for a in recipients):
        flash(_("Въведете валиден имейл адрес на получателя."), "error")
        return _back_to_compose()
    if not f["subject"]:
        flash(_("Въведете тема на писмото."), "error")
        return _back_to_compose()
    if not cfg["host"]:
        flash(mail_error_text(mail_export.MailError("not_configured")), "error")
        return _back_to_compose()
    try:
        files = _attachments(con, docs, f["mode"])
    except (pdf_export.PdfBusyError, RuntimeError) as exc:
        _pdf_failure(exc)
        return _back_to_compose()
    from_addr = cfg["from"] or _raw.get("sender_email") or cfg["user"]
    msg = mail_export.build_message(", ".join(recipients), f["subject"], f["body"], files,
                                    from_addr=from_addr, draft=False)
    detail = "до=%s, документи=%s, PDF=%d, сървър=%s:%s" % (
        ", ".join(recipients), ",".join(str(d["row"]["id"]) for d in docs), len(files),
        cfg["host"], cfg["port"])
    try:
        mail_export.smtp_send(cfg, msg, request.form.get("password", ""))
    except mail_export.MailError as exc:
        applog.log_audit("неуспешно изпращане на имейл", "%s; грешка=%s" % (detail, exc.code))
        flash(_("Имейлът НЕ е изпратен. %(reason)s") % {"reason": mail_error_text(exc)}, "error")
        return _back_to_compose()
    applog.log_audit("изпратен имейл с документи", detail)
    flash(_("Имейлът е изпратен до %(to)s (%(count)d PDF).")
          % {"to": ", ".join(recipients), "count": len(files)}, "success")
    from_id = request.values.get("from", type=int) or docs[0]["row"]["id"]
    return redirect(url_for("view_document", doc_id=from_id))


# ---------------------------------------------------------------- настройки

@admin_required
def finance_settings():
    con = get_db()
    if request.method == "POST":
        values = {k: request.form.get(k, "").strip() if not k.startswith("fin_mail_body")
                  and k != "fin_mi_columns" else request.form.get(k, "").replace("\r\n", "\n")
                  for k in FIN_KEYS if k in request.form}
        errors = []
        if "fin_smtp_port" in values and values["fin_smtp_port"]:
            if not values["fin_smtp_port"].isdigit() or not 0 < int(values["fin_smtp_port"]) < 65536:
                errors.append(_("Портът трябва да е число от 1 до 65535."))
        if values.get("fin_smtp_security", "starttls") not in mail_export.SMTP_SECURITY:
            values["fin_smtp_security"] = "starttls"
        if values.get("fin_smtp_timeout") and not values["fin_smtp_timeout"].isdigit():
            errors.append(_("Таймаутът трябва да е цяло число секунди."))
        if values.get("fin_smtp_from") and not fin_data.valid_email(values["fin_smtp_from"]):
            errors.append(_("Невалиден имейл адрес на подателя."))
        if "fin_mi_columns" in values:
            cols, bad = parse_mi_columns(values["fin_mi_columns"])
            if bad:
                errors.append(_("Непознати редове в колоните за Microinvest: %(lines)s")
                              % {"lines": "; ".join(bad[:5])})
            elif not cols:
                values["fin_mi_columns"] = ""
        if errors:
            for e in errors:
                flash(e, "error")
            s = dict(fin_settings(con)[1])
            s.update(values)
            return render_template("finance_settings.html", s=s, mi_fields=MI_FIELDS,
                                   mi_spec=MICROINVEST_SPEC, defaults=FIN_DEFAULTS)
        # Записват се само реално променените полета (не презаписваме чужда
        # промяна от друг отворен прозорец с непипнати стойности).
        current = fin_settings(con)[1]
        values = {k: v for k, v in values.items() if v != current.get(k, "")}
        if not values:
            flash(_("Няма променени данни."), "info")
            return redirect(url_for("finance_settings"))
        db.save_settings(con, values)
        con.commit()
        applog.log_audit("променени настройки за счетоводство и имейл",
                         "полета=%s" % ", ".join(sorted(values)))
        flash(_("Настройките за счетоводство и имейл са запазени."), "success")
        return redirect(url_for("finance_settings"))
    return render_template("finance_settings.html", s=fin_settings(con)[1], mi_fields=MI_FIELDS,
                           mi_spec=MICROINVEST_SPEC, defaults=FIN_DEFAULTS)
