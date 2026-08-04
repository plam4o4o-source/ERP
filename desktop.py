# -*- coding: utf-8 -*-
"""Стартиране на ПачоЛогистик като истинско Windows приложение.

Вместо обикновен таб в браузъра, отваряме инсталирания Microsoft Edge (или
Chrome, ако Edge липсва) в „режим на приложение“ (--app) — собствен прозорец
без адресна лента, табове или бутони на браузъра, с икона в лентата на
задачите като всяка друга Windows програма. Ако не бъде намерен нито Edge,
нито Chrome (напр. на Linux/Mac при разработка), се отваря обикновен таб в
подразбиращия се браузър.
"""
import os
import shutil
import subprocess
import sys


def _find_windows_browser():
    candidates = []
    for env_var, sub in (
        ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
        ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
        ("LOCALAPPDATA", r"Microsoft\Edge\Application\msedge.exe"),
        ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
        ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
    ):
        base = os.environ.get(env_var)
        if base:
            candidates.append(os.path.join(base, sub))
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "msedge.exe", "chrome", "chrome.exe", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_app_window(url, width=1360, height=860):
    """Отваря URL в самостоятелен прозорец без интерфейс на браузъра.

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
