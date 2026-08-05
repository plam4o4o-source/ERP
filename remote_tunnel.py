# -*- coding: utf-8 -*-
"""Отдалечен достъп (сканиране на баркод с телефон от всяка мрежа — не само
локалната Wi-Fi мрежа на офиса) чрез Cloudflare Quick Tunnel.

Cloudflare Quick Tunnel дава временен публичен HTTPS адрес
(*.trycloudflare.com) с истински, доверен от браузърите сертификат — без
предупреждения — който пренасочва трафика към локалния сървър на
програмата (http://127.0.0.1:port). Не изисква пренасочване на портове на
рутера, статичен IP, домейн или регистрация — само изходяща връзка към
Cloudflare, каквато има всеки нормален интернет достъп в офиса.

Изпълнимият файл `cloudflared` се изтегля еднократно (официално, от
GitHub releases на Cloudflare) при първо стартиране на функцията и се пази
локално за следващи пускания — не се разпространява вградено в програмата,
за да не се разраства инсталаторът за функция, която не всеки ползва.

Адресът е защитен допълнително от нормалния вход с потребителско
име/парола на самата програма — но е достъпен за всеки, който го знае,
докато е активен, затова се стартира само при нужда от администратор и се
спира след употреба.
"""
import os
import platform
import re
import subprocess
import sys
import threading
import urllib.request

import net

_UA = {"User-Agent": "PachoLogistic-RemoteAccess"}
_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

_lock = threading.Lock()
_state = {
    "process": None,
    "status": "stopped",   # stopped | starting | running | error
    "url": None,
    "error": None,
}


def _base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _binary_path():
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    d = os.path.join(_base_dir(), "bin")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _download_url():
    if os.name == "nt":
        return ("https://github.com/cloudflare/cloudflared/releases/latest/"
                "download/cloudflared-windows-amd64.exe")
    if sys.platform == "darwin":
        return ("https://github.com/cloudflare/cloudflared/releases/latest/"
                "download/cloudflared-darwin-amd64.tgz")
    machine = platform.machine().lower()
    if "arm" in machine or "aarch64" in machine:
        return ("https://github.com/cloudflare/cloudflared/releases/latest/"
                "download/cloudflared-linux-arm64")
    return ("https://github.com/cloudflare/cloudflared/releases/latest/"
            "download/cloudflared-linux-amd64")


def ensure_binary():
    """Осигурява наличен `cloudflared` — изтегля го еднократно от
    официалните GitHub releases на Cloudflare, ако липсва локално."""
    path = _binary_path()
    if os.path.exists(path) and os.path.getsize(path) > 100000:
        return path
    req = urllib.request.Request(_download_url(), headers=_UA)
    tmp_path = path + ".download"
    with net.urlopen(req, timeout=60) as resp:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    os.replace(tmp_path, path)
    if os.name != "nt":
        os.chmod(path, 0o755)
    return path


def _consume_output(proc):
    """Чете изхода на cloudflared ред по ред, докато открие публичния
    адрес, и следи процеса да не приключи неочаквано."""
    found = False
    try:
        for raw in iter(proc.stdout.readline, ""):
            if not raw:
                break
            m = _URL_RE.search(raw)
            if m and not found:
                found = True
                with _lock:
                    if _state["process"] is proc:
                        _state["url"] = m.group(0)
                        _state["status"] = "running"
                        _state["error"] = None
    except Exception:
        pass
    finally:
        proc.wait()
        with _lock:
            if _state["process"] is proc and not found:
                _state["status"] = "error"
                _state["error"] = ("Компонентът за отдалечен достъп спря "
                                   "неочаквано (проверете интернет връзката).")
            if _state["process"] is proc and found:
                # Процесът приключи, след като вече бе показал адрес —
                # тунелът вече не работи.
                _state["process"] = None
                _state["status"] = "stopped"
                _state["url"] = None


def start(local_port):
    """Стартира нов тунел към http://127.0.0.1:local_port (ако вече не
    работи такъв). Изпълнява се асинхронно — статусът се проверява чрез
    status()."""
    with _lock:
        if _state["process"] is not None or _state["status"] == "starting":
            return
        _state["status"] = "starting"
        _state["url"] = None
        _state["error"] = None

    def _run():
        try:
            binary = ensure_binary()
        except Exception as exc:
            with _lock:
                _state["status"] = "error"
                _state["error"] = ("Неуспешно изтегляне на компонента за "
                                   "отдалечен достъп: %s" % exc)
            return
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                [binary, "tunnel", "--no-autoupdate", "--url",
                 "http://127.0.0.1:%d" % local_port],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                **kwargs,
            )
        except Exception as exc:
            with _lock:
                _state["status"] = "error"
                _state["error"] = ("Неуспешно стартиране на компонента за "
                                   "отдалечен достъп: %s" % exc)
            return
        with _lock:
            _state["process"] = proc
        _consume_output(proc)

    threading.Thread(target=_run, daemon=True).start()


def stop():
    with _lock:
        proc = _state["process"]
        _state["process"] = None
        _state["status"] = "stopped"
        _state["url"] = None
        _state["error"] = None
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass


def status():
    with _lock:
        return {"status": _state["status"], "url": _state["url"], "error": _state["error"]}
