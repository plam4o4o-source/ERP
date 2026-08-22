# -*- coding: utf-8 -*-
"""Вход/изход и смяна на парола. Извлечено от app.py (Фаза 3) без промяна
в поведението — виж appcore.py за общите decorator-и/hook-ове."""
from flask import flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _
from werkzeug.security import check_password_hash, generate_password_hash

import applog
import db
import login_guard
import remote_tunnel
from appcore import MIN_PASSWORD_LENGTH, get_db, login_required

# Одит (12.08.2026, находка №15, средна): check_password_hash (scrypt,
# умишлено бавен) се изпълняваше само когато потребителят СЪЩЕСТВУВА
# (`if user and ...` — short-circuit), заради което съществуващо
# потребителско име + грешна парола отнемаше измеримо повече CPU време
# (~120ms) от несъществуващо потребителско име (~0.06ms) — над 2000×
# разлика, тривиално различима дори при мрежов jitter (особено важно, ако
# сървърът е изложен и през отдалечен тунел — виж remote_tunnel.py).
# Съобщението за грешка е едно и също и в двата случая, но времето издава
# истината — изброяване на валидни потребителски имена без нито един
# опит за грешна парола да провокира заключване. Фиксиран dummy хеш,
# изчислен ЕДНОКРАТНО при импортиране на модула (не при всяка заявка —
# това пак би издало разликата), се ползва вместо истинския хеш точно
# когато потребителят липсва/е неактивен, за да отнеме СЪЩОТО CPU време.
_DUMMY_PASSWORD_HASH = generate_password_hash("не-е-истинска-парола-само-за-изравняване-на-времето")


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


def _mask_login(name):
    """Одит (22.08.2026, находка №7): потребителско име за ОДИТНИЯ ЛОГ.

    При НЕуспешен вход въведеното в полето „потребител“ може да е всичко —
    най-често паролата, набрана в грешното поле. Затова в лога отиват само
    първите два знака и дължината: достатъчно, за да се проследи „някой
    упорито опитва да влезе като admin“, но безполезно като парола."""
    text = (name or "").strip()
    if not text:
        return "(празно)"
    if len(text) <= 2:
        return "%s (дължина %d)" % ("*" * len(text), len(text))
    return "%s… (дължина %d)" % (text[:2], len(text))


_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _client_ip_for_rate_limit():
    """Одит (19.08.2026, находка №15, висока): ключът за per-IP лимита на
    входа.

    Поправката на находка №6 (16.08) добави праг по `request.remote_addr` с
    изричната обосновка, че „в тази инсталация няма доверен обратен прокси
    пред waitress“. Това е фактически невярно винаги, когато е включен
    отдалеченият достъп: `cloudflared tunnel --url http://127.0.0.1:<порт>`
    Е точно такъв прокси, и ВСЯКА заявка от интернет пристига с
    `remote_addr == "127.0.0.1"`. Последствията бяха три:

      (а) всички отдалечени потребители деляха ЕДНА кофа от 15 опита;
      (б) един нападател по публичния адрес заключваше входа за всички
          отдалечени потребители — точно DoS-ът, който №6 премахна за
          локалната мрежа, върнат през задната врата;
      (в) локалните потребители (със свои LAN адреси) не бяха защитени от
          нищо, идващо през тунела.

    `CF-Connecting-IP` се ЗАДАВА от edge сървъра на Cloudflare и клиентът
    не може да го подправи през quick tunnel. Затова му вярваме САМО
    когато връзката идва от loopback И тунелът реално работи в момента —
    иначе всеки в локалната мрежа би могъл да си избира произволен ключ за
    лимита, просто като изпрати заглавието."""
    remote = request.remote_addr or ""
    if remote in _LOOPBACK:
        try:
            tunnel_running = remote_tunnel.status().get("status") == "running"
        except Exception:
            tunnel_running = False
        if tunnel_running:
            forwarded = (request.headers.get("CF-Connecting-IP") or "").strip()
            if forwarded:
                return "cf:%s" % forwarded[:64]
    return remote


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
        # Одит (12.08.2026, находка №14, средна): глобален (не по
        # потребителско име) праг — вижте login_guard.register_global_attempt/
        # is_globally_throttled за пълния разказ. Регистрира се БЕЗУСЛОВНО
        # (независимо от резултата), а самата скъпа проверка на паролата
        # по-долу изобщо не се стига, ако прагът е надвишен — спира DoS
        # чрез запълване на всички нишки на сървъра с валидни потребителски
        # имена + произволни грешни пароли.
        login_guard.register_global_attempt()
        # Одит (16.08.2026, находка №6): виж login_guard.register_ip_attempt/
        # is_ip_throttled — допълнителен, ПО-СТРОГ праг ПО IP адрес, за да
        # не заключва ЕДИН нападателски адрес всички останали потребители
        # чрез самия глобален праг по-долу.
        client_ip = _client_ip_for_rate_limit()
        login_guard.register_ip_attempt(client_ip)
        if login_guard.is_ip_throttled(client_ip) or login_guard.is_globally_throttled():
            error = "Твърде много опити за вход в момента. Опитайте отново след малко."
            return render_template("login.html", error=error,
                                   login_scene=db.get_login_scene(get_db()))
        con = get_db()
        user = con.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        # Одит (12.08.2026, находка №15): при липсващ/неактивен потребител
        # СЪЩО се изпълнява check_password_hash — върху фиксиран dummy хеш
        # (виж _DUMMY_PASSWORD_HASH по-горе), за да отнеме СЪЩОТО време
        # като истинската проверка по-долу, вместо short-circuit
        # `if user and ...` директно да прескочи скъпата стъпка.
        if not (user and user["active"]):
            check_password_hash(_DUMMY_PASSWORD_HASH, password)
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
            # Одит (16.08.2026, находка №5): PERMANENT_SESSION_LIFETIME
            # (виж appcore.create_app, 12 часа) НЯМА никакъв ефект, докато
            # session.permanent не е True — Flask издава сесийна бисквитка
            # БЕЗ изтичане (валидна, докато браузърът не я изтрие сам,
            # потенциално неограничено на постоянно включена машина), а не
            # 12-часова, каквато конфигурацията всъщност цели. session.
            # permanent=True активира реално configured TTL.
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            session["theme"] = theme
            session["lang"] = user_lang or chosen_before_login or db.DEFAULT_LANGUAGE
            session["must_change_password"] = bool(user["must_change_password"])
            # Одит (16.08.2026, находка №5): виж db._m007_session_epoch —
            # запазва версията на паролата, каквато е БИЛА при издаването
            # на тази бисквитка, за да може appcore._session_user_
            # deactivated_or_missing да я прекрати при по-късна смяна.
            session["session_epoch"] = user["session_epoch"]
            applog.log_audit("успешен вход", "потребител=%s" % username)  # находка №51
            target = _safe_next_target(request.args.get("next")) or url_for("dashboard")
            return redirect(target)
        locked, wait_seconds = login_guard.is_locked_out(username)
        if locked:
            wait_minutes = max(1, (wait_seconds + 59) // 60)
            error = ("Твърде много неуспешни опити за вход. Опитайте отново след "
                     "около %d мин." % wait_minutes)
            applog.log_audit("отказан вход (заключен акаунт)",
                             "потребител=%s" % _mask_login(username))  # находки №51/№7
        else:
            login_guard.register_failure(username)
            # Одит (22.08.2026, находка №7): потребителското име се МАСКИРА.
            #
            # Докстрингът на log_audit обещава „никога пароли“, но най-честата
            # грешка при вход е паролата да бъде набрана в полето за име (или
            # двете полета да са разменени) — и тогава тя влизаше в лога в
            # ЧИСТ ТЕКСТ, в файл до .exe-то, който се синхронизира и с
            # бекъпите. Възпроизведено. За целта на одитната следа („някой
            # опитва да влезе като X“) първите два знака + дължината са
            # напълно достатъчни, а паролата остава неразчитаема.
            applog.log_audit("неуспешен вход", "потребител=%s" % _mask_login(username))
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
        # Одит (12.08.2026, находка №29, дребна): тази форма изисква вече
        # валидна сесия, затова практическата стойност на brute-force атака
        # тук е ниска (нападател с валидна сесия не се нуждае от паролата,
        # за да прави каквото сесията позволява) — НО при открадната/
        # фалшифицирана сесия (виж db._harden_secret_key_permissions,
        # находка №1) позволяваше НЕОГРАНИЧЕН brute-force на РЕАЛНАТА
        # парола именно през тази форма. Споделя СЪЩИЯ login_guard механизъм
        # като /login, с отделен ключ по user_id (не по username — тук
        # винаги е известен от сесията), за последователност.
        guard_key = "pwdchange:%s" % session["user_id"]
        locked, wait_seconds = login_guard.is_locked_out(guard_key)
        if locked:
            wait_minutes = max(1, (wait_seconds + 59) // 60)
            flash(_("Твърде много грешни опити. Опитайте отново след около %d мин.")
                 % wait_minutes, "error")
            return render_template("change_password.html",
                                   forced=session.get("must_change_password", False))
        user = con.execute("SELECT * FROM users WHERE id = ?",
                           (session["user_id"],)).fetchone()
        if not check_password_hash(user["password_hash"], current):
            login_guard.register_failure(guard_key)
            flash(_("Текущата парола е грешна."), "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(_("Новата парола трябва да е поне %d символа.") % MIN_PASSWORD_LENGTH, "error")
        elif new != repeat:
            flash(_("Двете нови пароли не съвпадат."), "error")
        else:
            login_guard.clear(guard_key)
            # Одит (16.08.2026, находка №5): session_epoch = session_epoch+1
            # прекратява ВСЯКА друга вече отворена сесия на този потребител
            # (напр. открадната бисквитка) — виж db._m007_session_epoch и
            # appcore._session_user_deactivated_or_missing. session["session_
            # epoch"] тук се обновява СЪЩО, за да не изкара текущата,
            # легитимна сесия на самия потребител, направил смяната.
            con.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0,"
                " session_epoch = session_epoch + 1 WHERE id = ?",
                (generate_password_hash(new), session["user_id"]))
            con.commit()
            new_epoch = con.execute(
                "SELECT session_epoch FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()["session_epoch"]
            session["must_change_password"] = False
            session["session_epoch"] = new_epoch
            applog.log_audit("сменена собствена парола")  # находка №51
            flash(_("Паролата е сменена успешно."), "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html", forced=session.get("must_change_password", False))
