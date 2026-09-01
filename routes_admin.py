# -*- coding: utf-8 -*-
"""Административен панел: системни настройки (мрежа/локален архив), отдалечен
достъп (Cloudflare тунел), управление на служители, и проверка/инсталиране на
обновления. Извлечено от app.py (Фаза 3).

Бележка (25.08.2026): синхронизацията с GitHub беше премахната по заявка на
потребителя — заедно с нея отпаднаха и системните ѝ настройки/бутони тук
(качване/изтегляне от GitHub). Локалният архив (папка/мрежов диск) остана."""
from flask import abort, flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _
from werkzeug.security import generate_password_hash

import applog
import backup
import config as appconfig
import db
import remote_tunnel
import updater
from appcore import MIN_PASSWORD_LENGTH, admin_required, get_db, get_runtime_port

import re as _re
from urllib.parse import urlsplit

#: Одит (25.08.2026, предложение Д): груба, но достатъчна проверка за
#: „прилича ли на хост“ — букви/цифри/тире в етикети, разделени с точки
#: (домейн), или чист IPv4. Целта не е RFC-пълнота, а да отсече очевидно
#: невалидните адреси (без домейн, с интервал, с „?“/път), които иначе биха
#: влезли в печатния QR код като траен неработещ линк.
_HOSTNAME_RE = _re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _public_base_url_error(raw):
    """Връща съобщение за грешка, ако `raw` не е годен постоянен публичен
    адрес (предложение Д), или None, ако е наред. Очаква вече добавена схема
    (http/https) от извикващия."""
    # Интервалът се проверява ПЪРВИ и с първоначалното съобщение (пази
    # съвместимост с регресионния тест от находка №2/gaps) — той и обърква
    # разбора по-долу.
    if " " in raw:
        return _("Адресът не изглежда валиден — не трябва да съдържа интервали.")
    try:
        parts = urlsplit(raw)
    except ValueError:
        return _("Адресът не изглежда валиден.")
    if parts.scheme not in ("http", "https"):
        return _("Адресът трябва да започва с http:// или https://.")
    # netloc може да включва порт (и по изключение потребител@) — за проверката
    # ни трябва само хостът.
    host = parts.hostname or ""
    if not host:
        return _("Адресът трябва да съдържа домейн (напр. https://firma.example.com).")
    # Път/заявка/фрагмент нямат място в базов адрес — те се долепят по-късно
    # при строенето на конкретния линк към документа.
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return _("Въведете само адреса на сайта, без път или параметри след домейна.")
    if not _HOSTNAME_RE.match(host):
        return _("Домейнът в адреса не изглежда валиден.")
    return None


def register(app):
    app.add_url_rule("/admin/system", "system_settings", system_settings, methods=["GET", "POST"])
    app.add_url_rule("/admin/system/backup-now", "system_backup_now",
                     system_backup_now, methods=["POST"])
    # Бележка (25.08.2026): маршрутите /admin/system/backup-github-now и
    # /admin/system/pull-now (качване/изтегляне от GitHub) отпаднаха заедно с
    # премахнатата синхронизация с GitHub. Локалният архив остана.
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
        # Дребни (одит): int(request.form.get("network_port")) гърмеше с
        # необработен ValueError (500 грешка) при НЕЧИСЛОВ или празен след
        # strip() вход (напр. случайно вмъкнат текст в полето) — вместо
        # ясно съобщение „невалиден порт“. Проверяваме и допустимия
        # диапазон на TCP порт (1-65535), не само че е число.
        port_raw = request.form.get("network_port", "").strip()
        try:
            port = int(port_raw) if port_raw else 5000
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            flash(_("Невалиден мрежов порт — въведете число между 1 и 65535."), "error")
            return redirect(url_for("my_settings"))
        # Одит (31.08.2026, находка №11): пътят до базата се валидира също
        # толкова строго, колкото порта над него. Печатна грешка тук е
        # най-скъпата в цялата програма — виж config.validate_db_path.
        db_error, db_path_value = appconfig.validate_db_path(
            request.form.get("db_path", ""),
            allow_new=request.form.get("db_path_new") == "on")
        if db_error:
            flash(db_error, "error")
            return redirect(url_for("my_settings"))
        appconfig.save_config({
            "db_path": db_path_value,
            "network_mode": request.form.get("network_mode") == "on",
            "network_port": port,
        })
        applog.log_audit("променени мрежови настройки",
                        "db_path=%r, network_mode=%s, port=%s" % (
                            db_path_value,
                            request.form.get("network_mode") == "on", port))  # находка №51
        flash(_("Мрежовите настройки са запазени. Рестартирайте програмата, "
             "за да влязат в сила."), "success")
    elif form == "login_scene":
        # Изглед на входния екран (заявка: „запази този [класическия] и
        # добави опция да може да се сменя в настройките“) — обща
        # настройка за инсталацията, виж db.LOGIN_SCENES/get_login_scene.
        scene = request.form.get("login_scene", "")
        if scene not in db.LOGIN_SCENES:
            scene = db.DEFAULT_LOGIN_SCENE
        db.save_settings(con, {"login_scene": scene})
        con.commit()
        flash(_("Изгледът на входния екран е запазен."), "success")
    elif form == "backup_folder":
        db.save_settings(con, {
            "backup_folder": request.form.get("backup_folder", "").strip(),
            "backup_auto": "on" if request.form.get("backup_auto") == "on" else "",
        })
        con.commit()
        flash(_("Настройките за локален/мрежов архив са запазени."), "success")
    elif form == "public_base_url":
        # Одит (22.08.2026, находка №2): постоянният публичен адрес, който
        # влиза в QR кода на ПЕЧАТНАТА бланка. Виж routes_documents.
        # _public_doc_url: тунелният адрес е ефимерен (Cloudflare преизползва
        # поддомейните), затова върху хартия има работа само стабилен адрес.
        raw = request.form.get("public_base_url", "").strip()
        if raw and not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        # Одит (25.08.2026, предложение Д): валидираме ХОСТА, не само за
        # интервали. Този адрес влиза буквално в QR кода на ПЕЧАТНАТА бланка;
        # невалиден хост (напр. „https://“ без домейн, „https://???“, или адрес
        # с път/интервал) означаваше траен неработещ QR върху официален
        # документ — открива се чак когато насрещната страна не може да отвори
        # линка. По-добре ясна грешка при запис, отколкото мълчаливо счупен
        # печат. ВАЖНО: валидацията е ПРЕДИ rstrip("/") — иначе „https://“ се
        # окастряше до „https:“ и после минаваше като (безсмислен) хост.
        if raw:
            invalid = _public_base_url_error(raw)
            if invalid:
                flash(invalid, "error")
                return redirect(url_for("my_settings"))
        # Съхраняваме без завършващ „/“ (конкретният линк го долепя сам).
        raw = raw.rstrip("/")
        db.save_settings(con, {"public_base_url": raw})
        con.commit()
        applog.log_audit("променен постоянен публичен адрес", "url=%s" % (raw or "(изчистен)"))
        flash(_("Постоянният публичен адрес е запазен. Новоотпечатаните QR кодове "
                "ще го ползват.") if raw else
              _("Постоянният публичен адрес е изчистен — QR кодовете отново ще "
                "ползват локалния адрес."), "success")
    elif form == "client_export":
        db.save_settings(con, {
            "client_export_dir": request.form.get("client_export_dir", "").strip(),
            "client_export_auto": "on" if request.form.get("client_export_auto") == "on" else "",
        })
        con.commit()
        flash(_("Настройките за клиентски папки са запазени."), "success")
    # Бележка (25.08.2026): формата „backup_github“ (настройки за GitHub
    # синхронизация) отпадна заедно с премахнатата функция. Остана само
    # локалният архив (формата „backup_folder“ по-горе).
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


# Бележка (25.08.2026): функциите system_backup_github_now (качване в GitHub)
# и system_pull_now (изтегляне от GitHub) отпаднаха заедно с премахнатата
# синхронизация с GitHub. Локалният архив остана (system_backup_now по-горе).


# ---------------------------------------------------------------- отдалечен достъп (сканиране с телефон)

@admin_required
def system_remote_start():
    # Одит (12.08.2026, находка №10): реално използваният порт (може да е
    # различен от конфигурирания, ако е бил зает при стартиране — виж
    # appcore.set_runtime_port/app.py) вместо сляпо да се чете
    # конфигурацията, която може да сочи към вече незает от тази сесия
    # порт.
    configured_port = appconfig.get_network_port(appconfig.load_config())
    port = get_runtime_port(configured_port)
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


#: Одит (31.08.2026, находка №1, ВИСОКА): съобщението е едно и също за
#: деактивиране и за изтриване — и в двата случая проблемът е един: това е
#: последният администратор, който още може да влезе.
_LAST_ADMIN_MSG = _(
    "Това е последният активен администратор — ако го деактивирате или изтриете, "
    "никой няма да може да влезе в администрацията (управление на служители, "
    "системни настройки, архивиране, обновяване). Първо направете друг "
    "потребител администратор.")


def _would_leave_no_active_admin(con, user_id):
    """Одит (31.08.2026, находка №1, ВИСОКА): вярно, ако след деактивиране/
    изтриване на `user_id` НЕ би останал нито един активен администратор.

    Досега единствената защита беше „не можеш да пипнеш СЕБЕ СИ“. Тя пази
    инварианта последователно (последният админ не може да пипне себе си),
    но НЕ и конкурентно: адмиН A изпълнява `UPDATE … WHERE id=B`, докато в
    друга нишка на waitress адмиН B изпълнява огледалното за A. И двете
    заявки минават проверката „не съм аз“ и двете commit-ват.

    Проверено с изпълнение (2 админа, 2 тестови клиента, threading.Barrier):
    и двата POST-а върнаха 302, крайно състояние — НУЛА активни
    администратора. Рестартът не помага: db.init_db засява „admin“ само при
    ПРАЗНА таблица users, а тук потребители има. Оттам нататък управлението
    на служители, системните настройки (вкл. пътя до базата), архивирането и
    обновяването са недостъпни завинаги; изходът е ръчна редакция на .db
    файла — нещо, което тази потребителска група не може да направи.

    Извиква се ВИНАГИ вътре в отворена `BEGIN IMMEDIATE` транзакция (виж
    двата извикващи маршрута) — само така проверката и самата промяна са
    едно неделимо цяло и второто едновременно деактивиране вижда вече
    намаленото множество."""
    return con.execute(
        "SELECT COUNT(*) AS c FROM users"
        " WHERE role = 'admin' AND active = 1 AND id <> ?",
        (user_id,)).fetchone()["c"] == 0


@admin_required
def admin_user_toggle(user_id):
    if user_id == session["user_id"]:
        flash(_("Не можете да деактивирате собствения си акаунт."), "error")
        return redirect(url_for("admin_users"))
    # Одит (31.08.2026, находка №13): очакваното състояние идва от реда,
    # който администраторът е ВИЖДАЛ (скрито поле в admin_users.html).
    # Липсва (стара отворена страница) → третираме го като „не знам“ и
    # пропускаме проверката, за да не счупим работещ поток.
    expected_raw = request.form.get("expected_active", "")
    expected = expected_raw if expected_raw in ("0", "1") else None

    con = get_db()
    # BEGIN IMMEDIATE: проверката за „последен администратор“ и самата
    # промяна трябва да са неделими (находка №1) — иначе две едновременни
    # деактивирания и двете виждат „има още един активен“.
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT active, role FROM users WHERE id = ?",
                          (user_id,)).fetchone()
        if row is None:
            con.rollback()
            abort(404)
        deactivating = bool(row["active"])
        if deactivating and row["role"] == "admin" and _would_leave_no_active_admin(con, user_id):
            con.rollback()
            flash(_LAST_ADMIN_MSG, "error")
            return redirect(url_for("admin_users"))
        if expected is not None:
            # Находка №13: условен UPDATE + проверка на rowcount, същият
            # оптимистичен модел като при документите.
            cur = con.execute(
                "UPDATE users SET active = 1 - active WHERE id = ? AND active = ?",
                (user_id, int(expected)))
            if cur.rowcount == 0:
                con.rollback()
                flash(_("Състоянието на този акаунт е било променено междувременно "
                        "— страницата е презаредена, проверете и опитайте пак."),
                      "warning")
                return redirect(url_for("admin_users"))
        else:
            con.execute("UPDATE users SET active = 1 - active WHERE id = ?", (user_id,))
        con.commit()
    except Exception:
        con.rollback()
        raise
    applog.log_audit("променено състояние на служител",
                     "user_id=%s активен=%s" % (user_id, 0 if deactivating else 1))
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
    # session_epoch = session_epoch + 1 (одит 16.08.2026, находка №5): виж
    # db._m007_session_epoch — прекратява ВСЯКА вече отворена сесия на този
    # потребител (напр. служителят е забравил да излезе на споделен
    # компютър — администраторът сменя паролата именно, за да го изкара).
    con.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1,"
        " session_epoch = session_epoch + 1 WHERE id = ?",
        (generate_password_hash(password), user_id))
    con.commit()
    applog.log_audit("нулирана парола на служител", "user_id=%s" % user_id)  # находка №51
    flash(_("Паролата е сменена. Служителят ще трябва да я смени при следващия си вход."), "success")
    return redirect(url_for("admin_users"))


@admin_required
def admin_user_delete(user_id):
    if user_id == session["user_id"]:
        flash(_("Не можете да изтриете собствения си акаунт."), "error")
        return redirect(url_for("admin_users"))
    con = get_db()
    # Одит (31.08.2026, находка №1): същата неделима проверка като при
    # деактивирането — изтриването на последния активен администратор
    # заключва фирмата извън собствената ѝ програма необратимо.
    con.execute("BEGIN IMMEDIATE")
    row = con.execute("SELECT id, role, active FROM users WHERE id = ?",
                      (user_id,)).fetchone()
    if row is None:
        con.rollback()
        abort(404)
    if row["role"] == "admin" and row["active"] and _would_leave_no_active_admin(con, user_id):
        con.rollback()
        flash(_LAST_ADMIN_MSG, "error")
        return redirect(url_for("admin_users"))
    con.execute("UPDATE documents SET created_by = NULL WHERE created_by = ?", (user_id,))
    # Одит (19.08.2026, находка №16): и прикачените файлове. `document_
    # attachments.uploaded_by` е външен ключ към users БЕЗ ON DELETE
    # правило — преди тази поправка изтриването на служител, който някога е
    # качвал прикачен файл (сканирана подписана бланка и т.н.), гърмеше с
    # „FOREIGN KEY constraint failed“, а потребителят виждаше генеричното
    # „Възникна неочаквана грешка“. Проверено с изпълнение: такъв служител
    # оставаше неизтриваем ЗАВИНАГИ, без никакво обяснение защо. NULL е
    # правилното поведение и тук — самият прикачен файл остава при
    # документа, губи се само авторството (както при documents.created_by).
    con.execute("UPDATE document_attachments SET uploaded_by = NULL WHERE uploaded_by = ?",
                (user_id,))
    con.execute("DELETE FROM users WHERE id = ?", (user_id,))
    con.commit()
    applog.log_audit("изтрит служител", "user_id=%s" % user_id)  # находка №51
    flash(_("Служителят е изтрит."), "success")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------- обновяване

@admin_required
def update_check():
    """Ръчна проверка за нова версия в GitHub Releases.

    Одит (находка В5, висок риск): само @login_required преди поправката
    — всеки служител можеше да задейства инсталиране на нова версия
    (update_install по-долу РЕСТАРТИРА цялата програма за всички
    едновременно работещи потребители, viz. находка В6), без изобщо да е
    администратор."""
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


@admin_required
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
        # Одит (31.08.2026, находка №6): ръчният бутон СЪЗНАТЕЛНО пренебрегва
        # маркера за провалена подмяна — админът натиска „Обнови сега“ именно
        # след като е отстранил причината (затворил е програмата на другите
        # компютри, изключил е антивирусната блокировка). Автоматичният път
        # уважава маркера и така не се върти безкрайно.
        updater.clear_failed_install_marker()
        updater.install_update(info["download"], info.get("expected_sha256"),
                               version=info.get("latest"), ignore_failed_marker=True)
    except Exception as exc:
        flash(_("Обновяването е неуспешно: %s") % updater.describe_error(exc), "error")
        return redirect(url_for("dashboard"))
    # Дребни (одит): бланката updating.html показваше ТВЪРДО закодиран
    # http://127.0.0.1:5000 — ако администраторът е сменил мрежовия порт в
    # „Системни настройки“ (виж system_settings по-горе), този адрес е
    # ПОГРЕШЕН след рестарт, а операторът остава без работещ адрес.
    # Одит (12.08.2026, находка №10): реално използваният порт (виж
    # system_remote_start по-горе за същото разсъждение) вместо сляпо
    # четене на конфигурацията — важно при fallback на зает порт.
    configured_port = appconfig.get_network_port(appconfig.load_config())
    port = get_runtime_port(configured_port)
    return render_template("updating.html", latest=info["latest"], local_port=port)
