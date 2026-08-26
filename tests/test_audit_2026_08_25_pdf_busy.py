# -*- coding: utf-8 -*-
"""Одит (25.08.2026, находка №5): „PDF опашката е заета“ е ОТДЕЛНО състояние.

Преди това изчакването на реда за PDF (`_render_lock` таймаут) вдигаше гол
RuntimeError ВЪТРЕ в обвивката за reportlab грешки — значи:
  * логваше се като срив с пълен traceback (шум за нещо съвсем нормално);
  * текстът „опитайте пак след няколко секунди“ се преобличаше в общото
    „PDF генерирането е неуспешно“;
  * операторът виждаше тревожното „съобщете на администратор“ за временно
    натоварване.

Сега заетата опашка е PdfBusyError (подклас на RuntimeError, за да е
съвместим), излита чиста и маршрутът ѝ показва спокойно предупреждение.
"""
import pytest

import pdf_export


def test_pdf_busy_error_is_runtimeerror_subclass():
    """Съвместимост: всеки стар `except RuntimeError` все още я хваща."""
    assert issubclass(pdf_export.PdfBusyError, RuntimeError)


def test_busy_queue_raises_distinct_class_and_is_not_logged_as_crash(flask_app, monkeypatch):
    monkeypatch.setattr(pdf_export, "_RENDER_LOCK_TIMEOUT", 0)
    logged = []
    monkeypatch.setattr(pdf_export.applog, "log_exception",
                        lambda *a, **k: logged.append(a))

    # Държим катинара зает от друга „нишка“ → следващият опит удря таймаута.
    assert pdf_export._render_lock.acquire(timeout=1)
    try:
        with flask_app.test_request_context("/"):
            with pytest.raises(pdf_export.PdfBusyError) as ei:
                pdf_export.generate_document_pdf(
                    "Заглавие", "1", "BC0001", [("Поле", "стойност")], [], [])
    finally:
        pdf_export._render_lock.release()

    # Текстът оцелява непреоблечен…
    assert "Опитайте отново" in str(ei.value)
    # …и НЕ е логнат като срив.
    assert logged == [], "заетата опашка не бива да минава през log_exception"


def test_busy_queue_shows_calm_warning_on_the_export_route(admin_client, monkeypatch):
    """През реалния маршрут: заета опашка → пренасочване + спокойно
    предупреждение, БЕЗ „съобщете на администратор“."""
    # Издаваме един документ, за да има какво да се сваля.
    from conftest import post_with_csrf
    import json as _json
    resp = post_with_csrf(admin_client, "/pallet/preview", {
        "client_name": "Клиент",
        "items_json": _json.dumps([{"code": "A", "description": "x", "qty": "1"}]),
    }, csrf_source_url="/pallet/new", follow_redirects=False)
    # Издаваме реално (не само преглед) — минаваме през формата за издаване.
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    issue = post_with_csrf(admin_client, "/pallet/new", {
        "client_name": "Клиент",
        "items_json": _json.dumps([{"code": "A", "description": "x", "qty": "1"}]),
    }, csrf_source_url="/preview/%s" % token, follow_redirects=False)
    assert issue.status_code == 302
    doc_id = int(issue.headers["Location"].rstrip("/").rsplit("/", 1)[-1])

    monkeypatch.setattr(pdf_export, "_RENDER_LOCK_TIMEOUT", 0)
    assert pdf_export._render_lock.acquire(timeout=1)
    try:
        r = admin_client.get("/doc/%d/export.pdf" % doc_id, follow_redirects=True)
    finally:
        pdf_export._render_lock.release()

    body = r.get_data(as_text=True)
    assert "Изчакайте няколко секунди" in body
    assert "съобщете на администратор" not in body
