# -*- coding: utf-8 -*-
"""Регресионни тестове за „Прикачване на снимка/скен към издаден
документ“ (attachments.py, routes_documents.document_attachment_*) —
заявка: „направи всичко което предлагаш“ (списък с предложения за
подобрения)."""
import io
import os

from conftest import get_csrf_token, post_with_csrf

# Минимален валиден PNG (1x1 прозрачен пиксел) — истински магически байтове,
# за да мине проверката в attachments._detect_ext.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
_PDF_BYTES = b"%PDF-1.4\n%%EOF"


def _issue_cmr(client, consignee_name="Клиент За Прикачени Файлове ЕООД"):
    resp = post_with_csrf(client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": consignee_name,
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    return doc_id


def _upload(client, doc_id, filename, data, csrf_source=None):
    token = get_csrf_token(client, csrf_source or "/doc/%d" % doc_id)
    return client.post(
        "/doc/%d/attachments" % doc_id,
        data={"csrf_token": token, "attachment": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def test_upload_valid_png_attaches_to_document(admin_client):
    doc_id = _issue_cmr(admin_client)
    resp = _upload(admin_client, doc_id, "снимка.png", _PNG_BYTES)
    assert resp.status_code == 302
    view = admin_client.get("/doc/%d" % doc_id)
    body = view.data.decode()
    assert "снимка.png" in body
    assert "Няма прикачени файлове" not in body


def test_upload_pdf_accepted(admin_client):
    doc_id = _issue_cmr(admin_client)
    resp = _upload(admin_client, doc_id, "scan.pdf", _PDF_BYTES)
    assert resp.status_code == 302
    view = admin_client.get("/doc/%d" % doc_id)
    assert "scan.pdf" in view.data.decode()


def test_upload_rejects_unrecognized_format(admin_client):
    doc_id = _issue_cmr(admin_client)
    resp = _upload(admin_client, doc_id, "бележка.txt", b"just some text, not an image")
    assert resp.status_code == 302
    view = admin_client.get("/doc/%d" % doc_id)
    body = view.data.decode()
    assert "бележка.txt" not in body
    assert "Няма прикачени файлове" in body


def test_uploaded_attachment_is_downloadable_with_correct_mimetype(admin_client):
    doc_id = _issue_cmr(admin_client)
    _upload(admin_client, doc_id, "снимка.png", _PNG_BYTES)
    view = admin_client.get("/doc/%d" % doc_id)
    import re
    m = re.search(rb'/doc/%d/attachments/(\d+)"' % doc_id, view.data)
    assert m, view.data
    attachment_id = int(m.group(1))
    resp = admin_client.get("/doc/%d/attachments/%d" % (doc_id, attachment_id))
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == _PNG_BYTES


def test_employee_cannot_delete_attachment(admin_client, employee_client):
    doc_id = _issue_cmr(admin_client)
    _upload(admin_client, doc_id, "снимка.png", _PNG_BYTES)
    view = admin_client.get("/doc/%d" % doc_id)
    import re
    m = re.search(rb'/doc/%d/attachments/(\d+)"' % doc_id, view.data)
    attachment_id = int(m.group(1))
    token = get_csrf_token(employee_client, "/doc/%d" % doc_id)
    resp = employee_client.post(
        "/doc/%d/attachments/%d/delete" % (doc_id, attachment_id),
        data={"csrf_token": token},
    )
    assert resp.status_code == 403
    view_again = admin_client.get("/doc/%d" % doc_id)
    assert "снимка.png" in view_again.data.decode()


def test_admin_can_delete_attachment(admin_client):
    doc_id = _issue_cmr(admin_client)
    _upload(admin_client, doc_id, "снимка.png", _PNG_BYTES)
    view = admin_client.get("/doc/%d" % doc_id)
    import re
    m = re.search(rb'/doc/%d/attachments/(\d+)"' % doc_id, view.data)
    attachment_id = int(m.group(1))
    token = get_csrf_token(admin_client, "/doc/%d" % doc_id)
    resp = admin_client.post(
        "/doc/%d/attachments/%d/delete" % (doc_id, attachment_id),
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    view_again = admin_client.get("/doc/%d" % doc_id)
    body = view_again.data.decode()
    assert "снимка.png" not in body
    assert "Няма прикачени файлове" in body


def test_preview_document_has_no_attachments_section(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/preview", {
        "sender_name": "Изпращач", "consignee_name": "Клиент За Преглед ЕООД",
    }, csrf_source_url="/cmr/new", follow_redirects=True)
    body = resp.data.decode()
    assert "Прикачени снимки/сканирания" not in body

# ---------------------------------------------------------------- С9: осиротели файлове при изтриване на документ


def test_deleting_document_removes_the_attachments_directory_from_disk(admin_client):
    """Одит (находка С9, среден риск): routes_documents.delete_document
    трие реда в documents (ON DELETE CASCADE маха document_attachments),
    но преди поправката НИКОЙ код не пипаше файловете на диска — папката
    `<база данни>/attachments/<doc_id>/` оставаше завинаги, включително
    четима сканирана подписана ЧМР бланка след „изтриване“ на документа.
    Тук качваме прикачен файл, проверяваме че реално съществува на диска,
    трием документа и проверяваме, че ЦЯЛАТА му папка с прикачени файлове
    изчезва."""
    import attachments

    doc_id = _issue_cmr(admin_client)
    _upload(admin_client, doc_id, "снимка.png", _PNG_BYTES)

    attach_dir = attachments._base_dir(doc_id)
    assert os.path.isdir(attach_dir), "тестът трябва да гарантира, че файлът реално е записан на диска"
    assert os.listdir(attach_dir), "папката трябва да съдържа поне един прикачен файл"

    token = get_csrf_token(admin_client, "/doc/%d" % doc_id)
    resp = admin_client.post("/doc/%d/delete" % doc_id, data={"csrf_token": token},
                             follow_redirects=False)
    assert resp.status_code == 302

    assert not os.path.exists(attach_dir), (
        "папката с прикачени файлове на изтрития документ остава осиротяла на диска: %s" % attach_dir
    )


def test_deleting_document_without_attachments_does_not_error(admin_client):
    """Безопасност по конструкция: shutil.rmtree(..., ignore_errors=True)
    — изтриване на документ БЕЗ прикачени файлове (най-честият случай) не
    бива да гърми, защото папката никога не е била създадена."""
    doc_id = _issue_cmr(admin_client)
    token = get_csrf_token(admin_client, "/doc/%d" % doc_id)
    resp = admin_client.post("/doc/%d/delete" % doc_id, data={"csrf_token": token},
                             follow_redirects=False)
    assert resp.status_code == 302
