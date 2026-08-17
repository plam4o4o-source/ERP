# -*- coding: utf-8 -*-
"""Регресионни тестове за icons.render_icon — одит (12.08.2026, находка
№31): преди тази поправка модулът нямаше НИТО ЕДИН тест — нито happy path
(позната икона), нито fallback пътя за непозната икона (`_ICONS.get(name,
"")` — връща празно `<svg>` тяло вместо грешка, тих UI дефект без
regression пазач)."""
from markupsafe import Markup

import icons


def test_render_icon_known_name_returns_non_empty_svg():
    html = icons.render_icon("plus")
    assert isinstance(html, Markup)
    assert "<svg" in html
    assert "</svg>" in html
    # Тялото на познатата икона не е празно между отварящия и затварящия таг.
    inner = html.split(">", 1)[1].rsplit("</svg>", 1)[0]
    assert inner.strip() != ""


def test_render_icon_unknown_name_does_not_raise_and_returns_empty_body():
    """Одит (находка №31): _ICONS.get(name, "") — непозната икона НЕ
    гърми, но връща SVG с празно тяло (тих UI дефект — липсваща икона на
    екрана, без грешка никъде). Тестът документира ТОЧНО това поведение,
    за да не се промени случайно/незабелязано занапред."""
    html = icons.render_icon("напълно-измислено-име-на-икона-което-не-съществува")
    assert isinstance(html, Markup)
    assert "<svg" in html
    assert "</svg>" in html
    inner = html.split(">", 1)[1].rsplit("</svg>", 1)[0]
    assert inner == ""


def test_render_icon_respects_size():
    html = icons.render_icon("plus", size=32)
    assert 'width="32"' in html
    assert 'height="32"' in html


def test_render_icon_default_size_is_18():
    html = icons.render_icon("plus")
    assert 'width="18"' in html
    assert 'height="18"' in html


def test_render_icon_applies_css_class_when_given():
    html = icons.render_icon("plus", cls="my-icon")
    assert 'class="my-icon"' in html


def test_render_icon_no_class_attribute_when_cls_omitted():
    html = icons.render_icon("plus")
    assert "class=" not in html


def test_render_icon_output_is_markup_safe_not_escaped_in_template():
    """Markup (не обикновен str) — Jinja НЕ escape-ва резултата при
    вграждане в шаблон (иначе '<svg>' би излязло като escaped текст
    '&lt;svg&gt;' на страницата)."""
    html = icons.render_icon("plus")
    assert str(Markup.escape(str(html))) != str(html)  # съдържа реални '<'/'>' символи


def test_all_declared_icons_have_non_empty_body():
    """Всяка декларирана в _ICONS икона реално има съдържание — хваща
    случайно оставена празна стойност при добавяне на нова икона."""
    for name in icons._ICONS:
        html = icons.render_icon(name)
        inner = html.split(">", 1)[1].rsplit("</svg>", 1)[0]
        assert inner.strip() != "", "иконата %r има празно тяло" % name
