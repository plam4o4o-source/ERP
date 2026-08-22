# -*- coding: utf-8 -*-
"""Тестове за единната лента за търсене и живото филтриране.

Заявка на потребителя: „подобри графично лентите за търсене“ + избор „живо
търсене, докато пишете“.

До тази промяна всеки екран имаше СВОЙ вид търсене — „Адресна книга“ дори
с гол браузърен input без клас. Тестовете тук пазят три неща:

  1. всички екрани ползват ЕДИН И СЪЩ компонент (иначе следващият екран пак
     ще си направи свой);
  2. търсенето работи БЕЗ JavaScript (обикновена GET форма) — живото
     филтриране е удобство, не предпоставка;
  3. контейнерът с резултатите, който JS подменя, съществува и има точно
     онзи идентификатор, който формата сочи — разминаване тук би счупило
     живото търсене напълно безшумно.
"""
import re

import pytest

from conftest import post_with_csrf


#: (адрес, идентификатор на контейнера с резултати)
SEARCH_PAGES = [
    ("/docs", "docs-results"),
    ("/clients", "clients-results"),
    ("/invoices", "invoices-results"),
    ("/invoices/clients", "invoice-clients-results"),
    ("/materials", "materials-results"),
]


@pytest.mark.parametrize("url, results_id", SEARCH_PAGES)
def test_every_search_page_uses_the_shared_component(admin_client, url, results_id):
    """Един компонент навсякъде: икона вътре в полето, бутон за изчистване,
    и `data-live-search`, сочещ контейнера с резултатите."""
    body = admin_client.get(url).get_data(as_text=True)
    assert 'class="searchbar' in body, "екранът не ползва общата лента за търсене"
    assert "searchbar-input" in body
    assert "searchbar-clear" in body, "липсва бутонът за изчистване"
    assert 'data-live-search="#%s"' % results_id in body


@pytest.mark.parametrize("url, results_id", SEARCH_PAGES)
def test_results_container_exists_with_the_id_the_form_points_at(admin_client, url, results_id):
    """Формата сочи `#<id>`; ако контейнерът липсва или има друг
    идентификатор, живото търсене спира да работи БЕЗ никаква грешка —
    точно затова има тест."""
    body = admin_client.get(url).get_data(as_text=True)
    assert 'id="%s"' % results_id in body
    assert "data-search-results" in body


@pytest.mark.parametrize("url", [u for u, _r in SEARCH_PAGES])
def test_search_still_works_without_javascript(admin_client, url):
    """Формата е обикновена GET форма — сървърът филтрира по `q` независимо
    от скрипта. Без това живото търсене би било единственият начин да се
    търси, което е крехко (изключен JS, стар браузър, грешка в скрипта)."""
    resp = admin_client.get(url, query_string={"q": "няма-такова-нещо-12345"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Стойността се връща обратно в полето, за да се вижда какво е търсено.
    assert "няма-такова-нещо-12345" in body


def test_search_input_carries_the_typed_value_back(admin_client, db_module):
    con = db_module.get_db()
    con.execute("INSERT INTO clients (name, city) VALUES ('Уникална Фирма ЕООД', 'Русе')")
    con.commit()
    con.close()

    body = admin_client.get("/clients", query_string={"q": "Уникална"}).get_data(as_text=True)
    assert "Уникална Фирма ЕООД" in body
    m = re.search(r'class="searchbar-input"[^>]*value="([^"]*)"', body)
    assert m and m.group(1) == "Уникална", "въведеното трябва да остане в полето"


def test_clear_button_is_only_marked_visible_when_something_is_typed(admin_client):
    """Бутонът „×“ се показва само при въведен текст — иначе виси празен
    кръг в полето. (JS го превключва после; тук проверяваме началното
    състояние, което идва от сървъра.)"""
    empty = admin_client.get("/clients").get_data(as_text=True)
    typed = admin_client.get("/clients", query_string={"q": "нещо"}).get_data(as_text=True)
    assert "searchbar-clear is-visible" not in empty
    assert "searchbar-clear is-visible" in typed


def test_active_filters_are_shown_as_removable_chips(admin_client):
    """Одит/UX: при зададен филтър операторът вижда „3 намерени“ и не
    разбира защо, ако е забравил дата от миналата седмица. Активните филтри
    се показват като етикети, всеки премахваем с един клик."""
    body = admin_client.get("/docs", query_string={"type": "cmr", "from": "2026-01-01"}).get_data(as_text=True)
    assert "filter-chip" in body
    assert "Активни филтри" in body


def test_removing_a_filter_chip_keeps_the_other_filters(admin_client):
    """Връзката на етикета трябва да маха САМО своя филтър — иначе
    „премахни“ на практика значи „изчисти всичко“."""
    body = admin_client.get("/docs", query_string={
        "type": "cmr", "from": "2026-01-01", "to": "2026-12-31"}).get_data(as_text=True)
    chips = re.findall(r'<a class="filter-chip" href="([^"]+)"', body)
    assert chips, "не бяха намерени етикети за активни филтри"
    type_chip = [h for h in chips if "type=" not in h]
    assert type_chip, "етикетът за тип трябва да води към адрес БЕЗ type="
    # ...но с останалите филтри непокътнати.
    assert any("from=2026-01-01" in h and "to=2026-12-31" in h for h in type_chip)


def test_document_type_gets_a_colour_class_for_scanning_the_list(admin_client, db_module):
    """Типът документ вече се разпознава по цвят от пръв поглед. Класът е
    `doc-type--<тип>` — CSS дава цвета, текстът остава водещ (важно за
    далтонисти и за печат)."""
    post_with_csrf(admin_client, "/cmr/new", {
        "sender_name": "Изпращач", "consignee_name": "Получател",
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    body = admin_client.get("/docs").get_data(as_text=True)
    assert 'class="doc-type doc-type--cmr"' in body


def test_search_bar_input_is_a_real_search_field(admin_client):
    """`type="search"` дава на браузъра/екранния четец правилната семантика
    (и клавиша Escape за изчистване). Полето трябва да е и без
    автодовършване — предложенията на браузъра закриват резултатите."""
    body = admin_client.get("/clients").get_data(as_text=True)
    m = re.search(r'<input[^>]*class="searchbar-input"[^>]*>', body)
    assert m, "полето за търсене не беше намерено"
    tag = m.group(0)
    assert 'type="search"' in tag
    assert 'autocomplete="off"' in tag
    assert "aria-label=" in tag, "полето няма етикет за екранен четец"
