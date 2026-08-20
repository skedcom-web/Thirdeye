"""EmailJS-backed transactional email sending.

Third Eye has no SMTP server of its own -- EmailJS (https://www.emailjs.com)
lets a backend send templated emails with a plain REST call, using a
Service ID / Template ID / Public Key (and optionally a Private Key, for
EmailJS's stricter "API requests" mode) an admin generates in their own
EmailJS account and enters on the Notifications settings page. Nothing
here decides *when* or *to whom* an email gets sent -- that's a separate,
not-yet-built decision (a citizen digest, an alert, etc.); this module is
just the send primitive plus what the settings page's "send test email"
button calls to prove the configured credentials actually work.
"""

from __future__ import annotations

import sqlite3

import httpx

_EMAILJS_SEND_URL = "https://api.emailjs.com/api/v1.0/email/send"
_SETTINGS_KEYS = ("emailjs_service_id", "emailjs_template_id", "emailjs_public_key", "emailjs_private_key")


class EmailNotConfigured(RuntimeError):
    pass


def get_config(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        f"SELECT key, value FROM system_settings WHERE key IN ({','.join('?' * len(_SETTINGS_KEYS))})",
        _SETTINGS_KEYS,
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def is_configured(conn: sqlite3.Connection) -> bool:
    cfg = get_config(conn)
    return bool(cfg.get("emailjs_service_id") and cfg.get("emailjs_template_id") and cfg.get("emailjs_public_key"))


def save_config(
    conn: sqlite3.Connection, *, service_id: str, template_id: str, public_key: str, private_key: str = "",
) -> None:
    values = {
        "emailjs_service_id": service_id.strip(),
        "emailjs_template_id": template_id.strip(),
        "emailjs_public_key": public_key.strip(),
        "emailjs_private_key": private_key.strip(),
    }
    for key, value in values.items():
        conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, value))


def send_email(conn: sqlite3.Connection, *, to_email: str, template_params: dict) -> tuple[bool, str]:
    """Sends one email via EmailJS's REST API. template_params are merged
    with to_email and passed straight through to whichever template the
    admin configured in their EmailJS account -- this module has no
    opinion on subject/body content. Returns (ok, message)."""
    if not is_configured(conn):
        raise EmailNotConfigured("EmailJS is not configured -- set Service ID, Template ID and Public Key first")
    cfg = get_config(conn)

    payload = {
        "service_id": cfg["emailjs_service_id"],
        "template_id": cfg["emailjs_template_id"],
        "user_id": cfg["emailjs_public_key"],
        "template_params": {"to_email": to_email, **template_params},
    }
    if cfg.get("emailjs_private_key"):
        payload["accessToken"] = cfg["emailjs_private_key"]

    try:
        resp = httpx.post(_EMAILJS_SEND_URL, json=payload, timeout=20.0)
    except httpx.HTTPError as exc:
        return False, str(exc)
    if resp.status_code == 200:
        return True, "sent"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
