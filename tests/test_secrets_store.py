# -*- coding: utf-8 -*-
"""Тестове за secrets_store (шифроване на GitHub токена „в покой“, H4)."""
import os

import secrets_store


def _cfg_path(tmp_path):
    return os.path.join(str(tmp_path), "pacho_config.json")


def test_encrypt_then_decrypt_roundtrip(tmp_path):
    p = _cfg_path(tmp_path)
    token = "ghp_ExampleTokenValue1234567890"
    enc = secrets_store.encrypt(p, token)
    assert enc != token
    assert secrets_store.is_encrypted(enc)
    assert secrets_store.decrypt(p, enc) == token


def test_plaintext_not_stored_verbatim(tmp_path):
    p = _cfg_path(tmp_path)
    token = "super-secret-value"
    enc = secrets_store.encrypt(p, token)
    assert token not in enc


def test_empty_value_passes_through(tmp_path):
    p = _cfg_path(tmp_path)
    assert secrets_store.encrypt(p, "") == ""
    assert secrets_store.decrypt(p, "") == ""


def test_already_encrypted_value_not_double_encrypted(tmp_path):
    p = _cfg_path(tmp_path)
    enc = secrets_store.encrypt(p, "abc")
    enc2 = secrets_store.encrypt(p, enc)
    assert enc2 == enc


def test_unrecognized_plaintext_value_returned_as_is_on_decrypt(tmp_path):
    p = _cfg_path(tmp_path)
    # Обратна съвместимост: конфигурация от преди тази промяна има чист
    # текст без нашия префикс — decrypt трябва да го върне непроменен, не
    # да гръмне.
    assert secrets_store.decrypt(p, "ghp_oldplaintexttoken") == "ghp_oldplaintexttoken"


def test_key_file_created_with_restrictive_permissions(tmp_path):
    p = _cfg_path(tmp_path)
    secrets_store.encrypt(p, "abc")
    key_path = p + ".key"
    assert os.path.exists(key_path)
    mode = os.stat(key_path).st_mode & 0o777
    # На POSIX системи очакваме само собственикът да може да чете/пише.
    if os.name != "nt":
        assert mode == 0o600


def test_decrypt_fails_gracefully_with_wrong_key(tmp_path):
    p = _cfg_path(tmp_path)
    enc = secrets_store.encrypt(p, "abc")
    # Симулираме различен/загубен ключ (напр. .key файлът е бил пресъздаден).
    os.remove(p + ".key")
    with open(p + ".key", "wb") as f:
        from cryptography.fernet import Fernet
        f.write(Fernet.generate_key())
    assert secrets_store.decrypt(p, enc) == ""


def test_different_secrets_produce_different_ciphertext(tmp_path):
    # Регресия срещу "two-time pad": същият (config_path, key), различни
    # стойности -> различен ciphertext всеки път (Fernet включва случаен
    # nonce), не просто XOR с фиксиран keystream.
    p = _cfg_path(tmp_path)
    a = secrets_store.encrypt(p, "стойност-1")
    b = secrets_store.encrypt(p, "стойност-1")
    assert a != b  # различен nonce всеки път, дори за еднакъв plaintext
    assert secrets_store.decrypt(p, a) == "стойност-1"
    assert secrets_store.decrypt(p, b) == "стойност-1"
