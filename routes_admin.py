# -*- coding: utf-8 -*-
"""Административен панел: системни настройки (мрежа/архив/GitHub
синхронизация), отдалечен достъп (Cloudflare тунел), управление на
служители, и проверка/инсталиране на обновления. Извлечено от app.py
(Фаза 3) без промяна в поведението, освен system_backup_github_now,
която вече стартира качването във фонова нишка (виж backup.trigger_sync_now
и находка M3 — блокиращо I/O в нишката на заявката) вместо да блокира
заявката, докато трае мрежовата операция."""
from flask import flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _
from werkzeug.security import generate_password_hash

import backup
import config as appconfig
import db
import remote_tunnel
import updater
from appcore import MIN_PASSWORD_LENGTH, admin_required, get_db, login_required


def register(app):
    app.add_url_rule("/admin/system", "system_settings", system_settings, methods=["GET", "POST"])
    app.add_url_rule("/admin/system/backup-now", "system_backup_now",
                     system_backup_now, methods=["POST"])
    app.add_url_rule("/admin/system/backup-github-now", "system_backup_github_now",
                     system_backup_github_now, methods=["POST"])
    app.add_url_rule("/admin/system/pull-now", "system_pull_now",
                     system_pull_now, methods=["POST"])
    app.add_url_rule("/admin/system/remote-start", "system_remote_start",
                     system_remote_start, methods=["POST"])
    app.add_url_rule("/admin/system/remote-stop", "system_remote_stop",
                     system_remote_stop, methods=["POST"])
    app.add_url_rule("/admin/system/remote-status", "system_remote_status",
                     system_remote_status)

    app.add_url_rule("/admin/users", "admin_users", admin_users)
    app.add_url_rule("/admin/users/new", "admin_user_new", admin_user_new, methods=["POST"])
    app.add_url_rule("/admin/users/<int:user_id>/toggle", "admin_user_toggle",
                     admin_user_toggle, methods=["POST"])
    app.add_url_rule("/admin/users/<int:user_id>/password", "admin_user_password",
                     admin_user_password, methods=["POST"])
    app.add_url_rule("/admin/users/<int:user_id>/delete", "admin_user_delete",
                     admin_user_delete, methods=["POST"])

    app.add_url_rule("/update/check", "update_check", update_check)
    app.add_url_rule("/update/install", "update_install", update_install, methods=["POST"])


# ---------------------------------------------------------------- системни настройки (админ,
# показвани вградени в „Настройки“ — вижте routes_settings.my_settings)

@admin_required
def system_settings():
    if request.method == "GET":
        return redirect(url_for("my_settings"))
    con = get_db()
    form = request.form.get("form")
    if form == "network":
        appconfig.save_config({
            "db_path": request.form.get("db_path", "").strip(),
            "network_mode": request.form.get("network_mode") == "on",
            "network_port": int(request.form.get("network_port") or 5000),
        })
        flash(_("Мрежовите настройки са запазени. Рестартирайте програмата, "
             "за да влязат в сила."), "success")
    elif form == "backup_folder":
        db.save_settings(con, {
            "backup_folder": request.form.get("backup_folder", "").strip(),
            "backup_auto": "on" if request.form.get("backup_auto") == "on" else "",
        })
        con.commit()
        flash(_("Настройките за локален/мрежов архив са запазени."), "success")
    elif form == "client_export":
        db.save_settings(con, {
            "client_export_dir": request.form.get("client_export_dir", "").strip(),
            "client_export_auto": "on" if request.form.get("client_export_auto") == "on" else "",
        })
        con.commit()
        flash(_("Настройките за клиентски папки са запазени."), "success")
    elif form == "backup_github":
        # GitHub данните се пазят в pacho_config.json (не в базата), за да
        # може нова инсталация да ги прочете и да изтегли базата ПРЕДИ тя
        # изобщо да съществува локално.
        current = appconfig.load_config()
        appconfig.save_config({
            "gh_owner": request.form.get("gh_owner", "").strip(),
            "gh_repo": request.form.get("gh_repo", "").strip(),
            "gh_branch": request.form.get("gh_branch", "main").strip() or "main",
            "gh_path": request.form.get("gh_path", "pacho_logistic.db").strip()
                      or "pacho_logistic.db",
            "gh_token": request.form.get("gh_token", "").strip() or current.get("gh_token", ""),
            "gh_auto_sync": request.form.get("gh_auto_sync") == "on",
        })
        flash(_("Настройките за GitHub синхронизация са запазени."), "success")
    return redirect(url_for("my_settings"))


@admin_required
def system_backup_now():
    con = get_db()
    folder = db.get_settings(con).get("backup_folder", "").strip()
    try:
        path = backup.local_backup(folder)
        flash(_("Резервно копие е записано: %s") % path, "success")
    except Exception as exc:
        flash(_("Архивирането е неуспешно: %s") % exc, "error")
    return redirect(url_for("my_settings"))


@admin_required
def system_backup_github_now():
    """Стартира качването в GitHub във фонова нишка (backup.trigger_sync_now)
    вместо да блокира заявката, докато трае мрежовата операция (М3).
    Резултатът (успех/грешка) се вижда в статуса на „Настройки“ (sync_status,
    показван и от routes_settings.my_settings) при следващото ѝ зареждане,
    вместо в директен flash веднага след тази заявка."""
    backup.trigger_sync_now(appconfig.load_config)
    flash(_("Качването в GitHub стартира във фонов режим — статусът в „Настройки“ "
         "ще покаже резултата (презаредете страницата след малко)."), "info")
    return redirect(url_for("my_settings"))


@admin_required
def system_pull_now():
    """Ръчно изтегляне на базата данни от GitHub (замества текущата
    локална база!) — за възстановяване или преминаване към споделените
    данни на друга инсталация."""
    cfg = appconfig.load_config()
    ok, err = backup.pull_db(
        cfg.get("gh_owner", ""), cfg.get("gh_repo", ""), cfg.get("gh_token", ""),
        cfg.get("gh_branch", "main") or "main",
        cfg.get("gh_path", "pacho_logistic.db") or "pacho_logistic.db",
        db.DB_PATH,
    )
    if ok:
        flash(_("Базата данни е изтеглена от GitHub. Рестартирайте програмата, "
             "за да заредите новите данни."), "success")
    else:
        flash(_("Изтеглянето от GitHub е неуспешно: %s") % err, "error")
    return redirect(url_for("my_settings"))


# ---------------------------------------------------------------- отдалечен достъп (сканиране с телефон)

@admin_required
def system_remote_start():
    port = int(appconfig.load_config().get("network_port") or 5000)
    remote_tunnel.start(port)
    flash(_("Стартира се отдалечен достъп… изчакайте няколко секунди, статусът "
         "по-долу ще се обнови автоматично."), "info")
    return redirect(url_for("my_settings"))


@admin_required
def system_remote_stop():
    remote_tunnel.stop()
    flash(_("Отдалеченият достъп е спрян."), "success")
    return redirect(url_for("my_settings"))


@admin_required
def system_remote_status():
    return remote_tunnel.status()


# ---------------------------------------------------------------- админ панел

@admin_required
def admin_users():
    con = get_db()
    users = con.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("admin_users.html", users=users)


@admin_required
def admin_user_new():
    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    password = request.form.get("password", "")
    role = "admin" if request.form.get("role") == "admin" else "employee"
    if not username or not password:
        flash(_("Потребителско име и парола са задължителни."), "error")
        return redirect(url_for("admin_users"))
    if len(password) < MIN_PASSWORD_LENGTH:
        flash(_("Паролата трябва да е поне %d символа.") % MIN_PASSWORD_LENGTH, "error")
        return redirect(url_for("admin_users"))
    con = get_db()
    exists = con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    if exists:
        flash(_("Вече има служител с потребителско име „%s“.") % username, "error")
    else:
        # must_change_password=1: администраторът вече знае тази парола
        # (той я е въвел тук), затова не е лична тайна на служителя —
        # задължаваме смяна при първия му вход.
        con.execute(
            "INSERT INTO users"
            " (username, password_hash, full_name, role, active, must_change_password)"
            " VALUES (?, ?, ?, ?, 1, 1)",
            (username, generate_password_hash(password), full_name, role),
        )
        con.commit()
        flash(_("Служителят „%s“ е добавен. Ще трябва да смени паролата при първия вход.") % username, "success")
    return redirect(url_for("admin_users"))


@admin_required
def admin_user_toggle(user_id):
    if user_id == session["user_id"]:
        flash(_("Не можете да деактивирате собствения си акаунт."), "error")
        return redirect(url_for("admin_users"))
    con = get_db()
    con.execute("UPDATE users SET active = 1 - active WHERE id = ?", (user_id,))
    con.commit()
    return redirect(url_for("admin_users"))


@admin_required
def admin_user_password(user_id):
    password = request.form.get("password", "")
    if not password:
        flash(_("Въведете нова парола."), "error")
        return redirect(url_for("admin_users"))
    if len(password) < MIN_PASSWORD_LENGTH:
        flash(_("Паролата трябва да е поне %d символа.") % MIN_PASSWORD_LENGTH, "error")
        return redirect(url_for("admin_users"))
    con = get_db()
    # must_change_password=1 по същата причина, както при admin_user_new —
    # администраторът, не служителят, е избрал тази парола.
    con.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (generate_password_hash(password), user_id))
    con.commit()
    flash(_("Паролата е сменена. Служителят ще трябва да я смени при следващия си вход."), "success")
    return redirect(url_for("admin_users"))


@admin_required
def admin_user_delete(user_id):
    if user_id == session["user_id"]:
        flash(_("Не можете да изтриете собствения си акаунт."), "error")
        return redirect(url_for("admin_users"))
    con = get_db()
    con.execute("UPDATE documents SET created_by = NULL WHERE created_by = ?", (user_id,))
    con.execute("DELETE FROM users WHERE id = ?", (user_id,))
    con.commit()
    flash(_("Служителят е изтрит."), "success")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- обновяване

@login_required
def update_check():
    """Ръчна проверка за нова версия в GitHub Releases."""
    try:
        info = updater.check_for_update()
    except Exception as exc:
        flash(_("Проверката за обновяване е неуспешна: %s") % updater.describe_error(exc), "error")
        return redirect(url_for("dashboard"))
    updater.set_cache(info)  # М5: под заключване (виж updater._cache_lock), не директно
    if info["available"]:
        flash(_("Налична е нова версия %s (текущата е %s).") % (info["latest"], info["current"]), "info")
    else:
        flash(_("Използвате най-новата версия (%s).") % info["current"], "info")
    return redirect(url_for("dashboard"))


@login_required
def update_install():
    """Изтегля новата версия и рестартира програмата."""
    try:
        info = updater.check_for_update()
    except Exception as exc:
        flash(_("Проверката за обновяване е неуспешна: %s") % updater.describe_error(exc), "error")
        return redirect(url_for("dashboard"))
    if not info["available"]:
        flash(_("Вече използвате най-новата версия (%s).") % info["current"], "info")
        return redirect(url_for("dashboard"))
    try:
        updater.install_update(info["download"], info.get("expected_sha256"))
    except Exception as exc:
        flash(_("Обновяването е неуспешно: %s") % updater.describe_error(exc), "error")
        return redirect(url_for("dashboard"))
    return render_template("updating.html", latest=info["latest"])
