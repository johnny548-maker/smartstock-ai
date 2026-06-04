# -*- coding: utf-8 -*-
"""Email delivery via Gmail SMTP using an APP PASSWORD (a mail credential —
NOT an LLM API key, so within the no-API-key policy). Credentials come from
.env / environment; if absent, the send is skipped gracefully + logged."""
import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger(__name__)

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_env():
    """Load .env into os.environ if python-dotenv is available; otherwise a
    minimal manual parse. Environment values already set take precedence."""
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_PATH, override=False)
        return
    except Exception:
        pass
    if os.path.exists(_ENV_PATH):
        try:
            with open(_ENV_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception as e:
            log.warning("could not parse .env: %s", e)


def send_email(subject, body_text):
    """Send the report as a UTF-8 plain-text email. Returns True on success,
    False if not configured or on error (never raises)."""
    _load_env()
    sender = os.environ.get("EMAIL_FROM")
    password = os.environ.get("EMAIL_APP_PASSWORD")
    recipient = os.environ.get("EMAIL_TO") or sender
    host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))

    if not sender or not password:
        log.warning("SKIP email: EMAIL_FROM / EMAIL_APP_PASSWORD not set (see .env.example)")
        return False

    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("SmartStock Daily", sender))
    msg["To"] = recipient

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, [r.strip() for r in recipient.split(",")], msg.as_string())
        log.info("email sent to %s", recipient)
        return True
    except Exception as e:
        log.error("email send failed: %s", e)
        return False
