# -*- coding: utf-8 -*-
"""Заявка (22.10.2026): „След затваряне програмата профилът, с който е вписан
потребителят, не се затваря“ — прозорецът на програмата пази бисквитките
между пусканията, а бисквитката за вход е 12-часова. Входът вече важи само
до затварянето на програмата (appcore.SERVER_RUN_ID)."""
import appcore


def test_login_stays_valid_while_the_program_runs(admin_client):
    assert admin_client.get("/").status_code == 200
    with admin_client.session_transaction() as sess:
        assert sess["run"] == appcore.SERVER_RUN_ID


def test_session_from_a_previous_run_asks_for_login_again(admin_client, monkeypatch):
    assert admin_client.get("/").status_code == 200
    # Програмата е затворена и пусната отново: ново SERVER_RUN_ID, същата бисквитка.
    monkeypatch.setattr(appcore, "SERVER_RUN_ID", "next-run")
    resp = admin_client.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    with admin_client.session_transaction() as sess:
        assert "user_id" not in sess
    # Страницата за вход се показва (без кръгово пренасочване).
    assert admin_client.get("/login").status_code == 200


def test_language_survives_the_restart_logout(admin_client, monkeypatch):
    with admin_client.session_transaction() as sess:
        sess["lang"] = "en"
    monkeypatch.setattr(appcore, "SERVER_RUN_ID", "next-run")
    admin_client.get("/")
    with admin_client.session_transaction() as sess:
        assert sess.get("lang") == "en"


def test_admin_pages_also_require_login_after_restart(admin_client, monkeypatch):
    monkeypatch.setattr(appcore, "SERVER_RUN_ID", "next-run")
    resp = admin_client.get("/admin/users")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
