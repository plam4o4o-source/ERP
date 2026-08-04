# -*- coding: utf-8 -*-
"""Стартиране на ПачоЛогистик като истинско настолно Windows приложение.

Три нива, изпробвани по ред:

1. **Вграден прозорец (pywebview / WebView2)** — предпочитаният начин.
   Фласк сървърът работи във фонова нишка в СЪЩИЯ процес, а прозорецът е
   вграден компонент (WebView2, вграден в Windows 10/11), без изобщо да се
   стартира отделен браузър процес — истинско настолно приложение.
2. **Chrome/Edge в „режим на приложение“** (--app) — ако pywebview липсва
   или не успее да се инициализира (напр. WebView2 не е наличен на много
   стара Windows инсталация). Все пак изглежда като самостоятелен прозорец,
   без адресна лента/табове, но технически използва инсталирания браузър.
3. **Обикновен таб в браузъра по подразбиране** — последна резервна мярка,
   ако нищо друго не проработи, за да не остане потребителят без достъп.
"""
import os
import shutil
import subprocess
import time
import urllib.request


def wait_for_server(url, timeout=15):
    """Изчаква Flask сървърът реално да приема връзки, преди да отворим
    прозорец към него (иначе първото зареждане показва грешка за връзка)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def run_native_window(url, title="ПачоЛогистик", width=1360, height=860):
    """Опитва да отвори вграден настолен прозорец с pywebview (WebView2).

    Блокира до затварянето на прозореца от потребителя (webview.start() е
    главният цикъл на GUI-то). Връща True при нормално затваряне на
    прозореца. Връща False, ако pywebview липсва или не успее да се
    инициализира изобщо — тогава извикващият код трябва да ползва
    open_app_window() или webbrowser.open() като резервен вариант.
    """
    try:
        import webview
    except ImportError:
        return False

    wait_for_server(url)
    try:
        webview.create_window(title, url, width=width, height=height,
                              min_size=(960, 620))
        webview.start()
        return True
    except Exception:
        return False


def _find_windows_browser():
    candidates = []
    for env_var, sub in (
        ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
        ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
        ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
        ("LOCALAPPDATA", r"Microsoft\Edge\Application\msedge.exe"),
    ):
        base = os.environ.get(env_var)
        if base:
            candidates.append(os.path.join(base, sub))
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("chrome", "chrome.exe", "google-chrome", "msedge", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_app_window(url, width=1360, height=860):
    """Отваря URL в самостоятелен прозорец без интерфейс на браузъра
    (резервен вариант, ако pywebview/WebView2 не са налични).

    Връща True ако е успяло да стартира прозорец „като приложение“, иначе
    False (тогава извикващият код би трябвало да ползва webbrowser.open).
    """
    if os.name != "nt":
        return False
    browser = _find_windows_browser()
    if not browser:
        return False
    profile_dir = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "PachoLogistic", "AppWindowProfile",
    )
    try:
        os.makedirs(profile_dir, exist_ok=True)
        subprocess.Popen([
            browser,
            "--app=%s" % url,
            "--window-size=%d,%d" % (width, height),
            "--user-data-dir=%s" % profile_dir,
            "--no-first-run",
            "--no-default-browser-check",
        ], close_fds=True)
        return True
    except OSError:
        return False
