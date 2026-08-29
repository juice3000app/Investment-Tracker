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


def build_digest_content(
    exited_today: list[dict],
    close_to_trigger: list[dict],
    new_candidates: list[dict],
    run_at: Optional[datetime] = None,
    addon_opportunities: Optional[list[dict]] = None,
    addon_exits_recommended: Optional[list[dict]] = None,
    addon_merges: Optional[list[dict]] = None,
) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body). Kept separate from
    send_daily_digest so daily_job.py can preview/test the content
    without actually sending mail.

    The three addon_* lists are Mechanism O's own sections (recommended
    C+1 add-on buys, recommended add-on exits, and merges the job already
    performed as bookkeeping -- see daily_job.py's module docstring for
    why a merge isn't a recommendation). All three default to empty so
    existing callers that don't pass them still work."""
    run_at = run_at or datetime.now()
    date_str = run_at.strftime("%Y-%m-%d")
    addon_opportunities = addon_opportunities or []
    addon_exits_recommended = addon_exits_recommended or []
    addon_merges = addon_merges or []

    action_needed = bool(exited_today) or bool(new_candidates) or bool(addon_opportunities) or bool(addon_exits_recommended)
    subject = f"Model 2 daily digest -- {date_str}" + (" (action needed)" if action_needed else "")

    text_lines = [f"Model 2 daily digest -- {date_str}", ""]
    html_sections = []

    if exited_today:
        text_lines.append("EXIT RECOMMENDATIONS (sell these -- rule already triggered):")
        rows = []
        for e in exited_today:
            text_lines.append(
                f"  - {e['ticker']}: {e['reason']} at ${e['price']:.2f} (entered {e['entry_date']} @ ${e['entry_price']:.2f})"
            )
            rows.append(
                f"<tr><td>{e['ticker']}</td><td>{e['reason']}</td><td>${e['price']:.2f}</td>"
                f"<td>{e['entry_date']}</td><td>${e['entry_price']:.2f}</td></tr>"
            )
        html_sections.append(
            "<h2 style='color:#d03b3b'>Exit recommendations</h2>"
            "<p>Sell these -- the rule already triggered.</p>"
            "<table border='0' cellpadding='6' style='border-collapse:collapse;width:100%'>"
            "<tr style='text-align:left;color:#666'><th>Ticker</th><th>Reason</th><th>Price</th><th>Entry date</th><th>Entry price</th></tr>"
            + "".join(rows) + "</table>"
        )
        text_lines.append("")

    if close_to_trigger:
        text_lines.append("CLOSE TO A TRIGGER (no action needed yet):")
        rows = []
        for c in close_to_trigger:
            text_lines.append(
                f"  - {c['ticker']}: {c['pct_to_stop']:.1f}% above stop-loss "
                f"({'next stagnation check in ' + str(c['days_to_stagnation_check']) + ' day(s)' if c.get('days_to_stagnation_check') is not None else ''})"
            )
            rows.append(
                f"<tr><td>{c['ticker']}</td><td>{c['pct_to_stop']:.1f}%</td>"
                f"<td>{c.get('days_to_stagnation_check', '')}</td></tr>"
            )
        html_sections.append(
            "<h2>Open positions -- status</h2>"
            "<table border='0' cellpadding='6' style='border-collapse:collapse;width:100%'>"
            "<tr style='text-align:left;color:#666'><th>Ticker</th><th>Room above stop-loss</th><th>Days to next stagnation check</th></tr>"
            + "".join(rows) + "</table>"
        )
        text_lines.append("")

    if new_candidates:
        text_lines.append("UPCOMING CATALYSTS PASSING THE SCREEN:")
        rows = []
        for n in new_candidates:
            text_lines.append(f"  - {n['ticker']}: catalyst on {n['catalyst_date']} ({n['days_until']} day(s) away)")
            rows.append(f"<tr><td>{n['ticker']}</td><td>{n['catalyst_date']}</td><td>{n['days_until']}</td></tr>")
        html_sections.append(
            "<h2 style='color:#0ca30c'>Upcoming catalysts passing the screen</h2>"
            "<table border='0' cellpadding='6' style='border-collapse:collapse;width:100%'>"
            "<tr style='text-align:left;color:#666'><th>Ticker</th><th>Catalyst date</th><th>Days away</th></tr>"
            + "".join(rows) + "</table>"
        )
        text_lines.append("")

    if addon_opportunities:
        text_lines.append("MECHANISM O -- ADD-ON BUY OPPORTUNITIES (C+1 trigger fired today):")
        rows = []
        for o in addon_opportunities:
            label = "O1 dip-buy" if o["lot_type"] == "o1" else "O2 momentum-buy"
            text_lines.append(
                f"  - {o['ticker']}: {label}, moved {o['pct_change']:+.1f}% since entry, now ${o['price']:.2f} "
                f"-- suggested size {o['suggested_size_pct']:.1f}% of portfolio"
            )
            rows.append(
                f"<tr><td>{o['ticker']}</td><td>{label}</td><td>{o['pct_change']:+.1f}%</td>"
                f"<td>${o['price']:.2f}</td><td>{o['suggested_size_pct']:.1f}%</td></tr>"
            )
        html_sections.append(
            "<h2 style='color:#0ca30c'>Mechanism O -- add-on buy opportunities</h2>"
            "<p>C+1 trigger fired today. If you make this purchase, record it on the dashboard as an add-on "
            "linked to its base position.</p>"
            "<table border='0' cellpadding='6' style='border-collapse:collapse;width:100%'>"
            "<tr style='text-align:left;color:#666'><th>Ticker</th><th>Mechanism</th><th>Move since entry</th>"
            "<th>C+1 price</th><th>Suggested size</th></tr>" + "".join(rows) + "</table>"
        )
        text_lines.append("")

    if addon_exits_recommended:
        text_lines.append("MECHANISM O -- ADD-ON EXIT RECOMMENDATIONS:")
        rows = []
        for e in addon_exits_recommended:
            text_lines.append(f"  - {e['ticker']} ({e['lot_type'].upper()}): {e['reason']} at ${e['price']:.2f}")
            rows.append(f"<tr><td>{e['ticker']}</td><td>{e['lot_type'].upper()}</td><td>{e['reason']}</td><td>${e['price']:.2f}</td></tr>")
        html_sections.append(
            "<h2 style='color:#d03b3b'>Mechanism O -- add-on exit recommendations</h2>"
            "<p>Sell these add-on lots -- the rule already triggered.</p>"
            "<table border='0' cellpadding='6' style='border-collapse:collapse;width:100%'>"
            "<tr style='text-align:left;color:#666'><th>Ticker</th><th>Lot</th><th>Reason</th><th>Price</th></tr>"
            + "".join(rows) + "</table>"
        )
        text_lines.append("")

    if addon_merges:
        text_lines.append("MECHANISM O -- ADD-ON LOTS MERGED INTO BASE POSITIONS TODAY (no action needed):")
        rows = []
        for m in addon_merges:
            text_lines.append(
                f"  - {m['ticker']}: O1 add-on merged into base at ${m['price']:.2f}, combined {m['combined_shares']:.2f} shares"
            )
            rows.append(f"<tr><td>{m['ticker']}</td><td>${m['price']:.2f}</td><td>{m['combined_shares']:.2f}</td></tr>")
        html_sections.append(
            "<h2>Mechanism O -- add-on lots merged into base positions</h2>"
            "<p>No action needed -- the O1 add-on was on an uptrend at its checkpoint, so it's now tracked as "
            "part of the base position, subject to the base position's normal stagnation/stop-loss rules.</p>"
            "<table border='0' cellpadding='6' style='border-collapse:collapse;width:100%'>"
            "<tr style='text-align:left;color:#666'><th>Ticker</th><th>Merge price</th><th>Combined shares</th></tr>"
            + "".join(rows) + "</table>"
        )
        text_lines.append("")

    if not exited_today and not close_to_trigger and not new_candidates and not addon_opportunities and not addon_exits_recommended and not addon_merges:
        text_lines.append("Nothing needs attention today.")
        html_sections.append("<p>Nothing needs attention today.</p>")

    html_body = f"<html><body style='font-family:sans-serif'><h1>Model 2 daily digest -- {date_str}</h1>{''.join(html_sections)}</body></html>"
    text_body = "\n".join(text_lines)
    return subject, html_body, text_body
