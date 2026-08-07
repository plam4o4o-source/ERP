# -*- coding: utf-8 -*-
"""Регресионни тестове за „Обобщено табло/статистика“
(routes_dashboard._month_bounds, _dashboard_stats) — заявка: „направи
всичко което предлагаш“ (списък с предложения за подобрения)."""
from datetime import date

from conftest import post_with_csrf
from routes_dashboard import _dashboard_stats, _month_bounds


def test_month_bounds_normal_month():
    start, next_start = _month_bounds(date(2026, 3, 17))
    assert start == date(2026, 3, 1)
    assert next_start == date(2026, 4, 1)


def test_month_bounds_december_rollover():
    start, next_start = _month_bounds(date(2026, 12, 25))
    assert start == date(2026, 12, 1)
    assert next_start == date(2027, 1, 1)


def _issue_cmr(client, consignee_name):
    resp = post_with_csrf(client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": consignee_name,
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data


def test_dashboard_stats_counts_current_month_documents(admin_client, con):
    _issue_cmr(admin_client, "Клиент За Статистика ЕООД")
    _issue_cmr(admin_client, "Клиент За Статистика ЕООД")
    stats = _dashboard_stats(con)
    assert stats["month_count"] >= 2


def test_dashboard_stats_excludes_previous_month_from_current(admin_client, con):
    _issue_cmr(admin_client, "Клиент Стар Документ ЕООД")
    con.execute(
        "UPDATE documents SET created_at = '2020-01-15 10:00'"
        " WHERE data LIKE '%Клиент Стар Документ ЕООД%'"
    )
    con.commit()
    stats = _dashboard_stats(con, today=date(2020, 2, 10))
    assert stats["month_count"] == 0
    assert stats["prev_month_count"] == 1


def test_dashboard_stats_top_clients_ranks_by_document_count(admin_client, con):
    _issue_cmr(admin_client, "Топ Клиент ЕООД")
    _issue_cmr(admin_client, "Топ Клиент ЕООД")
    _issue_cmr(admin_client, "Слаб Клиент ЕООД")
    stats = _dashboard_stats(con)
    names = [name for name, _cnt in stats["top_clients"]]
    assert "Топ Клиент ЕООД" in names
    top_idx = names.index("Топ Клиент ЕООД")
    if "Слаб Клиент ЕООД" in names:
        assert top_idx < names.index("Слаб Клиент ЕООД")


def test_dashboard_page_shows_monthly_stats_card(admin_client):
    _issue_cmr(admin_client, "Клиент За Таблото ЕООД")
    resp = admin_client.get("/")
    body = resp.data.decode()
    assert resp.status_code == 200
    assert "Статистика за текущия месец" in body
    assert "Най-активни клиенти този месец" in body
    assert "Клиент За Таблото ЕООД" in body
