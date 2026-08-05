# -*- coding: utf-8 -*-
"""Тестове за помощните функции на слоя данни (настройки, пунктове за разтоварване)."""


def test_settings_roundtrip(con, db_module):
    db_module.save_settings(con, {"sender_name": "Тест ЕООД", "sender_city": "София"})
    con.commit()
    settings = db_module.get_settings(con)
    assert settings["sender_name"] == "Тест ЕООД"
    assert settings["sender_city"] == "София"


def test_settings_upsert_overwrites(con, db_module):
    db_module.save_settings(con, {"sender_name": "Първо име"})
    db_module.save_settings(con, {"sender_name": "Второ име"})
    con.commit()
    assert db_module.get_settings(con)["sender_name"] == "Второ име"


def test_default_sender_seeded(con, db_module):
    settings = db_module.get_settings(con)
    # init_db засява реквизитите на фирмата по подразбиране.
    assert "sender_name" in settings
    assert settings["sender_eik"] == "205284599"


def _make_client(con):
    cur = con.execute("INSERT INTO clients (name) VALUES ('Клиент АД')")
    return cur.lastrowid


def test_unload_points_roundtrip(con, db_module):
    cid = _make_client(con)
    db_module.save_unload_points(con, cid, [
        {"label": "Склад 1", "address": "ул. Индустриална 5", "city": "Пловдив",
         "postcode": "4000", "country": "България"},
        {"label": "Склад 2", "address": "бул. Европа 10", "city": "Варна",
         "postcode": "9000", "country": "България"},
    ])
    con.commit()
    points = db_module.get_unload_points(con, cid)
    assert len(points) == 2
    assert points[0]["label"] == "Склад 1"
    assert points[1]["city"] == "Варна"


def test_unload_points_replace_semantics(con, db_module):
    cid = _make_client(con)
    db_module.save_unload_points(con, cid, [{"label": "Стар"}])
    db_module.save_unload_points(con, cid, [{"label": "Нов"}])
    con.commit()
    points = db_module.get_unload_points(con, cid)
    # save_unload_points заменя (изтрива старите, вмъква новите).
    assert len(points) == 1
    assert points[0]["label"] == "Нов"


def test_unload_points_skips_empty_rows(con, db_module):
    cid = _make_client(con)
    db_module.save_unload_points(con, cid, [
        {"label": "", "address": "", "city": "", "postcode": "", "country": ""},
        {"label": "Реален", "address": "", "city": "", "postcode": "", "country": ""},
    ])
    con.commit()
    points = db_module.get_unload_points(con, cid)
    assert len(points) == 1
    assert points[0]["label"] == "Реален"


def test_unload_points_map_groups_by_client(con, db_module):
    c1 = _make_client(con)
    c2 = con.execute("INSERT INTO clients (name) VALUES ('Втори')").lastrowid
    db_module.save_unload_points(con, c1, [{"label": "A"}, {"label": "B"}])
    db_module.save_unload_points(con, c2, [{"label": "C"}])
    con.commit()
    m = db_module.get_unload_points_map(con, [c1, c2])
    assert len(m[c1]) == 2
    assert len(m[c2]) == 1


def test_user_theme_default_and_validation(con, db_module):
    uid = con.execute(
        "INSERT INTO users (username, password_hash) VALUES ('t', 'x')"
    ).lastrowid
    assert db_module.get_user_theme(con, uid) == db_module.DEFAULT_THEME
    db_module.save_user_settings(con, uid, {"theme": "dark"})
    con.commit()
    assert db_module.get_user_theme(con, uid) == "dark"
    # Невалидна тема => връща се темата по подразбиране.
    db_module.save_user_settings(con, uid, {"theme": "не-съществува"})
    con.commit()
    assert db_module.get_user_theme(con, uid) == db_module.DEFAULT_THEME
