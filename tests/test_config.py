# -*- coding: utf-8 -*-
"""Тестове за bootstrap конфигурацията (config.py).

Изолация: пренасочваме CONFIG_PATH към временен файл, за да не пипаме
реалния `pacho_config.json` на разработчика.
"""
import json
import os

import pytest

import config as appconfig


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = os.path.join(str(tmp_path), "pacho_config.json")
    monkeypatch.setattr(appconfig, "CONFIG_PATH", p)
    return p


def test_defaults_when_no_file(cfg_path):
    cfg = appconfig.load_config()
    assert cfg["db_path"] == ""
    assert cfg["network_mode"] is False
    assert cfg["network_port"] == 5000


def test_save_then_load_roundtrip(cfg_path):
    appconfig.save_config({"network_port": 8080, "network_mode": True})
    cfg = appconfig.load_config()
    assert cfg["network_port"] == 8080
    assert cfg["network_mode"] is True
    # Незададените ключове запазват стойностите по подразбиране.
    assert cfg["db_path"] == ""


def test_save_merges_and_preserves_unicode(cfg_path):
    appconfig.save_config({"db_path": r"Z:\ПачоЛогистик\pacho_logistic.db"})
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = f.read()
    # ensure_ascii=False => кирилицата се записва четимо, не като \uXXXX.
    assert "ПачоЛогистик" in raw


def test_corrupt_config_falls_back_to_defaults(cfg_path):
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("{ това не е валиден json ")
    cfg = appconfig.load_config()  # не хвърля грешка
    assert cfg["network_port"] == 5000


def test_resolve_db_path_default(tmp_path, cfg_path):
    base = str(tmp_path)
    resolved = appconfig.resolve_db_path(base)
    assert resolved == os.path.join(base, "pacho_logistic.db")


def test_resolve_db_path_custom(tmp_path, cfg_path):
    appconfig.save_config({"db_path": "/mnt/share/pacho.db"})
    resolved = appconfig.resolve_db_path(str(tmp_path))
    assert resolved == "/mnt/share/pacho.db"


# Бележка (25.08.2026): тестовете за gh_token (криптиране/декриптиране на
# GitHub токена в pacho_config.json) отпаднаха заедно с премахнатата
# синхронизация с GitHub. Общата криптираща помощна функция е тествана
# самостоятелно в tests/test_secrets_store.py.
