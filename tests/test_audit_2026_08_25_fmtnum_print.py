# -*- coding: utf-8 -*-
"""Одит (25.08.2026, находка №4): десетичният разделител на ПЕЧАТНИТЕ бланки.

fmt_num беше приложен само на фактурите (находка №44). Опаковъчният лист,
палетната карта, товарителницата и ЧМР продължаваха да печатат суровия
текст: ред с въведени „2,5“ и изчислена колона „3.00“ излизаше на официалния
документ със запетая и точка едновременно.

Проверката е РЕАЛНА: минава през същия маршрут за преглед (/…/preview →
/preview/<token>), който потребителят вижда при „Печат/PDF“, с въведени
запетайни стойности, и изисква те да излязат с точка.
"""
import json

from conftest import post_with_csrf


def _preview_html(admin_client, url, form, source):
    resp = post_with_csrf(admin_client, url, form, csrf_source_url=source,
                          follow_redirects=False)
    assert resp.status_code == 302, "прегледът трябва да пренасочи към /preview/<token>"
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    return admin_client.get("/preview/%s" % token).get_data(as_text=True)


def test_packing_print_normalizes_decimal_separator(admin_client):
    items = [{"packing": "кашон", "description": "стока",
              "qty": "2,5", "length": "1,1", "width": "2,2", "height": "3,3",
              "volume": "1,20", "net": "4,4", "gross": "5,5"}]
    body = _preview_html(admin_client, "/packing/preview", {
        "receiver_name": "Получател",
        "items_json": json.dumps(items, ensure_ascii=False),
        "total_volume": "1,20", "total_net": "4,4", "total_gross": "5,5",
    }, "/packing/new")
    for dotted in ("2.5", "1.20", "4.4", "5.5", "1.1", "2.2", "3.3"):
        assert dotted in body, "липсва нормализирана стойност %s" % dotted
    for comma in ("2,5", "1,20", "4,4", "5,5", "3,3"):
        assert comma not in body, "запетайна стойност %s е излязла на бланката" % comma


def test_pallet_print_normalizes_decimal_separator(admin_client):
    items = [{"code": "A1", "description": "стока", "qty": "2,5", "weight": "3,7"}]
    body = _preview_html(admin_client, "/pallet/preview", {
        "client_name": "Клиент",
        "items_json": json.dumps(items, ensure_ascii=False),
        "gross": "9,9", "height": "14,5",
    }, "/pallet/new")
    for dotted in ("2.5", "3.7", "9.9", "14.5"):
        assert dotted in body, "липсва нормализирана стойност %s" % dotted
    for comma in ("2,5", "3,7", "9,9", "14,5"):
        assert comma not in body, "запетайна стойност %s е излязла на бланката" % comma


def test_waybill_print_normalizes_decimal_separator(admin_client):
    items = [{"description": "стока", "packing": "палет", "marks": "—",
              "weight": "2,5", "qty": "3,7"}]
    body = _preview_html(admin_client, "/waybill/preview", {
        "consignee_name": "Получател",
        "items_json": json.dumps(items, ensure_ascii=False),
    }, "/waybill/new")
    for dotted in ("2.5", "3.7"):
        assert dotted in body
    for comma in ("2,5", "3,7"):
        assert comma not in body


def test_cmr_print_normalizes_decimal_separator(admin_client):
    body = _preview_html(admin_client, "/cmr/preview", {
        "sender_name": "Изпращач", "consignee_name": "Получател",
        "weight": "2,5", "volume": "1,20",
    }, "/cmr/new")
    assert "2.5" in body
    assert "1.20" in body
    assert "2,5" not in body
    assert "1,20" not in body
