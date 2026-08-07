# -*- coding: utf-8 -*-
"""Регресионни тестове за филтъра по диапазон от дати в списъка с
документи (routes_documents.documents, ?from=&to=) — заявка: „направи
всичко което предлагаш“ (списък с предложения за подобрения)."""
from datetime import date, timedelta

from conftest import post_with_csrf

_TODAY = date.today().isoformat()
_TOMORROW = (date.today() + timedelta(days=1)).isoformat()
_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _issue_cmr(client, consignee_name):
    resp = post_with_csrf(client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": consignee_name,
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302, resp.data


def test_documents_date_filter_includes_today(admin_client):
    _issue_cmr(admin_client, "Клиент За Днешен Филтър")
    resp = admin_client.get("/docs?from=%s&to=%s" % (_TODAY, _TODAY))
    assert resp.status_code == 200
    assert "Клиент За Днешен Филтър" in resp.data.decode()


def test_documents_date_filter_excludes_by_from(admin_client):
    _issue_cmr(admin_client, "Клиент Извън Диапазона")
    resp = admin_client.get("/docs?from=%s" % _TOMORROW)
    assert resp.status_code == 200
    assert "Клиент Извън Диапазона" not in resp.data.decode()


def test_documents_date_filter_excludes_by_to(admin_client):
    _issue_cmr(admin_client, "Клиент Преди Диапазона")
    resp = admin_client.get("/docs?to=%s" % _YESTERDAY)
    assert resp.status_code == 200
    assert "Клиент Преди Диапазона" not in resp.data.decode()


def test_documents_date_filter_combines_with_text_search(admin_client):
    _issue_cmr(admin_client, "Комбиниран Филтър ЕООД")
    resp = admin_client.get("/docs?q=Комбиниран&from=%s&to=%s" % (_TODAY, _TODAY))
    assert resp.status_code == 200
    assert "Комбиниран Филтър ЕООД" in resp.data.decode()
    resp2 = admin_client.get("/docs?q=Комбиниран&from=%s" % _TOMORROW)
    assert "Комбиниран Филтър ЕООД" not in resp2.data.decode()


def test_documents_search_form_shows_selected_date_values(admin_client):
    resp = admin_client.get("/docs?from=%s&to=%s" % (_YESTERDAY, _TODAY))
    body = resp.data.decode()
    assert 'name="from" value="%s"' % _YESTERDAY in body
    assert 'name="to" value="%s"' % _TODAY in body


def test_documents_group_toggle_links_preserve_date_filter(admin_client):
    resp = admin_client.get("/docs?from=%s&to=%s" % (_YESTERDAY, _TODAY))
    body = resp.data.decode()
    assert ("from=%s" % _YESTERDAY) in body
    assert ("to=%s" % _TODAY) in body


def test_documents_no_date_filter_shows_everything(admin_client):
    _issue_cmr(admin_client, "Клиент Без Филтър")
    resp = admin_client.get("/docs")
    assert "Клиент Без Филтър" in resp.data.decode()
