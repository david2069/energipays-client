import logging

import energipays


def test_mask_email():
    assert energipays.mask_email("user@example.com") == "u•••r@example.com"
    assert energipays.mask_email("ab@example.com") == "a•••@example.com"
    assert energipays.mask_email("") == ""
    assert energipays.mask_email("not-an-email") == "•••"


def test_login_does_not_log_the_email(client, caplog):
    client.session.add("GET", "/sanctum/csrf-cookie", __import__("conftest").Resp(200, {}))
    client.session.add("GET", "/", __import__("conftest").Resp(200, {}))
    client.session.add("POST", "/api/auth/login", __import__("conftest").Resp(401, {"message": "no"}))
    with caplog.at_level(logging.DEBUG, logger="energipays"):
        client.login()
    assert "user@example.com" not in caplog.text
    assert "u•••r@example.com" in caplog.text
