# -*- coding: utf-8 -*-
"""Вход/изход и смяна на парола. Извлечено от app.py (Фаза 3) без промяна
в поведението — виж appcore.py за общите decorator-и/hook-ове."""
from flask import flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _
from werkzeug.security import check_password_hash, generate_password_hash

import db
import login_guard
from appcore import MIN_PASSWORD_LENGTH, get_db, login_required


def register(app):
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", logout)
    app.add_url_rule("/password", "change_password", change_password, methods=["GET", "POST"])


def _safe_next_target(raw):
    """Одит (находка С15, среден риск): проверката `target.startswith("/")`
    пропускаше протоколно-относителни адреси като "//evil.example.com/x" —
    БРАУЗЪРЪТ третира водещото "//" като "същия протокол + ЧУЖД домейн", не
    като relative path в рамките на текущия сайт (POST /login?next=
    //evil.example.com/x → 302 Location: //evil.example.com/x → служителят
    въвежда истинските си данни в истинската програма и бива пренасочен към
    чужд сайт). Отхвърляме и "/\\" — някои браузъри нормализират обратната
    наклонена черта до "/" преди навигация, същият трик под друга форма.
    Връща None (извикващият пада към url_for("dashboard")), ако адресът не
    е сигурно ВЪТРЕШЕН relative път."""
    if not raw:
        return None
    if not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return None
    return raw


def login():
    # Превключвател на езика на логин панела (?lang=en и т.н.) — важи само
    # за текущата сесия/браузър, ПРЕДИ вход. Обикновен GET параметър, не
    # POST — само сменя показвания език, не сменя състояние на сървъра
    # отвъд собствената сесия на браузъра, затова не се нуждае от CSRF
    # токен (виж appcore._select_locale() за реда на избор на език).
    lang = request.args.get("lang")
    if lang in db.LANGUAGES:
        session["lang"] = lang

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        con = get_db()
        user = con.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        # Одит (Дребни): заключването беше проверявано ПРЕДИ паролата —
        # 5 грешни опита на 5 минути (лесно за нападателя — само по 1
        # опит на 5 мин., за да поддържа заключването постоянно) държаха
        # ИСТИНСКИЯ собственик на акаунта заключен навън дори с ПРАВИЛНАТА
        # парола (DoS чрез самото заключване). Сега паролата се проверява
        # ПЪРВО: правилна парола винаги влиза веднага, независимо от
        # историята на неуспешни опити (и я изчиства) — заключването пречи
        # само на ПО-НАТАТЪШНИ грешни опити, не и на истинския собственик.
        # Защитата срещу brute-force остава непокътната: нападател БЕЗ
        # правилната парола винаги пада в клона по-долу.
        if user and user["active"] and check_password_hash(user["password_hash"], password):
            login_guard.clear(username)
            theme = db.get_user_theme(con, user["id"])
            # Личният, трайно запазен избор на език на ТОЗИ потребител
            # (ако е избирал в Настройки преди) има предимство пред
            # временния избор от логин панела на това устройство —
            # иначе служител би виждал различен език на всяко
            # устройство, на което влиза за пръв път.
            user_lang = db.get_user_language(con, user["id"])
            chosen_before_login = session.get("lang")
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            session["theme"] = theme
            session["lang"] = user_lang or chosen_before_login or db.DEFAULT_LANGUAGE
            session["must_change_password"] = bool(user["must_change_password"])
            target = _safe_next_target(request.args.get("next")) or url_for("dashboard")
            return redirect(target)
        locked, wait_seconds = login_guard.is_locked_out(username)
        if locked:
            wait_minutes = max(1, (wait_seconds + 59) // 60)
            error = ("Твърде много неуспешни опити за вход. Опитайте отново след "
                     "около %d мин." % wait_minutes)
        else:
            login_guard.register_failure(username)
            error = "Грешно потребителско име или парола, или акаунтът е деактивиран."
    # languages/current_lang идват от appcore._register_globals (общи за
    # всички шаблони) — не се подават изрично тук.
    # Изгледът на анимираната сцена (реалистична/класическа) е ОБЩА
    # настройка на инсталацията (виж db.LOGIN_SCENES) — екранът е преди
    # вход и няма „текущ потребител“ с лична настройка.
    return render_template("login.html", error=error,
                           login_scene=db.get_login_scene(get_db()))


def logout():
    lang = session.get("lang")  # запазваме избрания език и след изход
    session.clear()
    if lang in db.LANGUAGES:
        session["lang"] = lang
    flash(_("Излязохте от системата."), "info")
    return redirect(url_for("login"))


@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        repeat = request.form.get("repeat", "")
        con = get_db()
        user = con.execute("SELECT * FROM users WHERE id = ?",
                           (session["user_id"],)).fetchone()
        if not check_password_hash(user["password_hash"], current):
            flash(_("Текущата парола е грешна."), "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(_("Новата парола трябва да е поне %d символа.") % MIN_PASSWORD_LENGTH, "error")
        elif new != repeat:
            flash(_("Двете нови пароли не съвпадат."), "error")
        else:
            con.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                (generate_password_hash(new), session["user_id"]))
            con.commit()
            session["must_change_password"] = False
            flash(_("Паролата е сменена успешно."), "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html", forced=session.get("must_change_password", False))
