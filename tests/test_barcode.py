# -*- coding: utf-8 -*-
"""Тестове за генератора на Code128-B баркодове (barcode128.code128_svg)."""
import re

import pytest

import barcode128


def test_returns_svg_string():
    svg = barcode128.code128_svg("CMR-2026-0001")
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "xmlns=\"http://www.w3.org/2000/svg\"" in svg


def test_contains_bars():
    svg = barcode128.code128_svg("ABC123")
    # Черните ленти се рисуват като <rect ... fill="#000"/>
    assert svg.count('fill="#000"') >= 1


def test_text_shown_by_default_and_can_be_hidden():
    with_text = barcode128.code128_svg("HELLO", show_text=True)
    without_text = barcode128.code128_svg("HELLO", show_text=False)
    assert "<text" in with_text
    assert "<text" not in without_text


def test_responsive_uses_percentage_width():
    fixed = barcode128.code128_svg("X", responsive=False)
    responsive = barcode128.code128_svg("X", responsive=False)
    resp = barcode128.code128_svg("X", responsive=True)
    assert 'width="100%"' in resp
    assert 'width="100%"' not in fixed
    # viewBox се запазва и в двата случая (за пропорционално смаляване).
    assert "viewBox=" in resp and "viewBox=" in fixed


def test_non_ascii_raises_value_error():
    with pytest.raises(ValueError):
        barcode128.code128_svg("Кирилица")


def test_control_chars_below_range_raise():
    with pytest.raises(ValueError):
        barcode128.code128_svg("A\tB")  # tab е под ASCII 32


def test_checksum_changes_with_content():
    # Различно съдържание => различен модел на лентите (различна дължина/подредба).
    a = barcode128.code128_svg("AAAA", show_text=False)
    b = barcode128.code128_svg("AAAB", show_text=False)
    assert a != b


def test_viewbox_dimensions_are_positive_integers():
    svg = barcode128.code128_svg("PAL-2026-0007")
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert m is not None
    w, h = int(m.group(1)), int(m.group(2))
    assert w > 0 and h > 0


def test_xml_special_chars_are_escaped_not_injected():
    """Регресионен тест за затягането на bandit бариерата в CI (B704) —
    „<“, „>“, „&“, кавичките и апострофът са валидни ASCII 32-126 знаци
    (позволени от Code128-B), затова code128_svg трябва да ги екранира
    преди да ги вгради в SVG, а не да ги предаде сурово — иначе съдържание
    като '"><script>...' (стигащо тук напр. през /barcode/<code>.svg, вижте
    routes_dashboard.barcode_svg, където `code` идва направо от адреса)
    би се вмъкнало като истински маркъп в страницата, не показано като
    текст (XSS)."""
    payload = "\"><script>alert(1)</script>"
    svg = barcode128.code128_svg(payload)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert "&quot;&gt;" in svg


def test_ampersand_and_quotes_escaped_in_text_and_label():
    svg = barcode128.code128_svg("A&B'C\"D")
    assert "A&B'C\"D" not in svg
    assert "A&amp;B&apos;C&quot;D" in svg
