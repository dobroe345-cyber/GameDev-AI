"""Вход по почте: отправка писем, одноразовые коды, сессии, защита от перебора и от чужих адресов."""
import json
import sqlite3

import pytest

from auth import CODE_TTL, GLOBAL_HOUR, MAX_ATTEMPTS, PER_EMAIL_HOUR, RESEND_GAP, SESSION_TTL, AuthError, AuthService
from db import Database
from fake_mail import FakeMail
from mailer import Mailer, MailError
from services import build_services
from test_api_http import TOKEN, Server, make_settings
from test_code_agent import Script

ME = "me@example.com"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def mail():
    m = FakeMail()
    yield m
    m.stop()


@pytest.fixture
def auth(tmp_path, mail):
    db = Database(tmp_path / "a.db")
    db.migrate()
    clock = Clock()
    mailer = Mailer("resend", "re_secretkey123", "GameDev AI <onboarding@resend.dev>", api_url=mail.url, timeout=3)
    a = AuthService(db, mailer, "server-secret", allowed=(ME,), clock=clock)
    a.clock_obj, a.mail, a.db_obj = clock, mail, db
    return a


# ------------------------------------------------------------------ письма
def test_resend_request_format_and_key_only_in_header(mail):
    Mailer("resend", "re_secretkey123", "GameDev AI <onboarding@resend.dev>", api_url=mail.url).send("a@b.co", "Тема", "Текст 123456")
    m = mail.sent[0]
    assert m["path"] == "/emails" and m["headers"]["authorization"] == "Bearer re_secretkey123"
    assert m["body"]["from"] == "GameDev AI <onboarding@resend.dev>" and m["body"]["to"] == ["a@b.co"] and m["subject"] == "Тема"
    assert "re_secretkey123" not in json.dumps(m["body"])


def test_brevo_request_format(mail):
    Mailer("brevo", "xkeysib-1", "GameDev AI <me@example.com>", api_url=mail.url).send("a@b.co", "Тема", "Текст")
    m = mail.sent[0]
    assert m["path"] == "/v3/smtp/email" and m["headers"]["api-key"] == "xkeysib-1"
    assert m["body"]["sender"] == {"email": "me@example.com", "name": "GameDev AI"} and m["body"]["to"] == [{"email": "a@b.co"}]


def test_mail_errors_are_readable_and_never_leak_the_key(mail):
    mailer = Mailer("resend", "re_secretkey123", "onboarding@resend.dev", api_url=mail.url, timeout=2)
    mail.fail_status = 403
    with pytest.raises(MailError) as e:
        mailer.send("a@b.co", "t", "x")
    assert "только на почту" in str(e.value) and "re_secretkey123" not in str(e.value)
    mail.fail_status = 429
    with pytest.raises(MailError) as e:
        mailer.send("a@b.co", "t", "x")
    assert "подождать" in str(e.value)
    with pytest.raises(MailError) as e:
        Mailer("resend", "re_secretkey123", "x@y.z", api_url="http://127.0.0.1:1", timeout=1).send("a@b.co", "t", "x")
    assert "недоступен" in str(e.value) and "re_secretkey123" not in str(e.value)
    with pytest.raises(ValueError):
        Mailer("unknown", "k", "a@b.c")


def test_mailer_from_settings_needs_key():
    from settings import Settings
    assert Mailer.from_settings(Settings.from_env({})) is None
    m = Mailer.from_settings(Settings.from_env({"MAIL_API_KEY": "k", "MAIL_PROVIDER": "Brevo"}))
    assert m.provider == "brevo"
    assert Mailer.from_settings(Settings.from_env({"MAIL_API_KEY": "k", "MAIL_PROVIDER": "nope"})) is None


# ------------------------------------------------------------------ коды и сессии
def test_login_flow_code_session_and_logout(auth):
    auth.request_code("  ME@Example.com ")
    assert len(auth.mail.sent) == 1 and auth.mail.sent[0]["to"] == ME
    code = auth.mail.last_code()
    assert len(code) == 6 and code in auth.mail.sent[0]["subject"]
    s = auth.verify(ME, code)
    assert s["token"].startswith("gdai_") and s["email"] == ME and s["expires_at"] > auth.clock_obj() + SESSION_TTL - 5
    assert auth.check_session(s["token"]) == ME
    assert auth.check_session("gdai_wrong") is None and auth.check_session("t" * 40) is None
    assert auth.logout(s["token"]) and auth.check_session(s["token"]) is None
    with pytest.raises(AuthError):
        auth.verify(ME, code)                                           # код одноразовый


def test_nothing_secret_is_stored_in_plain_text(auth):
    auth.request_code(ME)
    code = auth.mail.last_code()
    tok = auth.verify(ME, code)["token"]
    dump = " ".join(str(v) for t in ("auth_codes", "sessions") for r in auth.db_obj.query(f"SELECT * FROM {t}") for v in r.values())
    assert code not in dump and tok not in dump


def test_wrong_code_attempts_are_limited_and_code_is_burned(auth):
    auth.request_code(ME)
    code = auth.mail.last_code()
    wrong = "000000" if code != "000000" else "111111"
    for i in range(MAX_ATTEMPTS):
        with pytest.raises(AuthError) as e:
            auth.verify(ME, wrong)
        assert e.value.status == 400
    with pytest.raises(AuthError) as e:
        auth.verify(ME, code)                                           # даже верный код после перебора не принимается
    assert e.value.status == 429


def test_code_expires_and_new_request_invalidates_old_code(auth):
    auth.request_code(ME)
    old = auth.mail.last_code()
    auth.clock_obj.advance(RESEND_GAP + 1)
    auth.request_code(ME)
    new = auth.mail.last_code()
    if new != old:
        with pytest.raises(AuthError):
            auth.verify(ME, old)
    auth.clock_obj.advance(CODE_TTL + 1)
    with pytest.raises(AuthError) as e:
        auth.verify(ME, new)
    assert "истёк" in str(e.value)


def test_resend_gap_and_hourly_limits(auth):
    auth.request_code(ME)
    with pytest.raises(AuthError) as e:
        auth.request_code(ME)
    assert e.value.status == 429 and "через" in str(e.value)
    for _ in range(PER_EMAIL_HOUR - 1):
        auth.clock_obj.advance(RESEND_GAP + 1)
        auth.request_code(ME)
    auth.clock_obj.advance(RESEND_GAP + 1)
    with pytest.raises(AuthError) as e:
        auth.request_code(ME)
    assert e.value.status == 429 and "час" in str(e.value)


def test_strangers_get_the_same_answer_but_no_mail_and_cannot_log_in(auth):
    auth.request_code("evil@example.com")                              # ответ тот же, что для своих: ошибки нет
    assert auth.mail.sent == []
    with pytest.raises(AuthError):
        auth.verify("evil@example.com", "123456")
    # чужие запросы не расходуют общий лимит писем и не блокируют владельца
    for i in range(GLOBAL_HOUR + 5):
        auth.request_code(f"x{i}@example.com")
    auth.request_code(ME)
    assert len(auth.mail.sent) == 1


def test_link_mode_adds_the_email_to_allowed_users(auth):
    other = "second@example.com"
    auth.request_code(other, link=True)                                 # владелец (вошедший) привязывает вторую почту
    s = auth.verify(other, auth.mail.last_code(other))
    assert s["linked"] is True and auth.is_allowed(other)
    auth.logout(s["token"])
    auth.clock_obj.advance(RESEND_GAP + 1)
    auth.request_code(other)                                           # теперь обычный вход работает без привязки
    assert auth.verify(other, auth.mail.last_code(other))["linked"] is False


def test_mail_failure_and_missing_mailer_are_reported(auth, tmp_path):
    auth.mail.fail_status = 403
    with pytest.raises(AuthError) as e:
        auth.request_code(ME)
    assert e.value.status == 502 and "resend" in str(e.value).lower()
    auth.mail.fail_status = 0
    auth.clock_obj.advance(RESEND_GAP + 1)
    auth.request_code(ME)                                              # после сбоя можно запросить снова
    assert auth.verify(ME, auth.mail.last_code())["email"] == ME
    nomail = AuthService(auth.db_obj, None, "s", allowed=(ME,))
    with pytest.raises(AuthError) as e:
        nomail.request_code(ME)
    assert e.value.status == 503 and "MAIL_API_KEY" in str(e.value)


@pytest.mark.parametrize("bad", ["", "no-at", "a@b", "a b@c.de", "<x>@y.zz", "a@b.c", "x" * 130 + "@e.com"])
def test_bad_emails_are_rejected(auth, bad):
    with pytest.raises(AuthError) as e:
        auth.request_code(bad)
    assert e.value.status == 400


def test_old_sessions_are_trimmed_and_expire(auth):
    tokens = []
    for i in range(12):
        auth.request_code(ME)
        tokens.append(auth.verify(ME, auth.mail.last_code())["token"])
        auth.clock_obj.advance(RESEND_GAP + 1) if i % 5 == 4 else auth.clock_obj.advance(1)
        auth.db_obj.execute("DELETE FROM auth_codes")                  # тест не про лимиты запросов
    alive = [t for t in tokens if auth.check_session(t)]
    assert len(alive) == 10 and tokens[-1] in alive and tokens[0] not in alive
    auth.clock_obj.advance(SESSION_TTL + 1)
    assert auth.check_session(tokens[-1]) is None


# ------------------------------------------------------------------ через настоящий HTTP
def test_http_login_by_email_replaces_token(tmp_path, mail):
    mailer = Mailer("resend", "re_k", "onboarding@resend.dev", api_url=mail.url, timeout=3)
    s = Server(make_settings(tmp_path, {}, ALLOWED_EMAILS=ME), lambda st: build_services(st, router=Script(), mailer=mailer))
    try:
        assert s.j("GET", "/api/auth/info", token=None)[1] == {"email_login": True}
        assert s.j("GET", "/api/status", token=None)[0] == 401
        assert s.j("POST", "/api/auth/request", {"email": ME}, token=None) == (200, {"sent": True, "link": False})
        assert s.j("POST", "/api/auth/verify", {"email": ME, "code": "000000"}, token=None)[0] == 400
        code, out = s.j("POST", "/api/auth/verify", {"email": ME, "code": mail.last_code()}, token=None)
        assert code == 200 and out["email"] == ME
        sess = out["token"]
        assert s.j("GET", "/api/status", token=sess)[0] == 200                 # сессия работает как токен
        assert s.j("GET", "/api/auth/me", token=sess)[1] == {"user": ME}
        assert s.j("GET", "/api/auth/me")[1] == {"user": "token"}              # запасной вход по API_TOKEN не сломан
        assert s.j("GET", "/api/status", token="gdai_" + "x" * 40)[0] == 401
        assert s.j("POST", "/api/auth/logout", token=sess)[1]["revoked"] is True
        assert s.j("GET", "/api/status", token=sess)[0] == 401
        # чужая почта: тот же ответ, письма нет
        n = len(mail.sent)
        assert s.j("POST", "/api/auth/request", {"email": "evil@example.com"}, token=None)[0] == 200 and len(mail.sent) == n
        assert s.j("POST", "/api/auth/request", {"email": "bad"}, token=None)[0] == 400
        # токен проходит в private и не раскрывается наружу
        assert TOKEN not in json.dumps(s.j("GET", "/api/status")[1])
    finally:
        s.stop()


def test_http_link_email_with_api_token_then_login(tmp_path, mail):
    mailer = Mailer("resend", "re_k", "onboarding@resend.dev", api_url=mail.url, timeout=3)
    s = Server(make_settings(tmp_path, {}), lambda st: build_services(st, router=Script(), mailer=mailer))
    try:
        assert s.j("POST", "/api/auth/request", {"email": ME})[1]["link"] is True      # с токеном: привязка
        code, out = s.j("POST", "/api/auth/verify", {"email": ME, "code": mail.last_code()}, token=None)
        assert code == 200 and out["linked"] is True
        assert s.j("GET", "/api/status", token=out["token"])[0] == 200
        s.j("POST", "/api/auth/logout", token=out["token"])
        # с другого устройства: токена нет, входим только по почте
        import time
        con = sqlite3.connect(next((tmp_path / "data").glob("*.db")))
        con.execute("DELETE FROM auth_codes"); con.commit(); con.close()
        assert s.j("POST", "/api/auth/request", {"email": ME}, token=None)[1]["link"] is False
        code, out2 = s.j("POST", "/api/auth/verify", {"email": ME, "code": mail.last_code()}, token=None)
        assert code == 200 and out2["linked"] is False and s.j("GET", "/api/status", token=out2["token"])[0] == 200
    finally:
        s.stop()


def test_http_without_mail_key_explains_what_to_set(tmp_path):
    s = Server(make_settings(tmp_path, {}), lambda st: build_services(st, router=Script()))
    try:
        assert s.j("GET", "/api/auth/info", token=None)[1] == {"email_login": False}
        code, out = s.j("POST", "/api/auth/request", {"email": ME}, token=None)
        assert code == 503 and "MAIL_API_KEY" in out["detail"]
        assert s.j("GET", "/api/status")[1]["auth"] == {"email_login": False}
    finally:
        s.stop()
