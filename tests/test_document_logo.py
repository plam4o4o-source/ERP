# -*- coding: utf-8 -*-
"""Регресионни тестове за разположението на логото на фирмата в печатните
документи (ЧМР, опаковъчен лист, палетна карта, декларациите).

Контекст: логото (когато е качено през „Настройки“) се показва в горната
част на всеки печатен документ — виж static/style.css (.doc-logo) и
branding.py. За ЧМР/опаковъчен лист/палетна карта то седи в лявата част
на съществуващото заглавие (двуколонен хедър с баркод вдясно) — без
промяна тук. За двете декларации (dualuse/export_it), които имат
центрирано заглавие "Д Е К Л А Р А Ц И Я", логото и номерът на
документа са преместени в общ ред (.dud-head-row/.exi-head-row — лого
ляво, № дясно), вместо логото да седи самò центрирано над отделен
дясно подравнен номер — виж коментара в static/style.css."""
import io

from conftest import post_with_csrf

# 1×1 прозрачен PNG (валидни PNG магически байтове — единственото, което
# branding.save_logo() проверява, виж branding._detect_ext).
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _upload_logo(admin_client):
    resp = post_with_csrf(admin_client, "/settings/logo",
                          {"logo_file": (io.BytesIO(_PNG_BYTES), "logo.png")},
                          csrf_source_url="/settings", follow_redirects=False)
    assert resp.status_code == 302


def _issue_doc(admin_client, url, data):
    resp = post_with_csrf(admin_client, url, data, csrf_source_url=url,
                          follow_redirects=False)
    assert resp.status_code == 302
    return admin_client.get(resp.headers["Location"])


def test_no_logo_uploaded_no_doc_logo_markup_anywhere(admin_client):
    resp = _issue_doc(admin_client, "/cmr/new", {
        "sender_name": "Изпращач ЕООД", "consignee_name": "Получател ООД"})
    assert b'class="doc-logo"' not in resp.data


def test_cmr_logo_appears_in_left_title_column(admin_client):
    _upload_logo(admin_client)
    resp = _issue_doc(admin_client, "/cmr/new", {
        "sender_name": "Изпращач ЕООД", "consignee_name": "Получател ООД"})
    body = resp.data.decode()
    assert 'class="doc-logo"' in body
    # Логото е ВЪТРЕ в .title (лявата колона на .cmr-head, до "ЧМР / CMR"),
    # не самостоятелно извън заглавната секция.
    title_start = body.index('class="title"')
    logo_pos = body.index('class="doc-logo"')
    cmr_label_pos = body.index("ЧМР / CMR")
    assert title_start < logo_pos < cmr_label_pos


def test_packing_logo_appears_above_title(admin_client):
    _upload_logo(admin_client)
    resp = _issue_doc(admin_client, "/packing/new", {
        "sender_name": "Изпращач ЕООД", "receiver_name": "Получател"})
    body = resp.data.decode()
    logo_pos = body.index('class="doc-logo"')
    title_pos = body.index("ОПАКОВЪЧЕН ЛИСТ")
    assert logo_pos < title_pos


def test_pallet_logo_appears_above_title_but_not_in_label_format(admin_client):
    _upload_logo(admin_client)
    create_resp = post_with_csrf(admin_client, "/pallet/new", {
        "pallet_no": "1", "items_json": "[]"}, csrf_source_url="/pallet/new",
        follow_redirects=False)
    assert create_resp.status_code == 302
    doc_url = create_resp.headers["Location"]

    resp = admin_client.get(doc_url)
    assert b'class="doc-logo"' in resp.data

    label_resp = admin_client.get(doc_url + "?format=label")
    # На малкия термо етикет (100×150мм) логото се пропуска съзнателно —
    # няма достатъчно място (виж pallet_print.html: `has_logo and not label_format`).
    assert b'class="doc-logo"' not in label_resp.data


def test_dualuse_logo_and_number_share_head_row_above_centered_title(admin_client):
    _upload_logo(admin_client)
    resp = _issue_doc(admin_client, "/dualuse/new", {"sender_name": "Износител ЕООД"})
    body = resp.data.decode()

    row_start = body.index('class="dud-head-row"')
    logo_pos = body.index('class="doc-logo"')
    no_pos = body.index('class="no"')
    title_pos = body.index("Д Е К Л А Р А Ц И Я")
    # Логото и номерът са ДВЕТЕ в общия ред (letterhead), редът идва преди
    # центрираното заглавие "ДЕКЛАРАЦИЯ".
    assert row_start < logo_pos < no_pos < title_pos


def test_export_it_logo_and_number_share_head_row_above_centered_title(admin_client):
    _upload_logo(admin_client)
    resp = _issue_doc(admin_client, "/export-it/new", {"declarant_name": "Декларатор"})
    body = resp.data.decode()

    row_start = body.index('class="exi-head-row"')
    logo_pos = body.index('class="doc-logo"')
    no_pos = body.index('class="no"')
    title_pos = body.index("Д Е К Л А Р А Ц И Я")
    assert row_start < logo_pos < no_pos < title_pos


def test_dualuse_no_logo_keeps_head_row_wrapper_with_just_the_number(admin_client):
    # Без качено лого — {% if has_logo %} пропуска img-а, но .dud-head-row
    # остава (само с .no вътре); margin-left:auto на .no го пази вдясно
    # (виж коментара в style.css) — без визуална регресия спрямо преди тази промяна.
    resp = _issue_doc(admin_client, "/dualuse/new", {"sender_name": "Износител ЕООД"})
    body = resp.data.decode()
    assert 'class="dud-head-row"' in body
    assert 'class="doc-logo"' not in body
    assert 'class="no"' in body


def test_pallet_bulk_preview_shows_logo_above_title(admin_client):
    _upload_logo(admin_client)
    data = {
        "groups": "1",
        "sender_name": "Тест",
        "items_json_1": '[{"order_no": "O1", "pos": "1", "reference": "R1", '
                        '"reference_desc": "D1", "qty": "5"}]',
    }
    resp = post_with_csrf(admin_client, "/pallet/bulk-preview", data,
                          csrf_source_url="/pallet/new", follow_redirects=False)
    assert resp.status_code == 302
    preview_resp = admin_client.get(resp.headers["Location"])
    assert preview_resp.status_code == 200
    assert b'class="doc-logo"' in preview_resp.data
