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
