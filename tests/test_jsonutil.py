# -*- coding: utf-8 -*-
"""Тестове за jsonutil.dumps_for_inline_script (поправка на stored XSS, H2)."""
import json

import jsonutil


def test_valid_json_roundtrip():
    data = [{"id": 1, "name": "Клиент АД", "address": "ул. Тест 1"}]
    raw = jsonutil.dumps_for_inline_script(data)
    # \uXXXX поредиците са валидни в JS/JSON низови литерали — трябва да се
    # декодират обратно към същите данни, ако прочетем резултата като JSON
    # (JSON.parse и json.loads третират < еднакво с буквалния символ).
    assert json.loads(raw) == data


def test_escapes_script_close_tag():
    evil_name = "</script><script>alert(1)</script>"
    data = [{"id": 1, "name": evil_name}]
    raw = jsonutil.dumps_for_inline_script(data)
    assert "</script>" not in raw
    assert "<script>" not in raw
    # Но семантично си остава същото име след декодиране.
    assert json.loads(raw)[0]["name"] == evil_name


def test_escapes_ampersand_and_quote():
    data = [{"name": "Иванов & Синове", "note": "so-called 'client'"}]
    raw = jsonutil.dumps_for_inline_script(data)
    assert "&" not in raw
    assert "'" not in raw
    parsed = json.loads(raw)
    assert parsed[0]["name"] == "Иванов & Синове"
    assert parsed[0]["note"] == "so-called 'client'"


def test_empty_list():
    assert jsonutil.dumps_for_inline_script([]) == "[]"


def test_cyrillic_preserved_readable():
    data = [{"name": "Пламен"}]
    raw = jsonutil.dumps_for_inline_script(data)
    # ensure_ascii=False — кирилицата остава четима, не \uXXXX escape-и.
    assert "Пламен" in raw
