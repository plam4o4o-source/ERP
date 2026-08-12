# -*- coding: utf-8 -*-
"""Резервно копиране и синхронизация на базата данни — локална/мрежова
папка, и автоматична синхронизация с частно GitHub хранилище.

Настройките за локален архив се пазят в таблица `settings` на самата база
данни. Настройките за GitHub синхронизация се пазят в pacho_config.json
(config.py) — НЕ в базата — защото при чисто нова инсталация трябва да
знаем откъде да изтеглим базата данни, преди тя изобщо да съществува.
"""
import base64
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import applog
import db
import net

_UA = {"User-Agent": "PachoLogistic-Backup"}
_auto_thread = {"timer": None}


class RemoteChangedError(RuntimeError):
    """Отдалечената база в GitHub е била променена (различно SHA) след
    последното известно на тази инсталация състояние — качването е спряно,
    за да не презапише мълчаливо чужди промени (виж находка M2:
    ПЛАН_ЗА_РАЗРАБОТКА.md — GitHub синхронизацията на цялата база е
    "последният печели" при директен push без тази проверка)."""
    pass


def _sync_state_path(db_path=None):
    return (db_path or db.DB_PATH) + ".syncstate.json"


def _load_local_sync_state(db_path=None):
    """Локално (за тази инсталация) познато състояние на отдалечения файл
    в GitHub — НЕ е данни на фирмата, само техническо служебно състояние
    за синхронизацията, затова живее до .db файла (същия модел като
    .secret_key), не в самата база (за да не се качва/тегли безкрайно в
    цикъл заедно с базата при всяка синхронизация)."""
    path = _sync_state_path(db_path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def _save_local_sync_state(data, db_path=None):
    try:
        with open(_sync_state_path(db_path), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass  # не е критично — най-лошото е следваща конфликт-проверка да е по-малко точна

# ---------------------------------------------------------------- статус на
# автоматичната синхронизация с GitHub (за индикатор в „Настройки“)
DEBOUNCE_SECONDS = 8
RETRY_SECONDS = 120

_sync_state = {
    "dirty": False,
    "syncing": False,
    "last_synced_at": None,
    "last_error": None,
    "debounce_timer": None,
    "retry_timer": None,
}


def sync_status():
    """Текущ статус на GitHub синхронизацията, безопасен за показване в UI."""
    return {k: v for k, v in _sync_state.items()
            if k not in ("debounce_timer", "retry_timer")}


#: Одит (находка В11): sqlite3.Connection.backup() при SQLITE_BUSY (базата
#: заета от друга едновременна връзка — реалистично в мрежов режим на бавен
#: диск) спи и опитва отново БЕЗ горна граница — connect(timeout=...) НЕ
#: важи за самия backup цикъл, само за първоначалното отваряне. Без изрична
#: горна граница едно заето копиране виси безкрайно — на "Архивирай сега"
#: (замразява една от малкото нишки на сървъра в мрежов режим завинаги), на
#: автоматичния GitHub архив (фоновата нишка увисва, "dirty" никога не се
#: изчиства) и на часовия локален архив (спира да се възобновява). Виж
#: _bounded_backup по-долу.
_BACKUP_MAX_SECONDS = 25


def _bounded_backup(src, dst, max_seconds=_BACKUP_MAX_SECONDS):
    """src.backup(dst) с твърда горна граница на времето. Ползва официалната
    "progress" кука на sqlite3 (извиква се периодично между стъпките на
    копирането, включително между опитите при SQLITE_BUSY) — вдигнато оттам
    изключение прекъсва самия backup() цикъл, вместо да го оставя да виси."""
    deadline = time.monotonic() + max_seconds

    def _progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError(
                "Архивирането отне повече от %d сек. (базата вероятно е "
                "заета от друга едновременна операция) — прекратено, за да "
                "не остане заявката/нишката заключена безкрайно." % max_seconds
            )

    src.backup(dst, progress=_progress, sleep=0.25)


def local_backup(dest_folder):
    """Прави безопасно копие на живата база данни в dest_folder (локална
    папка или мрежов диск/споделена папка). Използва вградения SQLite
    backup API, за да не копира файл, който в момента се записва."""
    if not dest_folder:
        raise ValueError("Не е зададена папка за архив.")
    if not os.path.isdir(dest_folder):
        raise RuntimeError(
            "Папката за архив не съществува или не е достъпна: %s" % dest_folder
        )
    # Одит (находка В12, част 1): преди да добавим поредния файл, проверяваме
    # свободното място — часовите архиви растат неограничено (виж
    # _rotate_local_backups по-долу за самата ротация); при почти пълен диск
    # предпочитаме ясна грешка сега пред трудно обясним провал по средата на
    # копирането (или тих провал на СЛЕДВАЩ, несвързан запис в програмата).
    try:
        free_bytes = shutil.disk_usage(dest_folder).free
        db_size = os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else 0
        if db_size and free_bytes < db_size * 2:
            raise RuntimeError(
                "Малко свободно място в папката за архив (%.1f MB свободни, "
                "базата е %.1f MB) — архивирането е спряно, за да не се "
                "запълни дискът напълно." % (free_bytes / 1e6, db_size / 1e6)
            )
    except OSError:
        pass  # неуспешна проверка на мястото не бива да спира самия архив
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(dest_folder, "pacho_logistic_%s.db" % stamp)
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        _bounded_backup(src, dst)
    finally:
        dst.close()
        src.close()
    _rotate_local_backups(dest_folder)
    return dest_path


_BACKUP_NAME_RE = re.compile(r"^pacho_logistic_(\d{8})_(\d{6})\.db$")


def _rotate_local_backups(dest_folder, now=None):
    """Одит (находка В12): часовият автоматичен архив (start_auto_backup,
    по подразбиране на всеки 60 мин) преди тази поправка никога не трieше
    стари копия — при база от 100 MB това е ~2.3 GB/ден, необозримо с
    времето, обичайно на СЪЩИЯ мрежов диск, който вече е под натиск.

    Политика на пазене (проста "дядо-баща-син" ротация, без външни
    зависимости): всичко от последните 48 часа се пази непокътнато (пълна
    часова резолюция за бързо възстановяване веднага след инцидент); от
    48 часа до 30 дни назад — само НАЙ-СТАРИЯТ архив на всеки календарен
    ден; отвъд 30 дни — само най-старият архив на всеки календарен месец.
    Всичко друго извън тези правила се трие.

    Засяга само файлове, отговарящи ТОЧНО на собствения формат на името
    (pacho_logistic_ГГГГММДД_ЧЧММСС.db) — други файлове в папката (напр.
    ръчно направени копия) не се пипат."""
    now = now or datetime.now()
    entries = []
    try:
        names = os.listdir(dest_folder)
    except OSError:
        return
    for name in names:
        m = _BACKUP_NAME_RE.match(name)
        if not m:
            continue
        try:
            stamp = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        entries.append((stamp, os.path.join(dest_folder, name)))
    entries.sort()  # най-старите първи

    keep = set()
    seen_days = set()
    seen_months = set()
    for stamp, path in entries:
        age = now - stamp
        if age <= timedelta(hours=48):
            keep.add(path)
        elif age <= timedelta(days=30):
            day_key = stamp.date()
            if day_key not in seen_days:
                seen_days.add(day_key)
                keep.add(path)
        else:
            month_key = (stamp.year, stamp.month)
            if month_key not in seen_months:
                seen_months.add(month_key)
                keep.add(path)

    for stamp, path in entries:
        if path not in keep:
            try:
                os.remove(path)
            except OSError:
                applog.log_exception("backup._rotate_local_backups: неуспешно изтриване на стар архив %s" % path)


def _github_request(url, token, method="GET", body=None, tolerate_404=False):
    """tolerate_404=True връща (404, {}) вместо да хвърля грешка — нужно за
    проверки от рода на „съществува ли вече файлът“, където 404 е напълно
    очакван и нормален отговор (нов файл, или изцяло празно хранилище —
    GitHub отговаря с 404 „This repository is empty“), не истинска грешка."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        **_UA,
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with net.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        if tolerate_404 and exc.code == 404:
            return 404, {}
        payload = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(payload).get("message", payload)
        except ValueError:
            pass
        raise RuntimeError("GitHub отговори с грешка (%d): %s" % (exc.code, payload))
    except urllib.error.URLError as exc:
        raise RuntimeError("Няма връзка с GitHub: %s" % exc.reason)


def github_backup(owner, repo, token, branch="main", path_in_repo="pacho_logistic.db",
                  force=False):
    """Качва текущата база данни като файл в частно GitHub хранилище чрез
    Contents API (create-or-update-file). Изисква personal access token
    с права за запис (repo) върху посоченото хранилище.

    force=False (по подразбиране) прави проверка за конфликт (виж
    RemoteChangedError) преди да презапише отдалечения файл — ако друга
    инсталация е качила по-нова версия след последната ни известна тук,
    качването СПИРА вместо мълчаливо да я презапише (last-write-wins би
    загубил чужди документи/клиенти без никой да разбере). force=True
    пропуска тази проверка — за админ, който съзнателно иска да презапише
    (напр. знае, че тук е авторитетната версия) — виж system_settings в
    app.py за как е изложено в UI."""
    if not (owner and repo and token):
        raise ValueError("Липсват данни за GitHub хранилището (собственик/име/токен).")

    tmp_copy = local_backup_to_temp()
    try:
        with open(tmp_copy, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("ascii")
    finally:
        os.remove(tmp_copy)

    api_url = "https://api.github.com/repos/%s/%s/contents/%s" % (owner, repo, path_in_repo)

    def _existing_sha():
        # 404 тук е нормално и очаквано (файлът още не съществува, или
        # хранилището е изцяло празно) — просто връщаме None, PUT-ът по-долу
        # създава файла (и първия commit, ако е нужно).
        status, existing = _github_request(api_url + "?ref=" + branch, token, tolerate_404=True)
        if status == 200 and isinstance(existing, dict):
            return existing.get("sha")
        return None

    sha = _existing_sha()

    if not force:
        known_sha = _load_local_sync_state().get("last_known_remote_sha")
        # Конфликт само ако РЕАЛНО имаме предишно известно състояние (иначе
        # това е първото качване от тази инсталация — няма с какво да
        # сравним) И отдалеченото sha вече не съвпада с него — значи друго
        # място е качило нещо след последната ни синхронизация оттук.
        if known_sha and sha and sha != known_sha:
            raise RemoteChangedError(
                "Отдалечената база данни в GitHub е била променена от друго "
                "място след последната синхронизация оттук (напр. друг "
                "компютър е качил по-нова версия). За да не загубите чужди "
                "промени, качването е спряно — първо изтеглете последната "
                "версия („Изтегли от GitHub“), после опитайте пак."
            )

    body = {
        "message": "Автоматичен архив на базата данни — %s" %
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    try:
        status, result = _github_request(api_url, token, method="PUT", body=body)
    except RuntimeError as exc:
        # Състезание: файлът може току-що да е бил създаден от предишно
        # качване (напр. секунди по-рано), но проверката по-горе все още
        # да не го е "видяла" заради кратко забавяне в разпространението
        # на GitHub API — тогава PUT без sha гърми с 422 "sha wasn't
        # supplied", въпреки че файлът реално вече съществува. Презареждаме
        # sha-то веднъж още и пробваме отново, вместо да се предаваме.
        if sha is None and "422" in str(exc) and "sha" in str(exc).lower():
            sha = _existing_sha()
            if sha is None:
                raise
            body["sha"] = sha
            status, result = _github_request(api_url, token, method="PUT", body=body)
        else:
            raise
    if status not in (200, 201):
        raise RuntimeError("Неуспешно качване в GitHub (код %s)." % status)
    new_sha = result.get("content", {}).get("sha")
    if new_sha:
        _save_local_sync_state({"last_known_remote_sha": new_sha})
    return result.get("content", {}).get("html_url", "")


def pull_db(owner, repo, token, branch, path_in_repo, dest_path):
    """Изтегля базата данни от частно GitHub хранилище и я записва на
    dest_path (атомарно). Използва се при първо стартиране на нова
    инсталация, за да могат всички служители да заредят автоматично вече
    съществуващите данни (клиенти, издадени документи и т.н.).

    Връща (успех: bool, съобщение_при_грешка: str|None).
    """
    if not (owner and repo and token):
        return False, "Липсват данни за GitHub хранилището (собственик/име/токен)."
    api_url = "https://api.github.com/repos/%s/%s/contents/%s?ref=%s" % (
        owner, repo, path_in_repo, branch)
    try:
        status, result = _github_request(api_url, token, tolerate_404=True)
    except RuntimeError as exc:
        return False, str(exc)
    if status == 404:
        return False, "В хранилището все още няма запазена база данни."
    if status != 200 or not isinstance(result, dict):
        return False, "Неочакван отговор от GitHub (код %s)." % status

    content_b64 = result.get("content")
    if content_b64:
        content = base64.b64decode(content_b64)
    else:
        download_url = result.get("download_url")
        if not download_url:
            return False, "Файлът е твърде голям за директно изтегляне през GitHub API."
        req = urllib.request.Request(download_url, headers=_UA)
        with net.urlopen(req, timeout=60) as resp:
            content = resp.read()

    if len(content) < 100:
        return False, "Изтегленият файл изглежда невалиден или празен."

    tmp_path = dest_path + ".download"
    with open(tmp_path, "wb") as f:
        f.write(content)

    # Одит (находка К1, критична): предишната версия правеше директно
    # os.replace(tmp_path, dest_path) тук — атомарно САМО за самия .db
    # файл. Ако текущата (заменяната) база е била в WAL режим (виж
    # db._USE_WAL — включено по подразбиране за локална инсталация), до нея
    # стои "-wal" файл с неприложени промени; той НЕ се пипаше по никакъв
    # начин. При следващото отваряне SQLite прилага СТАРИЯ "-wal" върху
    # НОВОИЗТЕГЛЕНОТО съдържание — проверено емпирично: изтеглените данни
    # биват МЪЛЧАЛИВО изхвърлени (връща се старото съдържание, а
    # PRAGMA integrity_check дори докладва "ok" — няма никакъв признак за
    # потребителя), или, при друга комбинация от размери, базата става
    # изцяло нечетима. Функцията, предназначена да ВЪЗСТАНОВИ данни, беше
    # тази, която ги унищожаваше.
    #
    # Поправка на три стъпки, всяка от които може да спре процеса с ясно
    # съобщение вместо да рискува данни:
    #  1. проверка на изтегления файл (PRAGMA integrity_check) ПРЕДИ да се
    #     пипне живата база изобщо;
    #  2. предпазно копие на текущата база встрани (checkpoint + copy),
    #     преди да я презапишем — има от какво да се възстанови ръчно, ако
    #     нещо друго все пак се обърка;
    #  3. изрично изтриване на остатъчните "-wal"/"-shm" файлове ПРЕДИ
    #     атомарната замяна, за да не се приложи чужд WAL върху новото
    #     съдържание при следващото отваряне.
    try:
        check_con = sqlite3.connect(tmp_path)
        try:
            row = check_con.execute("PRAGMA integrity_check").fetchone()
            ok = bool(row) and row[0] == "ok"
        finally:
            check_con.close()
    except sqlite3.DatabaseError:
        ok = False
    if not ok:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False, ("Изтегленият файл не мина проверка за цялост (повреден "
                      "или невалиден) — нищо в текущата база не е презаписано.")

    if os.path.exists(dest_path):
        try:
            # Checkpoint на текущата база (ако е в WAL режим), за да можем
            # да копираме консистентно съдържание встрани — самият .db файл
            # без checkpoint може да не съдържа последните комитнати
            # промени (те стоят само в "-wal").
            chk_con = sqlite3.connect(dest_path, timeout=5)
            try:
                chk_con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
            chk_con.close()
            safety_copy = dest_path + ".преди-изтегляне-%s" % (
                datetime.now().strftime("%Y%m%d_%H%M%S"))
            shutil.copy2(dest_path, safety_copy)
        except OSError as exc:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False, ("Неуспешно създаване на предпазно копие на текущата "
                          "база данни (%s) — замяната е спряна, за да не се "
                          "рискуват съществуващите данни." % exc)

    for suffix in ("-wal", "-shm"):
        try:
            os.remove(dest_path + suffix)
        except OSError:
            pass  # няма такъв файл (не е бил в WAL режим) — нормално

    try:
        os.replace(tmp_path, dest_path)  # атомарна замяна
    except OSError as exc:
        # Напр. Windows: файлът е отворен/заключен от текущо работещия
        # процес на програмата (самата тя чете тази база в момента) —
        # преди тази поправка това изключение изобщо не се хващаше.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False, ("Неуспешна замяна на базата данни (%s) — вероятно е "
                      "отворена от друг процес. Затворете програмата на "
                      "всички компютри, използващи тази база, и опитайте "
                      "пак." % exc)

    # Записваме познатото sha веднага СЛЕД успешно изтегляне — базовата
    # линия за бъдещата проверка за конфликт при качване (виж
    # github_backup/RemoteChangedError по-горе) следва точно тази версия,
    # която току-що стана и локалната.
    remote_sha = result.get("sha")
    if remote_sha:
        _save_local_sync_state({"last_known_remote_sha": remote_sha}, dest_path)
    return True, None


def mark_dirty(get_config_func):
    """Извиква се след всяка успешна промяна в базата данни (нов документ,
    клиент, служител, настройка). Насрочва синхронизация с GitHub след
    кратко забавяне (DEBOUNCE_SECONDS), за да обедини няколко бързи
    последователни промени в едно качване, вместо да блъска API-то при
    всяко единично записване."""
    try:
        cfg = get_config_func()
    except Exception:
        applog.log_exception("backup.mark_dirty: неуспешно четене на конфигурацията")
        return
    if not cfg.get("gh_auto_sync"):
        return
    _sync_state["dirty"] = True
    if _sync_state["debounce_timer"]:
        _sync_state["debounce_timer"].cancel()
    if _sync_state["retry_timer"]:
        _sync_state["retry_timer"].cancel()
        _sync_state["retry_timer"] = None
    t = threading.Timer(DEBOUNCE_SECONDS, _attempt_sync, args=(get_config_func,))
    t.daemon = True
    t.start()
    _sync_state["debounce_timer"] = t


def _attempt_sync(get_config_func):
    try:
        cfg = get_config_func()
    except Exception:
        applog.log_exception("backup._attempt_sync: неуспешно четене на конфигурацията")
        return
    if not cfg.get("gh_auto_sync") or not _sync_state["dirty"]:
        return
    _sync_state["syncing"] = True
    try:
        github_backup(
            cfg.get("gh_owner", ""), cfg.get("gh_repo", ""), cfg.get("gh_token", ""),
            cfg.get("gh_branch", "main") or "main",
            cfg.get("gh_path", "pacho_logistic.db") or "pacho_logistic.db",
        )
        _sync_state["dirty"] = False
        _sync_state["last_synced_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _sync_state["last_error"] = None
    except Exception as exc:
        # Няма връзка или друга грешка — данните остават запазени локално
        # (винаги успешно, независимо от интернет) и пробваме пак по-късно.
        _sync_state["last_error"] = str(exc)
        t = threading.Timer(RETRY_SECONDS, _attempt_sync, args=(get_config_func,))
        t.daemon = True
        t.start()
        _sync_state["retry_timer"] = t
    finally:
        _sync_state["syncing"] = False


def trigger_sync_now(get_config_func):
    """Стартира РЪЧНО заявеното от администратор качване в GitHub (бутон
    „Качи сега в GitHub“ в Настройки) във фонова нишка, вместо да блокира
    заявката/работника на Flask, докато трае мрежовата операция — виж
    находка M3 (блокиращо I/O в нишката на заявката). За разлика от
    mark_dirty/_attempt_sync (автоматичната синхронизация след промяна),
    тук качването става БЕЗ значение дали „gh_auto_sync“ е включено —
    администраторът иска качване веднага, независимо от автоматичните
    настройки. Резултатът (успех/грешка) се отразява в sync_state и се
    вижда при следващото зареждане/презареждане на „Настройки“ (същият
    sync_status(), който страницата вече показва — не е нужен нов
    интерфейс), вместо в директен flash отговор на самата заявка."""
    try:
        cfg = get_config_func()
    except Exception:
        applog.log_exception("backup.trigger_sync_now: неуспешно четене на конфигурацията")
        return

    if _sync_state["debounce_timer"]:
        _sync_state["debounce_timer"].cancel()
        _sync_state["debounce_timer"] = None
    _sync_state["syncing"] = True

    def _run():
        try:
            github_backup(
                cfg.get("gh_owner", ""), cfg.get("gh_repo", ""), cfg.get("gh_token", ""),
                cfg.get("gh_branch", "main") or "main",
                cfg.get("gh_path", "pacho_logistic.db") or "pacho_logistic.db",
            )
            _sync_state["dirty"] = False
            _sync_state["last_synced_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _sync_state["last_error"] = None
        except Exception as exc:
            _sync_state["last_error"] = str(exc)
        finally:
            _sync_state["syncing"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def local_backup_to_temp():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db", prefix="pacho_backup_")
    os.close(fd)
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(path)
    try:
        _bounded_backup(src, dst)
    finally:
        dst.close()
        src.close()
    return path


def start_auto_backup(get_settings_func, interval_minutes=60):
    """Стартира фонов таймер, който периодично прави локален архив, ако е
    зададена папка в настройките. Извиква се веднъж при стартиране."""
    def _tick():
        try:
            s = get_settings_func()
            folder = s.get("backup_folder", "").strip()
            if folder and s.get("backup_auto"):
                local_backup(folder)
        except Exception:
            applog.log_exception("backup._tick: неуспешен автоматичен локален архив")
        finally:
            t = threading.Timer(interval_minutes * 60, _tick)
            t.daemon = True
            t.start()
            _auto_thread["timer"] = t

    t = threading.Timer(60, _tick)  # първи опит минута след стартиране
    t.daemon = True
    t.start()
    _auto_thread["timer"] = t
