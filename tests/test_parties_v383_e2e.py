# -*- coding: utf-8 -*-
"""Заявка (06.11.2026, група PARTIES, v3.83.0) — в истински браузър: избор на
превозвач от регистъра в ЧМР, „последният превозвач за клиента“ (без да
презаписва ръчно въведеното), бърз запис на превозвач, бутонът VIES (заявката
към VIES е подменена) и страниците без хоризонтално превъртане (1366/390)."""
import pytest

from conftest import e2e_login

pytestmark = pytest.mark.e2e


def _seed(db_module):
    con = db_module.get_db()
    client_id = con.execute(
        "INSERT INTO clients (name, city, country, vat) VALUES ('ABB Italia', 'Milano', 'Италия',"
        " 'IT01234567890')").lastrowid
    carrier_id = con.execute(
        "INSERT INTO carriers (name, vat, address, postcode, city, country) VALUES"
        " ('Транс Експрес', 'BG175074752', 'ул. Шипка 12', '5300', 'Габрово', 'България')").lastrowid
    con.execute("INSERT INTO carrier_vehicles (carrier_id, kind, plate, position) VALUES"
                " (?, 'truck', 'EB1234AB', 1), (?, 'trailer', 'EB9012EE', 2)", (carrier_id, carrier_id))
    con.execute("INSERT INTO carrier_drivers (carrier_id, name, phone, position) VALUES"
                " (?, 'Иван Иванов', '0888', 1)", (carrier_id,))
    con.commit()
    con.close()
    return client_id, carrier_id


def test_cmr_carrier_picker_and_last_carrier_for_client(page, live_server, db_module):
    client_id, carrier_id = _seed(db_module)
    e2e_login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.select_option("#f-carrier-select", str(carrier_id))
    assert page.input_value("#f-carrier") == \
        "Транс Експрес, ул. Шипка 12, 5300 Габрово, България, VAT: BG175074752"
    assert page.input_value("#f-truck_reg") == "EB1234AB"
    assert page.input_value("#f-trailer_reg") == "EB9012EE"
    assert page.input_value("#f-driver") == "Иван Иванов"
    assert page.input_value("input[name=carrier_id]") == str(carrier_id)
    page.select_option("#f-client-select-cmr", str(client_id))
    page.fill("input[name=pallet_eur_given]", "33")
    page.click('#main-doc-form button[type=submit]:not([formaction])')
    page.wait_for_url(live_server + "/doc/*")
    assert page.locator('a[href$="/transport-order"]').is_visible()

    # Ново ЧМР за същия клиент — празните полета се попълват сами.
    page.goto(live_server + "/cmr/new")
    page.select_option("#f-client-select-cmr", str(client_id))
    page.wait_for_function("document.querySelector('[data-carrier-memory]').hidden === false")
    assert page.input_value("#f-truck_reg") == "EB1234AB"
    assert page.input_value("input[name=carrier_id]") == str(carrier_id)
    assert page.input_value("#f-carrier-select") == str(carrier_id)
    assert "0001/" in page.inner_text("[data-carrier-memory]")

    # Ръчно въведен превозвач НЕ се презаписва — само предложение с „Попълни“.
    page.goto(live_server + "/cmr/new")
    page.fill("#f-carrier", "Ръчен превозвач ООД")
    page.select_option("#f-client-select-cmr", str(client_id))
    page.wait_for_function("document.querySelector('[data-carrier-memory]').hidden === false")
    assert page.input_value("#f-carrier") == "Ръчен превозвач ООД"
    assert page.input_value("input[name=carrier_id]") == ""
    assert "Попълни" in page.inner_text("[data-carrier-memory]")
    page.click("[data-carrier-memory] button")
    assert page.input_value("#f-carrier").startswith("Транс Експрес")
    assert page.input_value("#f-truck_reg") == "EB1234AB"
    assert page.input_value("input[name=carrier_id]") == str(carrier_id)


def test_cmr_quick_add_carrier(page, live_server, db_module):
    e2e_login(page, live_server)
    page.goto(live_server + "/cmr/new")
    page.fill("#f-carrier", "Бърз Транс ООД, Русе")
    page.fill("#f-truck_reg", "P 1234 AA")
    page.click("[data-carrier-quick-open]")
    assert page.input_value("#cq-name") == "Бърз Транс ООД"
    page.fill("#cq-vat", "BG111")
    page.press("#cq-vat", "Enter")          # Enter не издава ЧМР-то
    assert "/cmr/new" in page.url
    page.click("[data-carrier-quick-save]")
    page.wait_for_function("document.querySelector('input[name=carrier_id]').value !== ''")
    assert page.input_value("#f-carrier") == "Бърз Транс ООД, Русе", "ръчният текст остава"
    con = db_module.get_db()
    row = con.execute("SELECT c.name, c.vat, v.plate FROM carriers c JOIN carrier_vehicles v"
                      " ON v.carrier_id = c.id").fetchone()
    con.close()
    assert tuple(row) == ("Бърз Транс ООД", "BG111", "P 1234 AA")


def test_vies_button_on_client_card(page, live_server, db_module, monkeypatch):
    import vies
    calls = []

    def fake(url, payload, timeout):
        calls.append(payload)
        return 200, {"valid": True, "name": "ABB S.P.A.", "address": "VIA ROMA 1\n20100 MILANO",
                     "requestIdentifier": "R1"}
    monkeypatch.setattr(vies, "_http_post_json", fake)
    client_id, _carrier = _seed(db_module)
    e2e_login(page, live_server)
    page.goto(live_server + "/clients/%d/edit" % client_id)
    assert calls == []
    page.click("[data-vies-btn]")
    page.wait_for_selector(".vies-badge.is-valid")
    assert "ABB S.P.A." in page.inner_text("[data-vies-result]")
    assert calls == [{"countryCode": "IT", "vatNumber": "01234567890"}]
    page.reload()
    assert "ABB S.P.A." in page.inner_text("[data-vies-result]"), "резултатът е кеширан"
    page.fill("#f-vat", "IT99999999999")
    assert "друг номер" in page.inner_text("[data-vies-note]")


@pytest.mark.parametrize("width", [1366, 390])
def test_parties_pages_have_no_horizontal_scroll(e2e_context_factory, live_server, db_module, width):
    client_id, carrier_id = _seed(db_module)
    pg = e2e_context_factory(viewport={"width": width, "height": 900}).new_page()
    e2e_login(pg, live_server)
    pg.goto(live_server + "/cmr/new")
    pg.select_option("#f-carrier-select", str(carrier_id))
    pg.select_option("#f-client-select-cmr", str(client_id))
    pg.fill("input[name=pallet_eur_given]", "5")
    pg.click('#main-doc-form button[type=submit]:not([formaction])')
    pg.wait_for_url(live_server + "/doc/*")
    doc_url = pg.url
    for path in ("/carriers", "/carriers/new", "/carriers/%d/edit" % carrier_id, "/pallets",
                 "/pallets/party?kind=client&key=c%d" % client_id, "/carriers/settings",
                 doc_url.replace(live_server, "") + "/transport-order",
                 "/clients/%d/edit" % client_id):
        pg.goto(live_server + path)
        sw = pg.evaluate("document.documentElement.scrollWidth")
        assert sw <= width, (path, sw)
