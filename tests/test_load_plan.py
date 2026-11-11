# -*- coding: utf-8 -*-
"""Заявка (09.11.2026): „подредба на палети в камион/тир — и визуално да
има“ (load_plan.py, templates/load_plan.html)."""
import json
from urllib.parse import parse_qsl, urlsplit

import pytest
from werkzeug.datastructures import MultiDict

from conftest import post_with_csrf

ROW_KEYS = ("d", "q", "l", "w", "h", "kg", "st")


def _args(rows, vehicle="tent", **extra):
    pairs = [("v", vehicle)] + [(k, str(x)) for k, x in extra.items()]
    for row in rows:
        pairs += [(k, str(x)) for k, x in zip(ROW_KEYS, row)]
    return MultiDict(pairs)


def _plan(flask_app, rows, vehicle="tent", **extra):
    import load_plan
    with flask_app.test_request_context():
        args = _args(rows, vehicle, **extra)
        return load_plan.plan(load_plan.parse_lines(args), load_plan.resolve_vehicle(args))


# ---------------------------------------------------------------- подредба

def test_33_eur_pallets_fill_a_13_6_m_trailer_three_across(flask_app):
    r = _plan(flask_app, [("EUR", 33, 1200, 800, 1500, 500, 1)])
    assert r["placed_pallets"] == 33 and not r["unplaced"]
    assert r["ldm"] == pytest.approx(13.2)
    # 3 напряко (800 мм) в ред от 1200 мм.
    first_row = [it for it in r["placed"] if it["x"] == 0]
    assert len(first_row) == 3 and {it["along"] for it in first_row} == {1200}


def test_34_eur_crosswise_fit_only_on_the_edge_and_35_do_not(flask_app):
    """LKW Walter / Schmitz Cargobull: 34 EUR в 13 620 мм — 17 реда × 800 мм
    напречно, остават 20 мм; подредбата се показва с предупреждение."""
    r = _plan(flask_app, [("EUR", 34, 1200, 800, 1500, 500, 1)])
    assert r["placed_pallets"] == 34 and r["slack"] == 20
    assert any("на ръба" in w for w in r["warnings"])
    r = _plan(flask_app, [("EUR", 35, 1200, 800, 1500, 500, 1)])
    assert r["placed_pallets"] == 34
    assert [(len(u["stack"]), u["reason"]) for u in r["unplaced"]] == [(1, "space")]
    assert any("не се побират" in w for w in r["warnings"])
    # 33 EUR остават в обичайната подредба (3 надлъжно, 13,2 м) — без предупреждение.
    r = _plan(flask_app, [("EUR", 33, 1200, 800, 1500, 500, 1)])
    assert not any("на ръба" in w for w in r["warnings"])


def test_26_industrial_pallets_two_across(flask_app):
    r = _plan(flask_app, [("IND", 27, 1200, 1000, 1500, 500, 1)])
    assert r["placed_pallets"] == 26 and len(r["unplaced"]) == 1
    assert {it["across"] for it in r["placed"]} == {1200}


def test_stackable_pallets_go_two_high_and_halve_the_floor(flask_app):
    r = _plan(flask_app, [("EUR", 66, 1200, 800, 1200, 300, 2)])
    assert r["placed_pallets"] == 66 and r["stacks"] == 33
    assert all(len(it["stack"]) == 2 for it in r["placed"])


def test_heavier_pallet_never_goes_on_top(flask_app):
    r = _plan(flask_app, [("Леки", 1, 1200, 800, 1000, 200, 2), ("Тежки", 1, 1200, 800, 1000, 700, 2)])
    (only,) = r["placed"]
    assert [p["kg"] for p in only["stack"]] == [700, 200]


def test_no_stacking_when_not_allowed_too_tall_or_other_footprint(flask_app):
    # Единият не се стифира.
    r = _plan(flask_app, [("A", 1, 1200, 800, 1000, 500, 1), ("B", 1, 1200, 800, 1000, 400, 2)])
    assert r["stacks"] == 2
    # Двата заедно са по-високи от тента (2700 мм).
    r = _plan(flask_app, [("A", 2, 1200, 800, 1400, 500, 2)])
    assert r["stacks"] == 2
    # Различен отпечатък.
    r = _plan(flask_app, [("A", 1, 1200, 800, 1000, 500, 2), ("B", 1, 1200, 1000, 1000, 400, 2)])
    assert r["stacks"] == 2
    # Без височина — не се стифира.
    r = _plan(flask_app, [("A", 2, 1200, 800, "", 500, 2)])
    assert r["stacks"] == 2
    assert any("без височина" in w.lower() for w in r["warnings"])


def test_smaller_pallets_fill_the_gap_at_the_end_of_a_row(flask_app):
    # Индустриален напречно (1200 напряко) оставя 1280 мм — EUR (800 по дължина) влиза в същия ред.
    r = _plan(flask_app, [("IND", 1, 1200, 1000, 1000, 500, 1), ("EUR", 1, 1200, 800, 1000, 300, 1)])
    assert {it["x"] for it in r["placed"]} == {0}
    assert r["ldm"] == pytest.approx(1.0)


def test_pallet_bigger_than_the_body_is_reported(flask_app):
    r = _plan(flask_app, [("Голям", 1, 3000, 2600, 1000, 100, 1)])
    assert r["placed_pallets"] == 0 and r["unplaced"][0]["reason"] == "size"
    r = _plan(flask_app, [("Висок", 1, 1200, 800, 2800, 100, 1)])
    assert r["unplaced"][0]["reason"] == "size"
    r = _plan(flask_app, [("Висок", 1, 1200, 800, 2800, 100, 1)], vehicle="mega")
    assert r["placed_pallets"] == 1


def test_overweight_warning_and_centre_of_gravity(flask_app):
    r = _plan(flask_app, [("A", 10, 1200, 800, 1000, 900, 1)], vehicle="van")
    assert r["placed_kg"] <= 9000
    assert any("надвишава полезния товар" in w for w in r["warnings"])
    r = _plan(flask_app, [("A", 3, 1200, 800, 1000, 500, 1)])
    assert r["cog"] == pytest.approx(600)


def test_containers_mix_orientations_like_the_published_counts(flask_app):
    """2352 мм вътре: 3 EUR напряко (2400) не влизат — EUR надлъжно + EUR
    напречно (800 + 1200 = 2000 мм) дава 11 в 20' и 25 в 40'."""
    r = _plan(flask_app, [("EUR", 12, 1200, 800, 1500, 500, 1)], vehicle="c20")
    assert r["placed_pallets"] == 11
    assert {(it["along"], it["across"]) for it in r["placed"]} == {(1200, 800), (800, 1200)}
    r = _plan(flask_app, [("EUR", 26, 1200, 800, 1500, 500, 1)], vehicle="c40")
    assert r["placed_pallets"] == 25


def test_mixed_sizes_use_less_length_than_rows_only(flask_app):
    r = _plan(flask_app, [("Шкаф", 4, 2200, 800, 2000, 700, 1), ("Табла", 9, 1200, 800, 1100, 400, 1),
                          ("Каса", 5, 1000, 1000, 900, 300, 1)])
    assert r["placed_pallets"] == 18 and r["used_length"] <= 9400
    # Без застъпване и в рамките на каросерията.
    boxes = [(it["x"], it["y"], it["x"] + it["along"], it["y"] + it["across"]) for it in r["placed"]]
    for i, a in enumerate(boxes):
        assert a[2] <= 13600 and a[3] <= 2480
        for b in boxes[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


def test_ldm_by_area_and_weight_per_metre(flask_app):
    import load_plan
    r = _plan(flask_app, [("EUR", 3, 1200, 800, 1000, 600, 1)])
    assert r["ldm_area"] == pytest.approx(3 * 1.2 * 0.8 / 2.4)  # 0,4 LDM на EUR палет
    bins = load_plan.weight_bins(r["placed"], 13600)
    assert len(bins) == 14 and sum(b["kg"] for b in bins) == pytest.approx(1800)


def test_container_door_limits_the_pallet_height(flask_app):
    # 20': вътре 2 395 мм, но вратата е 2 292 мм (Hapag-Lloyd).
    r = _plan(flask_app, [("Висок", 1, 1200, 800, 2350, 500, 1)], vehicle="c20")
    assert r["placed_pallets"] == 0 and r["unplaced"][0]["reason"] == "door"
    r = _plan(flask_app, [("Висок", 1, 1200, 800, 2350, 500, 1)], vehicle="tent")
    assert r["placed_pallets"] == 1


def test_epal_stacking_and_safe_working_load(flask_app):
    # EPAL 6 (800×600): върху долния палет най-много 1 500 кг.
    r = _plan(flask_app, [("Половин", 3, 800, 600, 600, 450, 3)])
    assert sorted(len(it["stack"]) for it in r["placed"]) == [3]
    r = _plan(flask_app, [("Половин", 3, 800, 600, 600, 900, 3)])
    assert sorted(len(it["stack"]) for it in r["placed"]) == [1, 2]
    assert any("безопасния товар" in w for w in r["warnings"])  # 900 > 500 кг
    r = _plan(flask_app, [("EUR", 2, 1200, 800, 1000, 1200, 2)])
    assert not any("безопасния товар" in w for w in r["warnings"])


def test_container_half_rule_and_side_gap_note(flask_app):
    r = _plan(flask_app, [("Тежък", 2, 1100, 1100, 1000, 6000, 1), ("Лек", 4, 1200, 800, 1000, 100, 1)],
              vehicle="c40")
    assert r["front_share"] > 0.6
    assert any("60%" in w for w in r["warnings"])
    r = _plan(flask_app, [("EUR", 1, 1200, 800, 1000, 500, 1)])
    assert any("над 8 см" in n for n in r["notes"])
    r = _plan(flask_app, [("EUR", 33, 1200, 800, 1000, 500, 1)])
    assert not any("над 8 см" in n for n in r["notes"])  # 3 × 800 = 2 400 от 2 480 мм


def test_custom_vehicle_dimensions(flask_app):
    import load_plan
    with flask_app.test_request_context():
        v = load_plan.resolve_vehicle(MultiDict({"v": "custom", "vl": "6000", "vw": "2400",
                                                 "vh": "2000", "vkg": "5000"}))
        assert (v["length"], v["width"], v["height"], v["payload"], v["custom"]) == (6000, 2400, 2000, 5000, True)
        bad = load_plan.resolve_vehicle(MultiDict({"v": "custom", "vl": "abc", "vw": "-5"}))
        assert bad["length"] == 13620 and bad["width"] == 2480
    r = _plan(flask_app, [("EUR", 20, 1200, 800, 1000, 100, 1)], vehicle="custom",
              vl=6000, vw=2400, vh=2000, vkg=5000)
    assert r["placed_pallets"] == 15  # 5 реда × 3


def test_parse_lines_is_tolerant_and_capped(flask_app):
    import load_plan
    rows = [("A", "2", "1 200", "800", "1000", "450,5", "9"), ("", "", "", "", "", "", "1"),
            ("B", "x", "1200", "800", "", "", "1")]
    with flask_app.test_request_context():
        lines = load_plan.parse_lines(_args(rows))
        assert len(lines) == 2  # напълно празният ред се пропуска
        assert lines[0]["length"] == 1200 and lines[0]["weight"] == 450.5 and lines[0]["stack"] == 3
        assert lines[0]["ok"] and not lines[1]["ok"]
        many = load_plan.parse_lines(_args([("A", 1, 1200, 800, 1000, 1, 1)] * 80))
        assert len(many) == load_plan.MAX_LINES


# ---------------------------------------------------------------- страницата

def test_page_without_data_shows_only_the_form(admin_client):
    body = admin_client.get("/load-plan").get_data(as_text=True)
    assert "data-load-plan" in body and "lp-svg" not in body


def test_page_draws_3d_top_and_weight_views(admin_client):
    args = _args([("Табла", 10, 1200, 800, 1100, 650, 2), ("Шкаф", 2, 1200, 1000, 1900, 900, 1)])
    resp = admin_client.get("/load-plan", query_string=list(args.items(multi=True)))
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert body.count('<svg class="lp-svg') == 3
    assert "3D изглед" in body and "Изглед отгоре" in body and "Тегло по дължината" in body
    assert "data-lp-range" in body and 'data-no="1"' in body
    assert "Ред на товарене" in body and "Кабина" in body and "Врати" in body
    assert "×2" in body  # стифиран куп
    assert "data-print" in body


def test_employee_can_use_the_page_and_menu_has_it(employee_client):
    body = employee_client.get("/load-plan").get_data(as_text=True)
    assert 'href="/load-plan"' in body


def test_page_requires_login(client):
    assert client.get("/load-plan").status_code == 302


def _issue_packing(admin_client):
    items = json.dumps([
        {"packing": "pallet", "description": "Табла", "qty": "5", "length": "1200", "width": "800",
         "height": "1100", "gross": "650"},
        {"packing": "pallet", "description": "Табла", "qty": "5", "length": "1200", "width": "800",
         "height": "1100", "gross": "650"},
        {"packing": "pallet", "description": "Шкаф", "qty": "1", "length": "1200", "width": "1000",
         "height": "1900", "gross": "900"},
        {"packing": "", "description": "без размери", "qty": "1"},
    ])
    resp = post_with_csrf(admin_client, "/packing/new", {"receiver_name": "Получател ЕООД",
                                                         "items_json": items},
                          csrf_source_url="/packing/new", follow_redirects=False)
    assert resp.status_code == 302
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def test_packing_list_view_has_the_button_and_opens_the_plan(admin_client):
    doc_id = _issue_packing(admin_client)
    view = admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert "/doc/%d/load-plan" % doc_id in view
    resp = admin_client.get("/doc/%d/load-plan" % doc_id)
    assert resp.status_code == 302
    query = parse_qsl(urlsplit(resp.headers["Location"]).query)
    assert ("src", str(doc_id)) in query
    rows = [v for k, v in query if k == "q"]
    assert sorted(rows) == ["1", "2"]  # еднаквите редове са слети, редът без размери — пропуснат
    page = admin_client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "lp-svg" in page and "Опаковъчен лист" in page


def _pallet(client, order_no, height="110", pallet_type="EUR"):
    data = {"client_name": "Клиент ЕООД", "gross": "300", "height": height, "pallet_type": pallet_type,
            "items_format": "orders",
            "items_json": json.dumps([{"order_no": order_no, "pos": "10", "reference": "R",
                                       "reference_desc": "M", "qty": "1"}])}
    resp = post_with_csrf(client, "/pallet/new", data, csrf_source_url="/pallet/new")
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])


def test_pallet_card_plan_takes_all_cards_of_the_shipment(admin_client):
    first = _pallet(admin_client, "4500077001")
    _pallet(admin_client, "4500077001")
    resp = admin_client.get("/doc/%d/load-plan" % first)
    query = parse_qsl(urlsplit(resp.headers["Location"]).query)
    assert [v for k, v in query if k == "q"] == ["2"]
    assert ("l", "1200") in query and ("w", "800") in query and ("h", "1100") in query
    assert ("kg", "300") in query


def test_cmr_plan_uses_the_ldm_lines_without_related_documents(admin_client):
    resp = post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Друг ЕООД",
        "ldm_lines": json.dumps([{"type": "eur", "qty": "12", "stack": "2"}]),
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    loc = admin_client.get("/doc/%d/load-plan" % doc_id).headers["Location"]
    query = parse_qsl(urlsplit(loc).query)
    assert ("q", "12") in query and ("l", "1200") in query and ("st", "2") in query


def test_plan_from_unsupported_document_is_404(admin_client, db_module):
    resp = post_with_csrf(admin_client, "/waybill/new", {"consignee_name": "Получател ЕООД"},
                          csrf_source_url="/waybill/new", follow_redirects=False)
    doc_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
    assert admin_client.get("/doc/%d/load-plan" % doc_id).status_code == 404
    assert "data-load-plan-link" not in admin_client.get("/doc/%d" % doc_id).get_data(as_text=True)
    assert admin_client.get("/doc/999999/load-plan").status_code == 404


# ---------------------------------------------------------------- e2e

@pytest.mark.e2e
@pytest.mark.parametrize("width", [1366, 390])
def test_load_plan_page_in_the_browser(page, live_server, width):
    pytest.importorskip("playwright.sync_api")
    from conftest import e2e_login
    page.set_viewport_size({"width": width, "height": 900})
    e2e_login(page, live_server)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(live_server + "/load-plan")
    page.click("[data-lp-add]")
    page.select_option("[data-lp-rows] tr:last-child [data-lp-preset]", "1200x1000")
    assert page.input_value("[data-lp-rows] tr:last-child [data-lp-l]") == "1200"
    assert page.input_value("[data-lp-rows] tr:last-child [data-lp-w]") == "1000"
    page.fill("[data-lp-rows] tr:first-child input[name=q]", "20")
    page.fill("[data-lp-rows] tr:first-child input[name=h]", "1000")
    page.fill("[data-lp-rows] tr:first-child input[name=kg]", "300")
    page.select_option("[data-lp-rows] tr:first-child select[name=st]", "2")
    page.fill("[data-lp-rows] tr:last-child input[name=q]", "4")
    page.select_option("[data-lp-vehicle]", "custom")
    assert page.is_visible("[data-lp-custom]")
    page.select_option("[data-lp-vehicle]", "tent")
    page.click("form[data-load-plan] button[type=submit]")
    page.wait_for_selector(".lp-svg")
    assert page.locator(".lp-svg").count() == 3
    # Стъпка по стъпка: след 3 стъпки палет № 4 е „бъдещ“ (полупрозрачен).
    page.fill("[data-lp-range]", "3")
    page.dispatch_event("[data-lp-range]", "input")
    assert "is-future" in page.get_attribute(".lp-box[data-no='4']", "class")
    assert "is-future" not in page.get_attribute(".lp-box[data-no='3']", "class")
    assert page.inner_text("[data-lp-out]").strip() == "3 / 14"
    assert page.locator(".lp-box").count() == 14  # 10 купа EUR ×2 + 4 индустриални
    assert "24 / 24" in page.inner_text(".lp-tiles").replace("\n", " ")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert not errors
