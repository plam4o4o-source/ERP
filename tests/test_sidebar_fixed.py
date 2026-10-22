# -*- coding: utf-8 -*-
"""Заявка (22.10.2026): „страничното меню се измества при скролиране на
таблото, нека е статично“ — на компютър лентата е фиксирана към прозореца."""
import re

import pytest

from conftest import read_source


def test_desktop_sidebar_is_fixed_and_content_is_offset():
    css = read_source("static", "style.css")
    block = re.search(r"@media screen and \(min-width: 981px\) \{(.*?)\n\}", css, re.S)
    assert block, "липсва правилото за фиксирана лента на компютър"
    body = block.group(1)
    assert re.search(r"\.sidebar \{[^}]*position: fixed", body)
    assert re.search(r"\.content-area \{[^}]*margin-left: var\(--sidebar-w\)", body)


@pytest.mark.e2e
def test_sidebar_does_not_move_when_the_dashboard_scrolls(page, live_server, db_module):
    pytest.importorskip("playwright.sync_api")
    from conftest import e2e_login
    page.set_viewport_size({"width": 1920, "height": 1080})
    e2e_login(page, live_server)
    page.goto(live_server + "/")
    page.wait_for_load_state("networkidle")
    # Дълга страница, по-висока от .app-shell (както таблото при 1920×1080,
    # където sticky лентата се изместваше нагоре в дъното).
    page.evaluate("(() => { const d = document.createElement('div'); d.style.height = '1500px';"
                  " document.body.appendChild(d); })()")
    tops = []
    for y in (0, 300, 900, 99999):
        # html { scroll-behavior: smooth } — без анимация, за да се мери крайното място.
        page.evaluate("window.scrollTo({top: %d, behavior: 'instant'})" % y)
        page.wait_for_timeout(100)
        tops.append(page.evaluate("Math.round(document.querySelector('.sidebar').getBoundingClientRect().top)"))
    assert page.evaluate("window.scrollY") > 1000
    assert tops == [0, 0, 0, 0]
    # Съдържанието започва точно след лентата, без хоризонтално превъртане.
    left = page.evaluate("Math.round(document.querySelector('main').getBoundingClientRect().left)")
    width = page.evaluate("Math.round(document.querySelector('.sidebar').getBoundingClientRect().width)")
    assert left >= width
    assert page.evaluate("document.documentElement.scrollWidth") <= 1920
