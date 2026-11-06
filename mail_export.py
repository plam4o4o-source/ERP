# -*- coding: utf-8 -*-
"""„Изпращане на пакета по имейл“ — .eml чернова и (по избор) SMTP.

Заявка (06.11.2026, FIN): от печата на пратката и от всеки документ —
  * „Отвори като имейл“: .eml файл с „X-Unsent: 1“ (Outlook го отваря като
    НОВА чернова за изпращане), прикачени PDF-и от pdf_export (по един на
    документ или слети в един с pypdf), тема/текст от Настройки (BG/EN),
    получател — имейлът на клиента;
  * директно изпращане през SMTP — САМО при изрично натискане, с таймаут,
    ясни грешки и одитен запис. Никога автоматично.

Паролата за SMTP НЕ се пази никъде (в програмата няма механизъм за тайни —
базата е споделена по мрежата и се чете от всеки компютър); въвежда се при
всяко изпращане и живее само в рамките на заявката."""
import contextlib
import io
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import db
import pdf_export
from appcore import safe_json_data

#: Таван на документите в един имейл — PDF-ите се генерират последователно.
MAX_EMAIL_DOCS = 40
SMTP_DEFAULT_TIMEOUT = 20
SMTP_SECURITY = ("starttls", "ssl", "none")

#: Заглавията на типовете в имейла (BG / EN) — като заглавния лист на пратката.
DOC_TITLES = {
    "cmr": ("ЧМР", "CMR"),
    "packing": ("Опаковъчен лист", "Packing list"),
    "pallet": ("Палетна карта", "Pallet card"),
    "waybill": ("Товарителница", "Waybill"),
    "dualuse": ("Декларация за двойна употреба", "Dual-use declaration"),
    "export_it": ("Декларация за износ", "Export declaration"),
    "invoice_br": ("Фактура", "Invoice"),
    "invoice_no": ("Фактура", "Commercial invoice"),
    "invoice_dubai": ("Фактура", "Commercial invoice"),
}
BUNDLE_TITLES = ("Документи на пратката", "Shipment documents")

DEFAULT_TEMPLATES = {
    "fin_mail_subject_bg": "{doc_title} № {numbers} — {company}",
    "fin_mail_body_bg": ("Здравейте,\n\nПрилагаме {doc_title} № {numbers}{po_text_bg}.\n\n"
                         "Поздрави,\n{user}\n{company}"),
    "fin_mail_subject_en": "{doc_title} No. {numbers} — {company}",
    "fin_mail_body_en": ("Dear Sir or Madam,\n\nPlease find attached {doc_title} No. {numbers}"
                         "{po_text_en}.\n\nBest regards,\n{user}\n{company}"),
}


class MailError(Exception):
    """Грешка при SMTP (code — за превода; detail — техническият текст)."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = str(detail or "")[:300]


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_template_text(template, values):
    """str.format_map без гърмене при непознат/грешен плейсхолдър."""
    try:
        return str(template or "").format_map(_SafeDict(values))
    except (ValueError, IndexError):
        return str(template or "")


def mail_values(docs, lang, company, user):
    """Стойностите за плейсхолдърите: {doc_title} {numbers} {number} {client}
    {company} {user} {po} {po_text_bg} {po_text_en} {date}."""
    idx = 1 if lang == "en" else 0
    types = {d["row"]["doc_type"] for d in docs}
    if len(docs) == 1 or len(types) == 1:
        title = DOC_TITLES.get(docs[0]["row"]["doc_type"], ("", ""))[idx]
    else:
        title = BUNDLE_TITLES[idx]
    numbers = ", ".join(str(d["row"]["number"]) for d in docs)
    client = next((str(d["row"]["client_name"]) for d in docs if d["row"]["client_name"]), "")
    pos = []
    for d in docs:
        data = d["data"]
        for po in [data.get("order_no")] + [it.get("po_no") or it.get("order_no")
                                            for it in (data.get("items") or [])
                                            if isinstance(it, dict)]:
            po = " ".join(str(po or "").split())
            if po and po not in pos:
                pos.append(po)
    po = ", ".join(pos[:5])
    return {"doc_title": title, "numbers": numbers, "number": str(docs[0]["row"]["number"]),
            "client": client, "company": company or "", "user": user or "", "po": po,
            "po_text_bg": (" (поръчка %s)" % po) if po else "",
            "po_text_en": (" (P.O. %s)" % po) if po else "",
            "date": str(docs[0]["row"]["created_at"] or "")[:10]}


def document_pdf(con, row):
    """(име на файл, байтове) — СЪЩИЯТ PDF като бутона „Изтегли PDF“
    (routes_documents.export_document_pdf), без HTTP отговора."""
    from routes_documents import (_export_fields_and_items, _export_filename, _export_totals_row,
                                  _pdf_normalized_numbers)
    data = safe_json_data(row["data"])
    doc_type = row["doc_type"]
    title = db.DOC_TYPES.get(doc_type, {}).get("title", doc_type)
    fields, items, cols = _export_fields_and_items(doc_type, data)
    totals_row = _export_totals_row(doc_type, data, items, cols)
    fields, items, totals_row = _pdf_normalized_numbers(fields, items, cols, totals_row, doc_type)
    barcode = "" if doc_type.startswith("invoice_") else row["barcode"]
    pdf = pdf_export.generate_document_pdf(title, row["number"], barcode, fields, items, cols,
                                           totals_row=totals_row)
    return _export_filename(con, doc_type, row, data, "pdf"), pdf


def merge_pdfs(pdfs):
    """Слива PDF-ите в един (pypdf — вече зависимост на xhtml2pdf)."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    for data in pdfs:
        writer.append(io.BytesIO(data))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build_message(to_addr, subject, body, attachments, from_addr="", draft=True):
    """EmailMessage с прикачени PDF-и. draft=True: „X-Unsent: 1“ — Outlook
    отваря .eml файла като нова чернова (бутон „Изпрати“), не като получено."""
    msg = EmailMessage()
    if from_addr:
        msg["From"] = from_addr
    if to_addr:
        msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if draft:
        msg["X-Unsent"] = "1"
    else:
        msg["Message-ID"] = make_msgid()
    msg.set_content(body or "")
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg


def eml_bytes(msg):
    from email import policy
    return msg.as_bytes(policy=policy.SMTP)


def smtp_config(settings):
    try:
        port = int(str(settings.get("fin_smtp_port") or "").strip() or 0)
    except ValueError:
        port = 0
    security = (settings.get("fin_smtp_security") or "starttls").strip()
    if security not in SMTP_SECURITY:
        security = "starttls"
    if not port:
        port = {"ssl": 465, "starttls": 587}.get(security, 25)
    try:
        timeout = int(str(settings.get("fin_smtp_timeout") or "").strip() or SMTP_DEFAULT_TIMEOUT)
    except ValueError:
        timeout = SMTP_DEFAULT_TIMEOUT
    return {"host": (settings.get("fin_smtp_host") or "").strip(), "port": port,
            "security": security, "user": (settings.get("fin_smtp_user") or "").strip(),
            "from": (settings.get("fin_smtp_from") or "").strip(),
            "timeout": max(3, min(timeout, 120))}


def _tls_context():
    ctx = ssl.create_default_context()
    # Резервните сертификати на certifi (както net.urlopen) — ако ги няма, само системните.
    with contextlib.suppress(Exception):
        import certifi
        ctx.load_verify_locations(certifi.where())
    return ctx


def smtp_send(cfg, msg, password=""):  # nosec B107 -- паролата се въвежда при всяко изпращане, не се пази
    """Изпраща msg през SMTP. Вдига MailError с код за ясно съобщение."""
    if not cfg["host"]:
        raise MailError("not_configured")
    try:
        if cfg["security"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"],
                                      context=_tls_context())
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"])
        try:
            server.ehlo()
            if cfg["security"] == "starttls":
                server.starttls(context=_tls_context())
                server.ehlo()
            if cfg["user"]:
                server.login(cfg["user"], password or "")
            server.send_message(msg)
        finally:
            with contextlib.suppress(Exception):
                server.quit()
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("auth", exc.smtp_error.decode("utf-8", "replace")
                        if isinstance(exc.smtp_error, bytes) else exc) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError("recipient", ", ".join(exc.recipients)) from exc
    except smtplib.SMTPSenderRefused as exc:
        raise MailError("sender", exc.sender) from exc
    except smtplib.SMTPNotSupportedError as exc:
        raise MailError("not_supported", exc) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise MailError("timeout", cfg["timeout"]) from exc
    except socket.gaierror as exc:
        raise MailError("host", cfg["host"]) from exc
    except ssl.SSLError as exc:
        raise MailError("tls", exc) from exc
    except ConnectionRefusedError as exc:
        raise MailError("refused", "%s:%s" % (cfg["host"], cfg["port"])) from exc
    except smtplib.SMTPException as exc:
        raise MailError("smtp", exc) from exc
    except OSError as exc:
        raise MailError("network", exc) from exc
