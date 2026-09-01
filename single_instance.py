# -*- coding: utf-8 -*-
"""Гарантира, че на един компютър работи само ЕДНО копие на ПачоЛогистик.

Одит (31.08.2026, находка №12): нямаше нищо подобно. Първият старт на
компилирания .exe е бавен (PyInstaller разопакова, антивирусът сканира), а
прозорец още няма — типичното поведение на потребителя е да щракне пак.
Резултатът бяха два процеса, които:

* пускат миграциите на базата едновременно;
* въртят два обновяващи цикъла, борещи се за един и същ `<exe>.new`
  (това гарантира провала на `move` от находка №6);
* въртят два таймера за автоматичен архив;
* и — на Windows — И ДВАТА „успяват“ на порт 5000, защото пробата в
  `net.find_available_port` слагаше `SO_REUSEADDR` (виж поправката там).

Механизмът е катинар върху файл, не маркерен файл: операционната система
освобождава катинара сама, когато процесът си отиде — включително при
рязко спиране, срив или изключване на тока. Маркерен файл в същия случай
би останал и би блокирал програмата завинаги.
"""
import os
import re
import sys

import applog

LOCK_FILENAME = "pacho_logistic.lock"

_lock_file = None  # държи се жив нарочно — затварянето му освобождава катинара

#: Одит (01.09.2026, доуточнение на находка №12, установено при преглед на
#: v3.69.0): извлича порта от информативния ред в катинарния файл
#: (виж set_running_port по-долу).
_PORT_RE = re.compile(r"port=(\d+)")


def _default_dir():
    try:
        import config as appconfig
        return os.path.dirname(appconfig.CONFIG_PATH) or "."
    except Exception:
        return os.path.dirname(os.path.abspath(sys.argv[0])) or "."


def _try_lock(fileobj):
    if os.name == "nt":
        # msvcrt.locking заключва `nbytes` от ТЕКУЩАТА позиция — затова
        # изрично застава на байт 0 (същия байт заключва и вторият процес).
        import msvcrt
        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fileobj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def acquire(directory=None, filename=LOCK_FILENAME):
    """Връща True, ако това е единственото копие; False — ако вече работи друго.

    При каквато и да е неочаквана пречка (папка само за четене, екзотична
    файлова система без катинари) връща True: по-добре второ копие,
    отколкото програма, която отказва да стартира.
    """
    global _lock_file
    if _lock_file is not None:
        return True
    path = os.path.join(directory or _default_dir(), filename)
    try:
        # O_CREAT без O_TRUNC: файлът се създава при първи старт и после
        # само се преизползва — съдържанието му (pid) е чисто информативно.
        fileobj = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+")
    except OSError:
        applog.log_warning("single_instance.acquire",
                           "катинарът %s не може да се отвори — пропускам "
                           "проверката за второ копие" % path)
        return True
    try:
        _try_lock(fileobj)
    except OSError:
        # Заето от друг процес — точно случаят, който търсим.
        fileobj.close()
        return False
    except Exception:
        fileobj.close()
        applog.log_warning("single_instance.acquire",
                           "неочаквана грешка при заключване на %s — "
                           "пропускам проверката за второ копие" % path)
        return True
    _lock_file = fileobj
    try:
        fileobj.seek(1)
        fileobj.truncate()
        fileobj.write(" pid=%d\n" % os.getpid())
        fileobj.flush()
    except OSError:
        pass
    return True


def set_running_port(port):
    """Одит (01.09.2026, доуточнение на находка №12): записва РЕАЛНО
    използвания мрежов порт в катинарния файл — за да може второ стартиране
    (виж read_running_port по-долу) да отвори ПРАВИЛНИЯ адрес.

    Защо изобщо е нужно: app.py извиква acquire() ПРЕДИ да е избран реалният
    порт (single-instance проверката трябва да стане преди миграциите на
    базата — виж коментара в app.py), а `net.find_available_port` може да
    премести сървъра на друг порт, ако конфигурираният е зает от нещо друго
    (виж бележката „Забележка: порт … е зает“ там). Второто стартиране,
    отхвърлено от катинара, знаеше само КОНФИГУРИРАНИЯ порт
    (`appconfig.get_network_port`) — точно в сценария, в който двете стойности
    се различават, отваряше грешен адрес: конфигурирания (свободен, никой не
    слуша на него) вместо реалния (където първото копие действително работи).
    Извиква се само от процеса, който държи катинара (`_lock_file` е
    неговият, ако `acquire()` е върнала True) — второто стартиране никога не
    би могло да го извика смислено, затова тихо не прави нищо без катинар."""
    if _lock_file is None:
        return
    try:
        _lock_file.seek(1)
        _lock_file.truncate()
        _lock_file.write(" pid=%d port=%d\n" % (os.getpid(), port))
        _lock_file.flush()
    except OSError:
        pass


def read_running_port(directory=None, filename=LOCK_FILENAME):
    """Чете порта, записан от вече работещото копие (виж set_running_port).

    Одит (01.09.2026, поправка на собствена регресия — уловена от реален
    windows-latest CI, не от преглед на кода): първата версия четеше от
    НАЧАЛОТО на файла (`f.read()` от позиция 0). Байт 0 обаче е ИМЕННО
    заключеният от `msvcrt.locking` байт (виж `_try_lock`) — Windows налага
    МАНДАТОРНО заключване на байтовия обхват, за разлика от POSIX
    (`fcntl.flock`, само съвещателно): дори обикновено четене на този байт
    от КОЙТО И ДА Е handle (включително друг handle в СЪЩИЯ процес, камо ли
    в друг) гърми с грешка на достъпа. Затова функцията винаги връщаше None
    — точно обратното на предназначението ѝ. Сега четенето изрично прескача
    байт 0 (`seek(1)` ПРЕДИ `read()`, в двоичен режим — избягва и
    неяснотата на текстовите seek-отмествания на Windows при CRLF превод).

    Връща None при липсващ файл, стар формат без порт (записан от версия
    отпреди тази поправка), или каквато и да е грешка при четене —
    извикващият пада обратно на конфигурирания порт, точно както преди."""
    path = os.path.join(directory or _default_dir(), filename)
    try:
        with open(path, "rb") as f:
            f.seek(1)  # прескача заключения байт 0 — виж обяснението горе
            content = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    m = _PORT_RE.search(content)
    return int(m.group(1)) if m else None


def release():
    """Освобождава катинара (за пълнота и за тестовете; ОС го прави и сама)."""
    global _lock_file
    if _lock_file is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            _lock_file.seek(0)
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        _lock_file.close()
    except OSError:
        pass
    _lock_file = None
