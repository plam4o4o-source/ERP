# -*- coding: utf-8 -*-
"""Шифроване „в покой“ (at rest) на чувствителни настройки в pacho_config.json.

Бележка (25.08.2026): единственият досегашен потребител на този модул беше
GitHub Personal Access Token-ът (`gh_token`) в config.py. GitHub
синхронизацията беше премахната по заявка на потребителя, затова в момента
модулът НЕ се ползва от работния код — запазен е като самостоятелна, тествана
помощна функция (encrypt/decrypt/is_encrypted върху път), готова за повторна
употреба, ако отново потрябва шифроване на тайна в конфигурацията.

МОДЕЛ НА ДОВЕРИЕ: ключът за шифроване се пази в отделен файл (`.key` до
самата конфигурация) — същият модел, който кодът вече ползва за секретния
ключ на Flask сесиите (виж db.get_secret_key). Това НЕ е еквивалент на
истинска ОС интеграция (Windows DPAPI/Credential Manager, macOS Keychain):
който има достъп и до двата файла (config + .key), пак може да прочете
токена. Но премахва явния plaintext от pacho_config.json — при случайно
споделяне/архивиране/екранна снимка САМО на конфигурационния файл, токенът
вече не е четим. Истинска DPAPI интеграция е добра следваща стъпка (виж
ПЛАН_ЗА_РАЗРАБОТКА.md, находка H4) и може да замести само този модул, без
да променя формата на pacho_config.json (все така string стойност на
"gh_token" — просто криптирана вместо чист текст).

Шифроването е Fernet (симетрично, автентикирано — AES-128-CBC + HMAC-SHA256,
от добре одитираната библиотека `cryptography`), не самоделна крипто схема.
"""
import os

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:v1:"


def _key_path(config_path):
    return config_path + ".key"


def _load_or_create_key(config_path):
    path = _key_path(config_path)
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = f.read().strip()
            if key:
                return key
    key = Fernet.generate_key()
    with open(path, "wb") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # напр. файлова система без POSIX права — файлът пак не е в git
    return key


def is_encrypted(value):
    return bool(value) and value.startswith(_PREFIX)


def encrypt(config_path, plaintext):
    """Връща стойност за запис в конфигурационния файл. Празен низ и вече
    криптирани стойности (напр. подадени обратно непроменени) минават
    без промяна, за да не се криптира два пъти."""
    if not plaintext or is_encrypted(plaintext):
        return plaintext
    key = _load_or_create_key(config_path)
    token = Fernet(key).encrypt(plaintext.encode("utf-8"))
    return _PREFIX + token.decode("ascii")


def decrypt(config_path, value):
    """Връща истинската (plaintext) стойност за употреба в паметта.
    Стойности без нашия префикс се връщат както са (обратна съвместимост
    с вече съществуващи, все още нешифровани конфигурационни файлове от
    инсталации отпреди тази версия)."""
    if not is_encrypted(value):
        return value
    key = _load_or_create_key(config_path)
    raw = value[len(_PREFIX):].encode("ascii")
    try:
        return Fernet(key).decrypt(raw).decode("utf-8")
    except (InvalidToken, ValueError):
        # Ключът липсва/е различен (напр. .key файлът е изтрит или преместен
        # отделно от конфигурацията) — токенът не може да се възстанови.
        # По-добре „изгубен“ токен (администраторът ще го въведе наново),
        # отколкото грешка, която спира цялото приложение при стартиране.
        return ""
