# -*- coding: utf-8 -*-
"""Одит (25.08.2026, находка №2): активните филтри трябва да са ВЪТРЕ в
контейнера, който живото търсене подменя (#docs-results / #invoices-results).

Регресията, която пазим: ако обобщението с етикетите застоява извън
контейнера, живото търсене обновява списъка, но етикетът „Тип/Вид: …“ виси
със СТАРАТА стойност на другите филтри в своя remove-адрес → реална промяна
кои документи вижда операторът.
"""
import re


def _issue_one_doc(admin_client):
    """Издава един прост документ, за да има какво да се филтрира/показва."""
    # Не е строго нужно за проверката на подредбата на етикетите — те
    # зависят само от заявените филтри в URL — но държи страницата „жива“.
    return None


def _results_block(body, container_id):
    """Връща само HTML-а ВЪТРЕ в <div id=container_id …> … (до края)."""
    idx = body.find('id="%s"' % container_id)
    assert idx != -1, "контейнерът #%s липсва" % container_id
    return body[idx:]


def test_docs_filter_chips_inside_live_container(admin_client):
    body = admin_client.get("/docs?type=cmr&from=2026-01-01&to=2026-12-31").get_data(as_text=True)
    # Обобщението с активните филтри трябва да е ВЪТРЕ в #docs-results.
    inside = _results_block(body, "docs-results")
    assert "searchbar-summary" in inside, (
        "обобщението с филтрите не е вътре в #docs-results — ще застоява при живо търсене")
    # А не и извън него (преди контейнера).
    before = body[: body.find('id="docs-results"')]
    assert "searchbar-summary" not in before, (
        "обобщението с филтрите стои и ИЗВЪН #docs-results")
    # „Изчисти“ е безусловен (виж лентата) дори с активни филтри.
    assert "Изчисти" in body


def test_docs_clear_button_unconditional_without_filters(admin_client):
    body = admin_client.get("/docs").get_data(as_text=True)
    assert "Изчисти" in body, "„Изчисти“ трябва да е винаги наличен (иначе застоява при живо търсене)"


def test_invoices_filter_chips_inside_live_container(admin_client):
    body = admin_client.get("/invoices?type=invoice_br&from=2026-01-01&to=2026-12-31").get_data(as_text=True)
    inside = _results_block(body, "invoices-results")
    assert "searchbar-summary" in inside, (
        "обобщението с филтрите не е вътре в #invoices-results — ще застоява при живо търсене")
    before = body[: body.find('id="invoices-results"')]
    assert "searchbar-summary" not in before, (
        "обобщението с филтрите стои и ИЗВЪН #invoices-results")


def test_invoices_clear_button_unconditional_without_filters(admin_client):
    body = admin_client.get("/invoices").get_data(as_text=True)
    assert "Изчисти" in body
