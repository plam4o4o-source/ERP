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
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import db

_UA = {"User-Agent": "PachoLogistic-Backup"}
_auto_thread = {"timer": None}

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
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(dest_folder, "pacho_logistic_%s.db" % stamp)
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest_path


def _github_request(url, token, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        **_UA,
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(payload).get("message", payload)
        except ValueError:
            pass
        raise RuntimeError("GitHub отговори с грешка (%d): %s" % (exc.code, payload))
    except urllib.error.URLError as exc:
        raise RuntimeError("Няма връзка с GitHub: %s" % exc.reason)


def github_backup(owner, repo, token, branch="main", path_in_repo="pacho_logistic.db"):
    """Качва текущата база данни като файл в частно GitHub хранилище чрез
    Contents API (create-or-update-file). Изисква personal access token
    с права за запис (repo) върху посоченото хранилище."""
    if not (owner and repo and token):
        raise ValueError("Липсват данни за GitHub хранилището (собственик/име/токен).")

    tmp_copy = local_backup_to_temp()
    try:
        with open(tmp_copy, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("ascii")
    finally:
        os.remove(tmp_copy)

    api_url = "https://api.github.com/repos/%s/%s/contents/%s" % (owner, repo, path_in_repo)
    sha = None
    status, existing = _github_request(api_url + "?ref=" + branch, token)
    if status == 200 and isinstance(existing, dict):
        sha = existing.get("sha")

    body = {
        "message": "Автоматичен архив на базата данни — %s" %
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    status, result = _github_request(api_url, token, method="PUT", body=body)
    if status not in (200, 201):
        raise RuntimeError("Неуспешно качване в GitHub (код %s)." % status)
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
        status, result = _github_request(api_url, token)
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()

    if len(content) < 100:
        return False, "Изтегленият файл изглежда невалиден или празен."

    tmp_path = dest_path + ".download"
    with open(tmp_path, "wb") as f:
        f.write(content)
    os.replace(tmp_path, dest_path)  # атомарна замяна
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


def local_backup_to_temp():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db", prefix="pacho_backup_")
    os.close(fd)
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
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
            pass
        finally:
            t = threading.Timer(interval_minutes * 60, _tick)
            t.daemon = True
            t.start()
            _auto_thread["timer"] = t

    t = threading.Timer(60, _tick)  # първи опит минута след стартиране
    t.daemon = True
    t.start()
    _auto_thread["timer"] = t
