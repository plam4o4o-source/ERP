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
    assert cfg["gh_branch"] == "main"


def test_save_then_load_roundtrip(cfg_path):
    appconfig.save_config({"network_port": 8080, "gh_owner": "plam4o4o-source"})
    cfg = appconfig.load_config()
    assert cfg["network_port"] == 8080
    assert cfg["gh_owner"] == "plam4o4o-source"
    # Незададените ключове запазват стойностите по подразбиране.
    assert cfg["gh_branch"] == "main"


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


def test_gh_token_encrypted_on_disk(cfg_path):
    appconfig.save_config({"gh_token": "ghp_SuperSecretExample1234"})
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = f.read()
    # Токенът никога не стои в чист текст в самия конфигурационен файл.
    assert "ghp_SuperSecretExample1234" not in raw
    assert "enc:v1:" in raw


def test_gh_token_decrypted_when_loaded(cfg_path):
    appconfig.save_config({"gh_token": "ghp_SuperSecretExample1234"})
    cfg = appconfig.load_config()
    # Извикващият код (app.py) продължава да вижда чист текст в паметта.
    assert cfg["gh_token"] == "ghp_SuperSecretExample1234"


def test_gh_token_blank_keeps_existing_via_load_then_save(cfg_path):
    # Възпроизвежда логиката в app.py: system_settings подава празно поле
    # "keep unchanged", извикващият код merge-ва с текущата (декриптирана)
    # стойност преди save_config — тук проверяваме, че този цикъл работи.
    appconfig.save_config({"gh_token": "ghp_Original111"})
    current = appconfig.load_config()
    merged_token = "" or current.get("gh_token", "")
    appconfig.save_config({"gh_token": merged_token})
    assert appconfig.load_config()["gh_token"] == "ghp_Original111"


def test_other_fields_still_plaintext(cfg_path):
    appconfig.save_config({"gh_owner": "plam4o4o-source", "gh_token": "ghp_x"})
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = f.read()
    assert "plam4o4o-source" in raw  # само токенът се крие, не другите полета
