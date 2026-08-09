"""Cloud service: transactional email — invite emails + password reset."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from cloud.app import create_app
from cloud.db import make_sessionmaker
from cloud.emailer import LogEmailer, ResendEmailer, make_emailer
from cloud.models import ResetToken, Token, utcnow


class FakeEmailer:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    def send(self, to, subject, html):
        if self.fail:
            raise RuntimeError("resend down")
        self.sent.append((to, subject, html))


@pytest.fixture()
def engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture()
def emailer():
    return FakeEmailer()


@pytest.fixture()
def client(engine, emailer):
    with TestClient(create_app(engine=engine, emailer=emailer)) as c:
        yield c


@pytest.fixture()
def sessionmaker_(engine):
    return make_sessionmaker(engine)


def signup(client, email="ada@acme.test"):
    resp = client.post(
        "/v1/workspaces",
        json={
            "workspace_name": "Acme",
            "name": "Ada Admin",
            "email": email,
            "password": "hunter22hunter22",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# -- invite emails -------------------------------------------------------------


def test_invite_with_email_sends_link(client, emailer):
    created = signup(client)
    r = client.post(
        "/v1/invites",
        headers=auth(created["token"]),
        json={"email": "New.Hire@acme.test"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["emailed_to"] == "new.hire@acme.test"
    assert body["token"]  # copyable link still returned

    (to, subject, html), = emailer.sent
    assert to == "new.hire@acme.test"
    assert "Acme" in subject
    assert body["token"] in html
    assert "/invite-accept?token=" in html


def test_invite_without_email_sends_nothing(client, emailer):
    created = signup(client)
    r = client.post("/v1/invites", headers=auth(created["token"]), json={})
    assert r.status_code == 201
    assert "emailed_to" not in r.json()
    assert emailer.sent == []


def test_invite_survives_emailer_failure(engine):
    emailer = FakeEmailer(fail=True)
    with TestClient(create_app(engine=engine, emailer=emailer)) as client:
        created = signup(client)
        r = client.post(
            "/v1/invites",
            headers=auth(created["token"]),
            json={"email": "new@acme.test"},
        )
        assert r.status_code == 201
        assert "emailed_to" not in r.json()  # send failed, link still usable


# -- signup notification -------------------------------------------------------


def test_signup_notifies_operator_when_configured(client, emailer, monkeypatch):
    monkeypatch.setenv("CLOUD_SIGNUP_NOTIFY_EMAIL", "operator@selfhost.test")
    created = signup(client)
    assert created["token"]  # response shape unchanged

    (to, subject, html), = emailer.sent
    assert to == "operator@selfhost.test"
    assert "Acme" in subject
    assert "Ada Admin" in html
    assert "ada@acme.test" in html


def test_signup_without_notify_env_sends_nothing(client, emailer):
    signup(client)
    assert emailer.sent == []


def test_signup_survives_notify_emailer_failure(engine, monkeypatch):
    monkeypatch.setenv("CLOUD_SIGNUP_NOTIFY_EMAIL", "operator@selfhost.test")
    with TestClient(create_app(engine=engine, emailer=FakeEmailer(fail=True))) as client:
        created = signup(client)  # asserts 201 — notify failure never blocks signup
        assert created["token"]


# -- password reset ------------------------------------------------------------


def test_reset_request_known_email_sends_one_link(client, emailer, sessionmaker_):
    signup(client)
    r = client.post("/v1/auth/reset-request", json={"email": "ADA@acme.test "})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    (to, subject, html), = emailer.sent
    assert to == "ada@acme.test"
    assert "reset-password?token=" in html
    with sessionmaker_() as s:
        assert s.scalar(select(ResetToken)) is not None


def test_reset_request_unknown_email_still_200_no_send(client, emailer, sessionmaker_):
    signup(client)
    r = client.post("/v1/auth/reset-request", json={"email": "nobody@acme.test"})
    assert r.status_code == 200
    assert emailer.sent == []
    with sessionmaker_() as s:
        assert s.scalar(select(ResetToken)) is None


def _reset_token_from_email(emailer) -> str:
    html = emailer.sent[-1][2]
    return html.split("reset-password?token=")[1].split('"')[0]


def test_reset_confirm_changes_password_and_kills_sessions(client, emailer, sessionmaker_):
    created = signup(client)
    old_token = created["token"]
    client.post("/v1/auth/reset-request", json={"email": "ada@acme.test"})
    raw = _reset_token_from_email(emailer)

    r = client.post(
        "/v1/auth/reset-confirm", json={"token": raw, "password": "newpassword99"}
    )
    assert r.status_code == 200, r.text

    # Every prior session is dead.
    assert client.get("/v1/me", headers=auth(old_token)).status_code == 401
    with sessionmaker_() as s:
        assert s.scalar(select(Token)) is None

    # Old password rejected, new one signs in.
    r = client.post(
        "/v1/auth/login", json={"email": "ada@acme.test", "password": "hunter22hunter22"}
    )
    assert r.status_code == 401
    r = client.post(
        "/v1/auth/login", json={"email": "ada@acme.test", "password": "newpassword99"}
    )
    assert r.status_code == 200


def test_reset_token_single_use(client, emailer):
    signup(client)
    client.post("/v1/auth/reset-request", json={"email": "ada@acme.test"})
    raw = _reset_token_from_email(emailer)
    assert (
        client.post(
            "/v1/auth/reset-confirm", json={"token": raw, "password": "newpassword99"}
        ).status_code
        == 200
    )
    r = client.post(
        "/v1/auth/reset-confirm", json={"token": raw, "password": "anotherpass99"}
    )
    assert r.status_code == 400


def test_reset_token_expires(client, emailer, sessionmaker_):
    signup(client)
    client.post("/v1/auth/reset-request", json={"email": "ada@acme.test"})
    raw = _reset_token_from_email(emailer)
    with sessionmaker_() as s:
        rt = s.scalar(select(ResetToken))
        rt.expires_at = utcnow() - timedelta(minutes=1)
        s.commit()
    r = client.post(
        "/v1/auth/reset-confirm", json={"token": raw, "password": "newpassword99"}
    )
    assert r.status_code == 400


def test_reset_confirm_garbage_token_and_short_password(client):
    signup(client)
    r = client.post(
        "/v1/auth/reset-confirm", json={"token": "nope", "password": "newpassword99"}
    )
    assert r.status_code == 400
    r = client.post("/v1/auth/reset-confirm", json={"token": "nope", "password": "short"})
    assert r.status_code == 400


# -- emailer selection ---------------------------------------------------------


def test_make_emailer_without_env_is_log_only(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    assert isinstance(make_emailer(), LogEmailer)


def test_make_emailer_with_env_is_resend(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("EMAIL_FROM", "Vetromar <no-reply@vetromar.com>")
    assert isinstance(make_emailer(), ResendEmailer)
