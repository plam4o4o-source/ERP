# -*- coding: utf-8 -*-
"""Тестове за сравнението на версии в updater.parse_version.

Тази логика решава дали да се предложи автоматично обновяване, затова
коректното семантично сравнение е критично (напр. 3.10.0 > 3.9.0).
"""
import updater


def test_parses_simple_version():
    assert updater.parse_version("3.10.0") == (3, 10, 0)


def test_strips_v_prefix():
    assert updater.parse_version("v3.10.0") == (3, 10, 0)
    assert updater.parse_version("V2.1.3") == (2, 1, 3)


def test_numeric_not_lexical_ordering():
    # Класически капан: като низове "3.9.0" > "3.10.0", но семантично е обратното.
    assert updater.parse_version("3.10.0") > updater.parse_version("3.9.0")
    assert (updater.parse_version("3.2.0") > updater.parse_version("3.10.0")) is False


def test_invalid_version_is_lowest():
    assert updater.parse_version("не-е-версия") == (0,)
    assert updater.parse_version(None) == (0,)


def test_equal_versions_not_greater():
    assert (updater.parse_version("3.10.0") > updater.parse_version("3.10.0")) is False


def test_patch_bump_detected():
    assert updater.parse_version("1.0.1") > updater.parse_version("1.0.0")
