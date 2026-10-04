# -*- coding: utf-8 -*-
"""Лого на фирмата изпращач — качва се от „⚙ Настройки“ → „Фирма изпращач“
и се показва на всички печатни документи.

Пази се като обикновен файл до базата данни (НЕ в static/) — защото при
компилираната .exe версия static/ се разопакова във временна папка на
PyInstaller при всяко стартиране и не е трайна за запис между отделните
пускания на програмата. Файлът до базата данни, както самата база, оцелява
през рестарти и обновявания.
"""
import errno
import os
import tempfile

import applog
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
    на .png). Хвърля LogoError (ValueError) с преводимо съобщение при
    проблем. Трие предишно лого с друго разширение, ако имаше такова.

    Одит (04.10.2026, R3): записът е АТОМАРЕН. Досега старото лого се
    триеше ПРЕДИ записа, а новото се пишеше направо под крайното име — при
    пълен диск оставаше орязан файл (напр. 8192 байта), който /logo.img
    сервираше на всяка печатна бланка, а старото лого вече го нямаше. Сега:
    временен файл в същата папка → fsync → проверка на размера и на самото
    изображение (Pillow) → os.replace върху крайното име; старото лого с
    ДРУГО разширение се трие чак след успешната подмяна. При грешка на
    диска остава старото лого непокътнато, а временният файл се изтрива.

    Одит (04.10.2026, I3): съобщенията са преводими (db.TranslatableError) —
    маршрутът ги показва на езика на интерфейса."""
    data = file_storage.read()
    if not data:
        raise LogoError(db.N_("Файлът е празен."))
    if len(data) > MAX_SIZE:
        raise LogoError(db.N_("Файлът е твърде голям (макс. 3MB)."))
    ext = _detect_ext(data[:8])
    if ext is None:
        raise LogoError(db.N_("Файлът не е разпознато изображение (приемат се PNG, JPG или GIF)."))

    base = _base_dir()
    path = os.path.join(base, "company_logo.%s" % ext)
    # Името на временния файл НЕ започва с „company_logo.“ — logo_path(),
    # архивът и възстановяването не го бъркат с истинско лого.
    fd, tmp_path = tempfile.mkstemp(prefix=".logo_upload_", suffix=".tmp", dir=base)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.getsize(tmp_path) != len(data):
            raise OSError(errno.EIO, "непълен запис на логото")
        _verify_image(tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    for other in _ALLOWED_EXT:
        old = os.path.join(base, "company_logo.%s" % other)
        if old != path and os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                applog.log_exception("branding.save_logo: старото лого %s не е изтрито" % old)
    return path


def _verify_image(path):
    """Pillow проверява структурата на файла (за PNG — всички парчета и
    контролните им суми до IEND, т.е. и орязан файл), без да декодира
    пикселите (затова и без риск от „декомпресионна бомба“)."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
    except Exception as exc:
        raise LogoError(db.N_("Файлът не е валидно изображение — повреден или непълен "
                              "(приемат се PNG, JPG или GIF).")) from exc


class LogoError(db.TranslatableValueError):
    """Отказано лого — съобщението е преводимо (виж db.TranslatableError)."""


def remove_logo():
    for ext in _ALLOWED_EXT:
        p = os.path.join(_base_dir(), "company_logo.%s" % ext)
        if os.path.exists(p):
            os.remove(p)
