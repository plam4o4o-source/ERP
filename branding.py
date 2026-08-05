# -*- coding: utf-8 -*-
"""Лого на фирмата изпращач — качва се от „⚙ Настройки“ → „Фирма изпращач“
и се показва на всички печатни документи.

Пази се като обикновен файл до базата данни (НЕ в static/) — защото при
компилираната .exe версия static/ се разопакова във временна папка на
PyInstaller при всяко стартиране и не е трайна за запис между отделните
пускания на програмата. Файлът до базата данни, както самата база, оцелява
през рестарти и обновявания.
"""
import os

import db

_ALLOWED_EXT = ("png", "jpg", "jpeg", "gif")
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
MAX_SIZE = 3 * 1024 * 1024  # 3MB — логото е малка картинка, не снимка с висока резолюция

_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}


def _base_dir():
    return os.path.dirname(db.DB_PATH)


def logo_path():
    """Пътят до текущото лого, или None ако няма качено."""
    base = _base_dir()
    for ext in _ALLOWED_EXT:
        p = os.path.join(base, "company_logo.%s" % ext)
        if os.path.exists(p):
            return p
    return None


def logo_mimetype(path):
    ext = path.rsplit(".", 1)[-1].lower()
    return _MIME.get(ext, "application/octet-stream")


def _detect_ext(head):
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    return None


def save_logo(file_storage):
    """Записва качен файл като логото на фирмата, след проверка че реално
    е изображение — по магическите байтове в началото на файла, НЕ само
    по разширението му (за да не се приема произволен файл, преименуван
    на .png). Хвърля ValueError с ясно съобщение при проблем. Трие
    предишно лого с друго разширение, ако имаше такова."""
    data = file_storage.read()
    if not data:
        raise ValueError("Файлът е празен.")
    if len(data) > MAX_SIZE:
        raise ValueError("Файлът е твърде голям (макс. 3MB).")
    ext = _detect_ext(data[:8])
    if ext is None:
        raise ValueError("Файлът не е разпознато изображение (приемат се PNG, JPG или GIF).")

    remove_logo()
    path = os.path.join(_base_dir(), "company_logo.%s" % ext)
    with open(path, "wb") as f:
        f.write(data)
    return path


def remove_logo():
    for ext in _ALLOWED_EXT:
        p = os.path.join(_base_dir(), "company_logo.%s" % ext)
        if os.path.exists(p):
            os.remove(p)
