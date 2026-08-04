@echo off
chcp 65001 >nul
title ПачоЛогистик
echo Инсталиране на нужните библиотеки (само първия път)...
python -m pip install -r requirements.txt --quiet
echo Стартиране на ПачоЛогистик на http://127.0.0.1:5000 ...
start "" http://127.0.0.1:5000
python app.py
pause
