# -*- coding: utf-8 -*-
"""Вход/изход и смяна на парола. Извлечено от app.py (Фаза 3) без промяна
в поведението — виж appcore.py за общите decorator-и/hook-ове."""
from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db
import login_guard
from appcore import MIN_PASSWORD_LENGTH, login_required


def register(app):
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", logout)
    app.add_url_rule("/password", "change_password", change_password, methods=["GET", "POST"])


def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        locked, wait_seconds = login_guard.is_locked_out(username)
        if locked:
            wait_minutes = max(1, (wait_seconds + 59) // 60)
            error = ("Твърде много неуспешни опити за вход. Опитайте отново след "
                     "около %d мин." % wait_minutes)
        else:
            con = db.get_db()
            user = con.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            con.close()
            if user and user["active"] and check_password_hash(user["password_hash"], password):
                login_guard.clear(username)
                con2 = db.get_db()
                theme = db.get_user_theme(con2, user["id"])
                con2.close()
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["full_name"] = user["full_name"]
                session["role"] = user["role"]
                session["theme"] = theme
                session["must_change_password"] = bool(user["must_change_password"])
                target = request.args.get("next") or url_for("dashboard")
                if not target.startswith("/"):
                    target = url_for("dashboard")
                return redirect(target)
            login_guard.register_failure(username)
            error = "Грешно потребителско име или парола, или акаунтът е деактивиран."
    return render_template("login.html", error=error)


def logout():
    session.clear()
    flash("Излязохте от системата.")
    return redirect(url_for("login"))


@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        repeat = request.form.get("repeat", "")
        con = db.get_db()
        user = con.execute("SELECT * FROM users WHERE id = ?",
                           (session["user_id"],)).fetchone()
        if not check_password_hash(user["password_hash"], current):
            flash("Текущата парола е грешна.")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash("Новата парола трябва да е поне %d символа." % MIN_PASSWORD_LENGTH)
        elif new != repeat:
            flash("Двете нови пароли не съвпадат.")
        else:
            con.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                (generate_password_hash(new), session["user_id"]))
            con.commit()
            con.close()
            session["must_change_password"] = False
            flash("Паролата е сменена успешно.")
            return redirect(url_for("dashboard"))
        con.close()
    return render_template("change_password.html", forced=session.get("must_change_password", False))
