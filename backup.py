# -*- coding: utf-8 -*-
"""Резервно копиране на базата данни — на локална/мрежова папка и в GitHub.

Настройките (папка за архив, GitHub хранилище и токен) се пазят в таблица
`settings` на самата база данни (виж app.py, маршрут /admin/system).
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
