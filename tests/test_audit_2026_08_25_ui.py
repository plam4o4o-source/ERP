# -*- coding: utf-8 -*-
"""Одит (25.08.2026), находки №7–№10 — механизмите на UI/печат поправките.

Бързи проверки без браузър (по модела на другите одитни тестове): пазят
конкретния механизъм на всяка поправка да не бъде премахнат при бъдещо
разместване. Реалното визуално поведение (напр. пренасянето на дълъг низ в
ЧМР) е за e2e слоя; тук пазим, че „винтчетата“ са налице.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ №7
def test_live_search_redirects_to_login_on_expired_session():
    """Изтекла сесия по време на живо търсене → пълно пренасочване към входа,
    вместо тих стар списък на привидно логната страница."""
    js = _read("static", "app.js")
    # Проверката за пренасочване към /login трябва да е в блока на живото
    # търсене (около присвояването на results.innerHTML).
    assert re.search(r"r\.redirected && /\\/login", js), \
        "живото търсене не разпознава пренасочване към /login (находка №7)"
    assert "window.location.href = r.url" in js


# ------------------------------------------------------------------ №8
def test_live_search_screenreader_announcer_exists():
    base = _read("templates", "base.html")
    assert 'id="live-search-announce"' in base, "липсва скритата жива област (находка №8)"
    assert 'aria-live="polite"' in base
    css = _read("static", "style.css")
    assert ".visually-hidden" in css, "липсва sr-only класът за живата област"
    js = _read("static", "app.js")
    assert "announceLiveSearch" in js, "app.js не съобщава обновяването на резултатите"
    assert 'getElementById("live-search-announce")' in js


# ------------------------------------------------------------------ №9
def test_mobile_touch_targets_cover_more_than_just_btn_small():
    """Находка №34 вдигна само .btn-small до 44px; №9 е непокритата
    половина — полета, падащи менюта, филтърни етикети, иконни бутони."""
    css = _read("static", "style.css")
    # Изолираме блока за тесни екрани (≤700px), в който живее touch-правилото.
    idx = css.find("@media (max-width: 700px)")
    assert idx != -1
    block = css[idx: css.find("@media", idx + 10) if css.find("@media", idx + 10) != -1 else len(css)]
    assert "min-height: 44px" in block
    # Освен .btn-small — и формовите контроли, и филтърните етикети.
    assert "select, textarea" in block, "полетата/менютата не са вдигнати до 44px (находка №9)"
    assert ".filter-chip { min-height: 44px" in block
    assert ".searchbar-clear" in block and ".btn-icon" in block


# ------------------------------------------------------------------ №10
def test_cmr_value_cells_break_long_unbroken_tokens():
    """Дълъг непрекъснат низ в полетата на ЧМР трябва да се пренася, не да
    изпълзява извън клетката и да се застъпва със съседното поле."""
    css = _read("static", "style.css")
    m = re.search(r"\.cmr-grid \.box \.val \{[^}]*\}", css, re.S)
    assert m, "липсва правилото за .cmr-grid .box .val"
    assert "word-break: break-word" in m.group(0), \
        "стойностите в ЧМР не пренасят дълги непрекъснати низове (находка №10)"
    assert "overflow-wrap: break-word" in m.group(0)
    # И клетките с описанието на стоката (поле 9).
    assert re.search(r"\.cmr-grid \.goods td \{[^}]*word-break: break-word", css)


def test_cmr_preview_still_renders_with_a_long_value(admin_client):
    """Реална проверка, че шаблонът не е счупен от промяната: дълга
    непрекъсната референция минава през прегледа и се показва."""
    from conftest import post_with_csrf
    long_token = "REF" + "0123456789" * 6
    resp = post_with_csrf(admin_client, "/cmr/preview", {
        "sender_name": "Изпращач", "consignee_name": "Получател",
        "consignee_address": long_token,
    }, csrf_source_url="/cmr/new", follow_redirects=False)
    assert resp.status_code == 302
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    body = admin_client.get("/preview/%s" % token).get_data(as_text=True)
    assert long_token in body
