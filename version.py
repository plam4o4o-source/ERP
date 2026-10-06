# -*- coding: utf-8 -*-
"""Версия на PH Logistics (семантична номерация: голяма.средна.малка).

При промяна на този номер и push към GitHub, автоматично се компилира
PHLogistics.exe и се публикува нов релийз с тази версия.
"""
__version__ = "3.80.0"

# Хранилище в GitHub, от което се проверява и тегли автоматичното обновяване
GITHUB_REPO = "plam4o4o-source/ERP"

# Одит (06.10.2026): техническите имена следват търговското „PH Logistics“.
# Старите имена остават ЗАВИНАГИ като резервни: клиентите до v3.78 търсят в
# релийза точно „PachoLogistic.exe“/„PachoLogistic-Setup.exe“ (release.yml
# публикува копия под тези имена), а мрежовите/преносимите инсталации
# продължават да работят със старите имена на файловете (legacy_migration.py).
EXE_NAME = "PHLogistics.exe"
SETUP_NAME = "PHLogistics-Setup.exe"
LEGACY_EXE_NAME = "PachoLogistic.exe"
LEGACY_SETUP_NAME = "PachoLogistic-Setup.exe"

# Папката на инсталацията под %LOCALAPPDATA%\Programs (и на потребителските
# данни под %LOCALAPPDATA%) — нова и стара.
INSTALL_DIR_NAME = "PHLogistics"
LEGACY_INSTALL_DIR_NAME = "PachoLogistic"
