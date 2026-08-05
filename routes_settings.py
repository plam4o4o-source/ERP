# -*- coding: utf-8 -*-
"""Настройки на фирмата изпращач (лого, данни) и лични настройки (тема),
плюс системните настройки вградени в „Моите настройки“ за администратори.
Извлечено от app.py (Фаза 3) без промяна в поведението."""
from flask import abort, flash, redirect, render_template, request, send_file, session, url_for

import backup
import branding
import config as appconfig
import db
from appcore import login_required


def register(app):
    app.add_url_rule("/settings", "settings_page", settings_page, methods=["GET", "POST"])
    app.add_url_rule("/settings/logo", "settings_logo_upload",
                     settings_logo_upload, methods=["POST"])
    app.add_url_rule("/settings/logo/remove", "settings_logo_remove",
                     settings_logo_remove, methods=["POST"])
    app.add_url_rule("/logo.img", "company_logo_image", company_logo_image)
    app.add_url_rule("/my-settings", "my_settings", my_settings, methods=["GET", "POST"])


@login_required
def settings_page():
    con = db.get_db()
    if request.method == "POST":
        keys = ("sender_name", "sender_address", "sender_city", "sender_postcode",
                "sender_country", "sender_eik", "sender_vat", "sender_phone",
                "sender_email", "sender_person")
        db.save_settings(con, {k: request.form.get(k, "").strip() for k in keys})
        con.commit()
        con.close()
        flash("Данните на фирмата изпращач са запазени.")
        return redirect(url_for("settings_page"))
    s = db.get_settings(con)
    con.close()
    return render_template("settings.html", s=s)


@login_required
def settings_logo_upload():
    file = request.files.get("logo_file")
    if not file or not file.filename:
        flash("Моля, изберете файл с изображение.")
        return redirect(url_for("settings_page"))
    try:
        branding.save_logo(file)
        flash("Логото на фирмата е качено успешно.")
    except ValueError as exc:
        flash("Логото не бе прието: %s" % exc)
    return redirect(url_for("settings_page"))


@login_required
def settings_logo_remove():
    branding.remove_logo()
    flash("Логото на фирмата е премахнато.")
    return redirect(url_for("settings_page"))


@login_required
def company_logo_image():
    path = branding.logo_path()
    if path is None:
        abort(404)
    return send_file(path, mimetype=branding.logo_mimetype(path))


@login_required
def my_settings():
    con = db.get_db()
    if request.method == "POST":
        theme = request.form.get("theme", db.DEFAULT_THEME)
        if theme not in db.THEMES:
            theme = db.DEFAULT_THEME
        db.save_user_settings(con, session["user_id"], {"theme": theme})
        con.commit()
        con.close()
        session["theme"] = theme
        flash("Настройките са запазени.")
        return redirect(url_for("my_settings"))
    current_theme = db.get_user_theme(con, session["user_id"])
    ctx = {"themes": db.THEMES, "current_theme": current_theme}
    if session.get("role") == "admin":
        # Системните настройки (мрежа/архив/GitHub синхронизация) се
        # показват на същата страница, видими само за администратори.
        ctx.update(s=db.get_settings(con), cfg=appconfig.load_config(),
                  db_path=db.DB_PATH, sync=backup.sync_status())
    con.close()
    return render_template("my_settings.html", **ctx)
