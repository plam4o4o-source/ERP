# -*- coding: utf-8 -*-
"""Минимално структурирано логване на грешки, прихванати НАРОЧНО на места,
където приложението трябва да продължи да работи въпреки грешката (напр.
неуспешна фонова синхронизация с GitHub, неуспешен опит за проверка на
обновяване, спрян тунел за отдалечен достъп) — находка L1 от
ПЛАН_ЗА_РАЗРАБОТКА.md ("Тихи широки except-и ... скриват грешки; само
файлово пренасочен print, без структурирано логване").

Преди тези грешки бяха напълно тихи (`except Exception: pass`/`return`) —
намирането на причина за проблем на терен (напр. защо синхронизацията с
GitHub мълчаливо спира) изискваше възпроизвеждане на живо при клиента.
Сега се пишат на stdout с времева марка и кратък контекст.

Умишлено НЕ ползваме модула `logging` от стандартната библиотека — при
компилиран `.exe` без конзола (`--windowed`) stdout вече е пренасочен към
`ph_startup*.log` от app.py (виж коментара там), значи обикновен print()
стига до същия лог файл без допълнителна конфигурация на handler-и/
формати. Добавянето на `logging` тук би дублирало съществуващия механизъм
без реална полза.

Тези функции НИКОГА не хвърлят изключение нагоре и НЕ променят
съществуващото поведение (пътя на изпълнение) на местата, откъдето се
викат — само добавят диагностичен ред."""
import atexit
import collections
import datetime
import re
import threading
import time
import traceback


def log_exception(context):
    """Записва текущото изключение (вика се ВЪТРЕ в `except` блок, докато
    `sys.exc_info()` все още сочи към него) с времева марка и кратък
    контекст на случването. Безопасна за викане навсякъде — сама по себе
    си не може да хвърли изключение при нормални условия."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        print("[%s] %s:\n%s" % (ts, context, traceback.format_exc()))
    except Exception:  # nosec B110 -- логваща функция, НАРОЧНО никога не хвърля нагоре (виж docstring на модула)
        pass


def log_warning(context, message):
    """Записва предупредително съобщение (без активно изключение) със
    същия формат — за случаи като „процесът спря неочаквано", без
    `except` блок в момента на извикването."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        print("[%s] %s: %s" % (ts, context, message))
    except Exception:  # nosec B110 -- логваща функция, НАРОЧНО никога не хвърля нагоре (виж docstring на модула)
        pass


#: Одит (13.10.2026, S5): кога за последно е записано предупреждение по ключ
#: (log_warning_throttled) — повтаряща се грешка не пълни лога.
_throttle_lock = threading.Lock()
_throttle_last = {}


def log_warning_throttled(key, context, message, interval=600):
    """Като log_warning, но най-много веднъж на `interval` секунди за един
    ключ — за грешки, които иначе биха се повтаряли на всяка заявка (отчетът
    на копието, почистващи задачи). True, ако редът е записан."""
    now = time.monotonic()
    with _throttle_lock:
        last = _throttle_last.get(key)
        if last is not None and now - last < interval:
            return False
        _throttle_last[key] = now
    log_warning(context, message)
    return True


#: Одит (22.08.2026, находка №7): максимална дължина на `detail` в един
#: одитен запис — виж log_audit по-долу.
_MAX_DETAIL_CHARS = 2000


_CONTROL_CHARS_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]")


def _single_line(text):
    """Одит (03.09.2026, находка №12): свежда стойност до ЕДИН ред за
    дневника — виж log_audit."""
    if not text:
        return text
    return _CONTROL_CHARS_RE.sub(" ", str(text))


def log_audit(action, detail=""):
    """Одит (19.08.2026, находка №51): следа за ЧУВСТВИТЕЛНИТЕ действия —
    точно тези, които ще потрябват след инцидент („кой изтри този документ“,
    „кой нулира паролата на този служител“, „кога е сменен пътят на базата“).

    До тази поправка НИТО едно от тях не се записваше никъде: `applog` не
    се викаше нито веднъж в routes_admin, routes_auth, routes_documents,
    routes_clients или login_guard. При спор или съмнение за злоупотреба
    нямаше абсолютно никаква информация — само състоянието „сега“.

    Записва потребителя от текущата сесия автоматично, ако има такава.
    СЪЗНАТЕЛНО не записва самите данни на документа/клиента (те са в
    базата) — само КОЙ, КАКВО и върху КОЙ обект, за да не се превърне
    логът в неконтролирано копие на лични данни. Никога не приема
    пароли/токени като `detail`.

    Не хвърля изключения — одитният запис никога не бива да проваля самото
    действие, което описва."""
    who = "?"
    user_id = username = ip = None
    try:
        from flask import has_request_context, request, session
        if has_request_context():
            username = session.get("username")
            user_id = session.get("user_id")
            ip = request.remote_addr or "?"
            who = "%s(id=%s)" % (username or "анонимен", user_id if user_id is not None else "-")
            who += " от %s" % ip
            # Одит (26.09.2026, находка №6): при достъп през тунела
            # remote_addr е винаги 127.0.0.1 — добавяме истинския адрес по
            # СЪЩОТО правило за доверие като лимита на входа
            # (routes_auth.tunnel_client_ip), не по ново.
            try:
                import routes_auth
                tunnel_ip = routes_auth.tunnel_client_ip()
            except Exception:  # nosec B110 -- без тунелния адрес записът остава с remote_addr
                tunnel_ip = None
            if tunnel_ip:
                who += " (през тунела: %s)" % tunnel_ip
                ip = "%s (през тунела: %s)" % (ip, tunnel_ip)
    except Exception:  # nosec B110 -- извън заявка/без Flask контекст: пише се без „кой“
        pass
    # Одит (22.08.2026, находка №7): таван на дължината. Няма проверка кой
    # какво подава като `detail`, а находка №51 добави по 1-2 записа на ВСЯКО
    # влизане — един-единствен неограничен ред (напр. многомегабайтово поле
    # от повреден импорт) би раздул лога в папката на програмата.
    if detail and len(detail) > _MAX_DETAIL_CHARS:
        detail = detail[:_MAX_DETAIL_CHARS] + "… (съкратено)"
    # Одит (03.09.2026, находка №12): нови редове и управляващи знаци се
    # неутрализират. Част от `detail` идва от ВЪВЕДЕНОТО (номерът на фактура
    # е свободен текст), а редът се пише в ph_startup*.log с print().
    # Проверено с изпълнение: обикновен служител издава фактура с номер,
    # съдържащ „\n[дата] ОДИТ: колега(id=7) от 192.168.1.9 | изтрит документ
    # …“, после натиска „Поднови публичния достъп“ — и в дневника се появява
    # ред, неразличим по форма от истински, с чуждо име, чуждо IP и чуждо
    # действие. Точно при спор във фирмата — сценарият, заради който този
    # дневник изобщо съществува (находка №51) — той спираше да доказва
    # каквото и да е и можеше да насочи в грешна посока.
    action = _single_line(action)
    detail = _single_line(detail)
    log_warning("ОДИТ", "%s | %s%s" % (who, action, (" | %s" % detail) if detail else ""))
    # Одит (13.10.2026, S4): и в общата база (audit_log) — файлът остава.
    try:
        _queue_audit_row({
            "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": user_id if isinstance(user_id, int) else None,
            "username": _single_line(username) if username else None,
            "ip": ip, "machine": _machine(), "action": action or "", "detail": detail or None})
    except Exception:  # nosec B110 -- дневникът никога не проваля действието
        log_exception("applog.log_audit: записът за базата не е поставен в опашката")


# ---------------------------------------------------------------- одит в базата
# Одит (13.10.2026, S4): записът в audit_log е със СОБСТВЕНА кратка връзка и
# кратко изчакване (AUDIT_SYNC_TIMEOUT) — потребителското действие никога не
# чака дълго и не пада заради дневника. Заета база (друг компютър пише, или
# самата заявка още държи катинара си) → записът отива в опашка и ФОНОВА
# нишка опитва пак (по-дълго изчакване, няколко опита); накрая —
# предупреждение в лога (най-много веднъж на 10 мин.), записът остава само
# във файла. Старите записи (срок/таван — db.AUDIT_*) се махат заедно със
# записа, най-много веднъж на 6 часа за база.

AUDIT_SYNC_TIMEOUT = 0.2
_AUDIT_QUEUE_MAX = 2000
_AUDIT_RETRY_DELAYS = (0.5, 2.0, 5.0)
#: На колко секунди се махат старите записи (на процес и база).
_AUDIT_PRUNE_EVERY = 6 * 3600
_audit_cond = threading.Condition()
_audit_queue = collections.deque()
_audit_state = {"thread": None, "busy": False, "pruned": {}}


def _machine():
    try:
        import legacy_migration
        return legacy_migration.machine_name()
    except Exception:  # nosec B110 -- без името на компютъра
        return None


def _queue_audit_row(row):
    import db  # тук, не горе: db внася applog
    path = db.DB_PATH
    if not db.audit_ready(path):
        return  # базата не е отворена от този процес — записът остава във файла
    prune = _prune_due(path)
    try:
        db.write_audit_rows(path, [row], prune=prune, timeout=AUDIT_SYNC_TIMEOUT)
    except Exception:  # заета/недостъпна база — фонов нов опит
        _enqueue(path, row)
        if prune:
            _enqueue(path, None)


def _prune_due(path):
    """Почистването на старите записи — най-много веднъж на _AUDIT_PRUNE_EVERY
    за база (две бързи DELETE по индекс, заедно със самия запис)."""
    now = time.monotonic()
    with _audit_cond:
        last = _audit_state["pruned"].get(path)
        if last is not None and now - last < _AUDIT_PRUNE_EVERY:
            return False
        _audit_state["pruned"][path] = now
        return True


def _enqueue(path, row):
    with _audit_cond:
        if len(_audit_queue) >= _AUDIT_QUEUE_MAX:
            _audit_queue.popleft()  # най-старият пада (във файла го има)
            log_warning_throttled("audit_queue_full", "applog.log_audit",
                                  "опашката за одитния дневник е пълна — най-старите "
                                  "записи остават само във файла")
        _audit_queue.append((path, row))  # row None = само почистване
        _ensure_writer()
        _audit_cond.notify_all()


def _ensure_writer():
    """Вика се при взет _audit_cond."""
    if _audit_state["thread"] is None or not _audit_state["thread"].is_alive():
        t = threading.Thread(target=_audit_writer, name="audit-writer", daemon=True)
        _audit_state["thread"] = t
        t.start()


def _audit_writer():
    import db
    while True:
        with _audit_cond:
            while not _audit_queue:
                _audit_state["busy"] = False
                _audit_cond.notify_all()
                _audit_cond.wait()
            _audit_state["busy"] = True
            batch = []
            while _audit_queue and len(batch) < 200:
                batch.append(_audit_queue.popleft())
        by_path = collections.OrderedDict()
        for path, row in batch:
            entry = by_path.setdefault(path, {"rows": [], "prune": False})
            if row is None:
                entry["prune"] = True
            else:
                entry["rows"].append(row)
        for path, entry in by_path.items():
            _write_audit_batch(db, path, entry["rows"], entry["prune"])


def _write_audit_batch(db, path, rows, prune):
    last_exc = None
    for delay in (0,) + _AUDIT_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            db.write_audit_rows(path, rows, prune=prune)
            return True
        except Exception as exc:  # заета/недостъпна база — нов опит
            last_exc = exc
    if rows:
        log_warning_throttled("audit_write", "applog.log_audit",
                              "%d записа не са записани в одитния дневник на базата (%s) — "
                              "остават само във файла" % (len(rows), last_exc))
    else:
        log_warning_throttled("audit_prune", "applog.log_audit",
                              "старите записи в одитния дневник не са махнати: %s" % last_exc)
    return False


def flush_audit(timeout=5.0):
    """Изчаква фоновия писач да запише опашката (тестове, изход). True при празна."""
    deadline = time.monotonic() + timeout
    with _audit_cond:
        while _audit_queue or _audit_state["busy"]:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _audit_cond.wait(left)
    return True


atexit.register(flush_audit, 2.0)
