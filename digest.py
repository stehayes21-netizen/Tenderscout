"""Weekly job emails for subscribed tradespeople.

Each subscriber picks planning-lead types (from planning.TRADE_ORDER), tender trades
(from tenderscout.TRADE_CATEGORIES) and areas. Every Monday morning they get one email
with the new planning leads and open local tenders that match.

Sending uses plain SMTP, so a free Gmail account works:
  SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD
  (a Gmail "app password", not your normal password), DIGEST_FROM (defaults to SMTP_USER),
  DIGEST_REPLY_TO (optional), DIGEST_SENDER_NAME (optional, e.g. "Merseyside Trade Leads").
"""

import html
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
MAX_PER_SECTION = 15


class DigestConfigError(RuntimeError):
    pass


def split_list(value):
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def week_start(now):
    """Monday 00:00 UK time for the week containing `now`, as a UTC datetime."""
    local = now.astimezone(LONDON)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(timezone.utc)


def is_send_window(now):
    """Weekly emails go out on Monday from 6am UK time."""
    local = now.astimezone(LONDON)
    return local.weekday() == 0 and local.hour >= 6


def due(subscriber, now):
    """True if this active subscriber hasn't had this week's email yet."""
    if not subscriber["active"]:
        return False
    last = subscriber["last_sent_at"]
    if not last:
        return True
    try:
        sent = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return sent < week_start(now)


def match_planning(subscriber, leads):
    trades = set(split_list(subscriber["planning_trades"]))
    areas = set(split_list(subscriber["areas"]))
    out = []
    for lead in leads:
        lead_trades = set(split_list(lead["trades"]))
        if trades and not (trades & lead_trades):
            continue
        if areas and lead["area"] not in areas:
            continue
        out.append(lead)
    # Approved first (work likely soon), then newest.
    approved = [l for l in out if l["app_state"] in ("Permitted", "Conditions")]
    pending = sorted([l for l in out if l not in approved], key=lambda l: str(l["start_date"] or ""), reverse=True)
    approved.sort(key=lambda l: str(l["decided_date"] or l["start_date"] or ""), reverse=True)
    return (approved + pending)[:MAX_PER_SECTION]


def match_tenders(subscriber, tenders):
    trades = set(split_list(subscriber["tender_trades"]))
    if not trades:
        return []
    return [t for t in tenders if t["trade_category"] in trades][:MAX_PER_SECTION]


def _line(label, value):
    return f"{label}: {value}" if value else ""


def compose(subscriber, planning_leads, tenders, status_label, now=None):
    """Return (subject, text, html) for one subscriber, or None if there is nothing to send."""
    now = now or datetime.now(timezone.utc)
    if not planning_leads and not tenders:
        return None
    first = (subscriber["name"] or "").split(" ")[0] or "there"
    n = len(planning_leads) + len(tenders)
    subject = f"{n} new job lead{'s' if n != 1 else ''} in Merseyside this week"
    sender = os.environ.get("DIGEST_SENDER_NAME", "").strip() or "Merseyside Trade Leads"

    text = [f"Hi {first},", "", f"Here are this week's leads for {subscriber['business'] or 'you'}.", ""]
    parts = [f"<p>Hi {html.escape(first)},</p><p>Here are this week's leads for {html.escape(subscriber['business'] or 'you')}.</p>"]

    if tenders:
        text += ["OPEN TENDERS (councils, schools, housing)", ""]
        parts.append('<h2 style="font-size:18px;margin:24px 0 8px">Open tenders</h2>')
        for t in tenders:
            deadline = str(t["deadline"] or "")[:10]
            text += [f"- {t['title']}", f"  {t['buyer'] or ''}", _line("  Closes", deadline), f"  {t['notice_url'] or ''}", ""]
            parts.append(
                '<div style="border:1px solid #d5e1d8;border-left:4px solid #168447;border-radius:8px;padding:12px;margin:10px 0">'
                f'<b>{html.escape(t["title"] or "")}</b><br>{html.escape(t["buyer"] or "")}'
                + (f'<br>Closes {html.escape(deadline)}' if deadline else "")
                + (f'<br><a href="{html.escape(t["notice_url"])}">Open the tender notice</a>' if t["notice_url"] else "")
                + "</div>"
            )

    if planning_leads:
        text += ["PLANNING LEADS (planned building and tree work)", ""]
        parts.append('<h2 style="font-size:18px;margin:24px 0 8px">Planning leads</h2>'
                     '<p style="color:#53645a;font-size:14px">Approved applications are listed first: that work is likely to start soon.</p>')
        for l in planning_leads:
            stage = status_label(l["app_state"])
            text += [f"- {l['description']}", f"  {l['address']}", f"  {stage}", f"  {l['council_url'] or l['planit_url'] or ''}", ""]
            link = l["council_url"] or l["planit_url"]
            parts.append(
                '<div style="border:1px solid #d5e1d8;border-radius:8px;padding:12px;margin:10px 0">'
                f'<b>{html.escape(l["description"] or "")}</b><br>{html.escape(l["address"] or "")}'
                f'<br><span style="color:#08783c">{html.escape(stage)}</span>'
                + (f'<br><a href="{html.escape(link)}">Council record</a>' if link else "")
                + "</div>"
            )
        note = ("Planning addresses are public records. Contact homeowners by letter or leaflet only, "
                "not by cold call or email. Businesses and developers can be approached directly.")
        text += [note, ""]
        parts.append(f'<p style="background:#fff9e8;border:1px solid #d8c28b;border-radius:8px;padding:10px;font-size:14px">{html.escape(note)}</p>')

    footer = "Reply to this email to change your trades or areas, or to stop these emails."
    text += [footer, "", sender]
    parts.append(f'<p style="color:#53645a;font-size:13px">{html.escape(footer)}<br>{html.escape(sender)}</p>')
    body_html = ('<div style="font:16px Arial,sans-serif;color:#18251e;max-width:640px;margin:0 auto;line-height:1.45">'
                 + "".join(parts) + "</div>")
    return subject, "\n".join(x for x in text if x is not None), body_html


def smtp_settings():
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not user or not password:
        raise DigestConfigError("Set SMTP_USER and SMTP_PASSWORD in Render to send emails (a Gmail address and a Gmail app password).")
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "user": user,
        "password": password,
        "from": os.environ.get("DIGEST_FROM", "").strip() or user,
        "reply_to": os.environ.get("DIGEST_REPLY_TO", "").strip(),
        "name": os.environ.get("DIGEST_SENDER_NAME", "").strip() or "Merseyside Trade Leads",
    }


def send(to_address, subject, text, body_html, settings=None, smtp_factory=smtplib.SMTP):
    settings = settings or smtp_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings["name"], settings["from"]))
    msg["To"] = to_address
    if settings.get("reply_to"):
        msg["Reply-To"] = settings["reply_to"]
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    with smtp_factory(settings["host"], settings["port"], timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings["user"], settings["password"])
        smtp.send_message(msg)
