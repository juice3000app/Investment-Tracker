"""
live_app/notify.py

Sends the daily digest via the person's own email account over SMTP
(decision 7.3). Needs three environment variables, set on whatever host
runs this (see the deployment README):

    SMTP_HOST           e.g. smtp.gmail.com
    SMTP_PORT           e.g. 587
    SMTP_USER           the sending mailbox address
    SMTP_APP_PASSWORD   an app password (NOT the account's real password --
                         Gmail and most providers require 2FA enabled on
                         the account before they'll issue one)
    DIGEST_TO_EMAIL     where the digest gets sent (can be the same address)
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional


def _env(name: str) -> Optional[str]:
    return os.environ.get(name)


def send_daily_digest(subject: str, html_body: str, text_body: str) -> bool:
    """Returns True if the send succeeded. Raises nothing on missing
    config -- returns False and lets the caller decide how loud to be
    about it (the daily job logs it either way)."""
    host = _env("SMTP_HOST")
    port = _env("SMTP_PORT")
    user = _env("SMTP_USER")
    app_password = _env("SMTP_APP_PASSWORD")
    to_email = _env("DIGEST_TO_EMAIL")

    missing = [
        name for name, val in [
            ("SMTP_HOST", host), ("SMTP_PORT", port), ("SMTP_USER", user),
            ("SMTP_APP_PASSWORD", app_password), ("DIGEST_TO_EMAIL", to_email),
        ] if not val
    ]
    if missing:
        raise RuntimeError(f"Missing email config: {', '.join(missing)}. Set these as environment variables.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(host, int(port)) as server:
        server.starttls()
        server.login(user, app_password)
        server.sendmail(user, [to_email], msg.as_string())
    return True


def build_action_email_content(action: str, ticker: str, detail: dict) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body) for a single 'ACTION
    RECOMMENDED' email -- one action, one ticker, sent the day it's time
    to act, replacing the old once-a-day bundled digest entirely (Mike:
    "I don't think I need a daily digest"). Subject format is fixed
    verbatim per his request: 'ACTION RECOMMENDED - BUY/SELL {TICKER}
    STOCK'.

    `action` is one of: buy_base, buy_addon, sell_base, sell_addon.
    `detail` is whichever recommendation dict daily_job.py already builds
    for that action (see run_once) -- rendered as a plain label/value
    list, skipping the internal-only position_id/base_position_id keys
    that only matter for this module's own dedup bookkeeping."""
    verb = "BUY" if action.startswith("buy") else "SELL"
    subject = f"ACTION RECOMMENDED - {verb} {ticker} STOCK"

    labels = {
        "ticker": "Ticker",
        "reason": "Reason",
        "price": "Price",
        "entry_date": "Entry date",
        "entry_price": "Entry price",
        "lot_type": "Lot",
        "catalyst_date": "Catalyst date",
        "days_until": "Days until catalyst",
        "recommended_entry_date": "Recommended entry date",
        "pct_change": "Move since entry",
        "suggested_size_pct": "Suggested size",
    }
    skip_keys = {"position_id", "base_position_id"}
    action_descriptions = {
        "buy_base": "New base position -- the recommended entry date has arrived.",
        "buy_addon": "Mechanism O add-on buy opportunity -- the C+1 trigger fired.",
        "sell_base": "Exit recommendation -- the rule already triggered.",
        "sell_addon": "Mechanism O add-on exit recommendation -- the rule already triggered.",
    }
    description = action_descriptions.get(action, "")

    def fmt_value(key, value):
        if key in ("price", "entry_price") and isinstance(value, (int, float)):
            return f"${value:.2f}"
        if key == "pct_change" and isinstance(value, (int, float)):
            return f"{value:+.1f}%"
        if key == "suggested_size_pct" and isinstance(value, (int, float)):
            return f"{value:.1f}%"
        if key == "lot_type" and isinstance(value, str):
            return value.upper()
        return str(value)

    rows_text = []
    rows_html = []
    for key, value in detail.items():
        if key in skip_keys:
            continue
        label = labels.get(key, key.replace("_", " ").title())
        formatted = fmt_value(key, value)
        rows_text.append(f"  {label}: {formatted}")
        rows_html.append(f"<tr><td style='color:#666'>{label}</td><td>{formatted}</td></tr>")

    color = "#0ca30c" if verb == "BUY" else "#d03b3b"
    text_body = "\n".join([subject, "", description, ""] + rows_text)
    html_body = (
        f"<html><body style='font-family:sans-serif'>"
        f"<h1 style='color:{color}'>{subject}</h1>"
        f"<p>{description}</p>"
        f"<table border='0' cellpadding='6' style='border-collapse:collapse'>"
        + "".join(rows_html) + "</table></body></html>"
    )
    return subject, html_body, text_body
