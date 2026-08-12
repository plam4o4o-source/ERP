# -*- coding: utf-8 -*-
"""Регресионни тестове за заявка на потребителя: „при връщане назад от
преглед за печат въведената информация се губи, оправи го да не се губи“.

Причина: бутонът „Предварителен преглед (без запис)“ винаги POST-ваше към
ФИКСИРАН endpoint за издаване на НОВ документ (напр. cmr_preview),
независимо дали формата в момента редактира вече ИЗДАДЕН документ (/doc/
<id>/edit) — „Назад към формата“ от прегледа тогава винаги връщаше към
празната форма за издаване на нов документ (само с възстановени полета
чрез ?restore=), НЕ към /doc/<id>/edit. Самите въведени СТОЙНОСТИ се
възстановяваха коректно, но потребителят губеше връзката коя точно
редакция продължава — бутонът „Запази промените“ вече не съществуваше
(само „Издай...“), а истинският редактиран документ оставаше непроменен.
На практика това изглежда точно като загубена информация.

Поправка: ново скрито поле `edit_doc_id` във всяка форма (празно при
издаване на нов документ, попълнено с ID-то при редакция), пренасяно през
appcore.render_preview() в самия preview payload, ползвано от:
- app.py preview_document() / tests/conftest.py-то копие: за да пресметне
  правилния back_url в печатните шаблони (edit_document вместо <type>_new).
- routes_documents.edit_document(): вече чете ?restore=<token> точно както
  _document_new() отдавна прави, за да предзареди В ПРОЦЕС НА РЕДАКТИРАНЕ
  стойностите (не запазените в базата), ако прегледът е тръгнал оттам."""
import json

from conftest import post_with_csrf


def _issue_cmr(admin_client, consignee_name="Клиент ЧМР"):
    resp = post_with_csrf(admin_client, "/cmr/new",
                          {"sender_name": "Тест", "consignee_name": consignee_name},
                          csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = resp.headers["Location"].rstrip("/").split("/")[-1]
    return int(doc_id)


# ---------------------------------------------------------------- скритото поле във формите

def test_new_document_form_has_empty_edit_doc_id_hidden_field(admin_client):
    body = admin_client.get("/cmr/new").data.decode()
    assert 'name="edit_doc_id" value=""' in body


def test_edit_form_has_edit_doc_id_hidden_field_populated_with_the_real_id(admin_client):
    doc_id = _issue_cmr(admin_client)
    body = admin_client.get("/doc/%d/edit" % doc_id).data.decode()
    assert 'name="edit_doc_id" value="%d"' % doc_id in body


def test_all_nine_document_type_forms_carry_the_edit_doc_id_field(admin_client):
    """И шестте самостоятелни *_form.html, И трите фактури (споделен макрос
    invoice_submit_buttons в _invoice_macros.html) — заявката засяга всички
    типове документи еднакво, не само ЧМР."""
    for path in ("/cmr/new", "/dualuse/new", "/export-it/new", "/packing/new",
                "/pallet/new", "/waybill/new", "/invoice-br/new",
                "/invoice-no/new", "/invoice-dubai/new"):
        body = admin_client.get(path).data.decode()
        assert 'name="edit_doc_id"' in body, "missing on %s" % path


# ---------------------------------------------------------------- edit_doc_id никога не пада в самите данни

def test_edit_doc_id_is_never_saved_as_part_of_the_document_data(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Тест", "consignee_name": "Клиент", "edit_doc_id": "999",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    con = db_module.get_db()
    saved = json.loads(con.execute("SELECT data FROM documents WHERE id = ?", (doc_id,)).fetchone()[0])
    con.close()
    assert "edit_doc_id" not in saved, (
        "edit_doc_id leaked into the saved document's data JSON: %r" % saved
    )


# ---------------------------------------------------------------- новото издаване (без edit) — непроменено поведение

def test_new_document_preview_back_link_still_points_to_the_new_form(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/preview",
                          {"sender_name": "Тест", "consignee_name": "Нов Клиент"},
                          csrf_source_url="/cmr/new", follow_redirects=False)
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    body = admin_client.get("/preview/%s" % token).data.decode()
    assert 'href="/cmr/new?restore=%s"' % token in body
    assert "/doc/" not in body.split('href="/cmr/new?restore=')[0][-50:]


# ---------------------------------------------------------------- редакция + преглед + връщане — самата поправка

def test_edit_then_preview_back_link_points_to_edit_document_not_new(admin_client):
    doc_id = _issue_cmr(admin_client)
    resp = post_with_csrf(admin_client, "/cmr/preview", {
        "sender_name": "Тест", "consignee_name": "РЕДАКТИРАН Клиент",
        "edit_doc_id": str(doc_id),
    }, csrf_source_url="/doc/%d/edit" % doc_id, follow_redirects=False)
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    body = admin_client.get("/preview/%s" % token).data.decode()
    assert 'href="/doc/%d/edit?restore=%s"' % (doc_id, token) in body
    assert 'href="/cmr/new?restore=%s"' % token not in body


def test_edit_document_restore_shows_in_progress_values_not_saved_ones(admin_client):
    """Основната поправка: GET /doc/<id>/edit?restore=<token> вече предзарежда
    стойностите от прегледа (все още незаписани), не старите от базата."""
    doc_id = _issue_cmr(admin_client, consignee_name="Оригинален Клиент")
    resp = post_with_csrf(admin_client, "/cmr/preview", {
        "sender_name": "Тест", "consignee_name": "НЕЗАПИСАН Клиент В Процес",
        "edit_doc_id": str(doc_id),
    }, csrf_source_url="/doc/%d/edit" % doc_id, follow_redirects=False)
    token = resp.headers["Location"].rsplit("/", 1)[-1]

    body = admin_client.get("/doc/%d/edit?restore=%s" % (doc_id, token)).data.decode()
    assert "НЕЗАПИСАН Клиент В Процес" in body
    assert "Оригинален Клиент" not in body
    # все още сме в режим редакция — бутонът е "Запази промените", не "Издай".
    assert "Запази промените" in body


def test_edit_document_restore_ignores_a_token_for_a_different_doc_type(admin_client):
    """Защитна проверка (огледална на _document_new): токен, съхранен за
    ДРУГ тип документ, не бива тихо да предзареди грешни/несъвместими
    полета в тази форма — payload[0] == doc_type проверката пази това."""
    doc_id = _issue_cmr(admin_client, consignee_name="Пазен Оригинал")
    # преглед на ПАЛЕТНА карта (различен doc_type), не свързан с това ЧМР
    resp = post_with_csrf(admin_client, "/pallet/preview", {
        "sender_name": "Тест", "client_name": "Друг Тип Документ",
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    token = resp.headers["Location"].rsplit("/", 1)[-1]

    body = admin_client.get("/doc/%d/edit?restore=%s" % (doc_id, token)).data.decode()
    assert "Пазен Оригинал" in body
    assert "Друг Тип Документ" not in body


def test_edit_document_without_restore_param_still_shows_the_saved_data(admin_client):
    """Обикновена редакция (без през преглед) не бива да се промени от тази
    поправка — /doc/<id>/edit БЕЗ ?restore= показва запазените данни."""
    doc_id = _issue_cmr(admin_client, consignee_name="Запазен В Базата")
    body = admin_client.get("/doc/%d/edit" % doc_id).data.decode()
    assert "Запазен В Базата" in body


def test_saving_after_preview_and_restore_updates_the_same_document_not_a_duplicate(admin_client):
    doc_id = _issue_cmr(admin_client, consignee_name="Оригинал")
    resp = post_with_csrf(admin_client, "/cmr/preview", {
        "sender_name": "Тест", "consignee_name": "Финално Съхранен",
        "edit_doc_id": str(doc_id),
    }, csrf_source_url="/doc/%d/edit" % doc_id, follow_redirects=False)
    token = resp.headers["Location"].rsplit("/", 1)[-1]

    # потребителят се връща към формата (вече предзаредена) и запазва.
    save_resp = post_with_csrf(admin_client, "/doc/%d/edit" % doc_id, {
        "sender_name": "Тест", "consignee_name": "Финално Съхранен",
    }, csrf_source_url="/doc/%d/edit?restore=%s" % (doc_id, token), follow_redirects=False)
    assert save_resp.status_code == 302
    assert save_resp.headers["Location"].rstrip("/").endswith("/doc/%d" % doc_id)

    body = admin_client.get("/docs").data.decode()
    assert body.count("Финално Съхранен") >= 1
    assert "Оригинал<" not in body  # старата стойност вече не е никъде видима
