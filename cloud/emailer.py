"""Transactional email via Resend's HTTP API — no SDK dep.

Email is best-effort everywhere: a send failure is logged, never raised into
the request that triggered it (an invite must not fail because Resend is
down). Without RESEND_API_KEY/EMAIL_FROM configured, the LogEmailer stands in
and just logs what would have been sent (dev/tests)."""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("cloud.email")

RESEND_URL = "https://api.resend.com/emails"


def public_url() -> str:
    """Public base URL of THIS server — used in emailed invite/reset links,
    which point at the server's own /invite-accept and /reset-password pages.
    (CLOUD_WEBSITE_URL is honored as a legacy fallback name.)"""
    url = os.environ.get("CLOUD_PUBLIC_URL") or os.environ.get(
        "CLOUD_WEBSITE_URL", "http://localhost:8787"
    )
    return url.rstrip("/")


def signup_notify_email() -> str:
    """Operator address to notify on new-workspace signups ('' = disabled)."""
    return os.environ.get("CLOUD_SIGNUP_NOTIFY_EMAIL", "").strip()


class Emailer:
    def send(self, to: str, subject: str, html: str) -> None:  # pragma: no cover
        raise NotImplementedError


class LogEmailer(Emailer):
    def send(self, to: str, subject: str, html: str) -> None:
        logger.info("email (not configured, dropped): to=%s subject=%r", to, subject)


class ResendEmailer(Emailer):
    def __init__(self, api_key: str, from_addr: str):
        self._api_key = api_key
        self._from = from_addr

    def send(self, to: str, subject: str, html: str) -> None:
        try:
            resp = httpx.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"from": self._from, "to": [to], "subject": subject, "html": html},
                timeout=15.0,
            )
            if resp.status_code >= 400:
                logger.error(
                    "email send failed (%d): to=%s subject=%r body=%s",
                    resp.status_code, to, subject, resp.text[:300],
                )
            else:
                logger.info("email sent: to=%s subject=%r", to, subject)
        except httpx.HTTPError as exc:
            logger.error("email send failed: to=%s (%s)", to, exc)


def make_emailer() -> Emailer:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("EMAIL_FROM", "").strip()
    if api_key and from_addr:
        return ResendEmailer(api_key, from_addr)
    return LogEmailer()


def send_safely(emailer: Emailer, to: str, subject: str, html: str) -> bool:
    """Send, swallowing every failure (logged). Returns False on a raised
    error so callers can annotate responses, never fail them."""
    try:
        emailer.send(to, subject, html)
        return True
    except Exception:  # noqa: BLE001 — email must never break the request
        logger.error("emailer raised for to=%s subject=%r", to, subject, exc_info=True)
        return False
