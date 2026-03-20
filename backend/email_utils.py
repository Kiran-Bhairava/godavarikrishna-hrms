"""Simple email utility using smtplib — no extra dependencies."""
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import settings

logger = logging.getLogger("hrms.email")


def _send_email(to: str, subject: str, html_body: str) -> bool:
    """Send an HTML email. Returns True on success, False on failure (never raises)."""
    if not settings.smtp_user or not settings.smtp_password:
        logger.warning("Email not configured — skipping send to %s", to)
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{settings.smtp_from_name} <{settings.smtp_user}>"
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, to, msg.as_string())

        logger.info("Email sent to %s | subject: %s", to, subject)
        return True
    except Exception as e:
        logger.error("Email failed to %s: %s", to, e)
        return False


def send_welcome_credentials(
    to_email: str,
    full_name: str,
    temp_password: str,
) -> bool:
    """Send welcome email with login credentials."""
    first_name = full_name.split()[0]
    subject = "Your SDPL HR Portal Login Credentials"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto">
      <h2 style="color:#1a56db">Welcome to SDPL HR Portal, {first_name}!</h2>
      <p>Your account has been created. Here are your login credentials:</p>
      <div style="background:#f3f4f6;padding:16px;border-radius:8px;margin:16px 0">
        <p style="margin:4px 0"><strong>Login URL:</strong> <a href="{settings.app_url}">{settings.app_url}</a></p>
        <p style="margin:4px 0"><strong>Email:</strong> {to_email}</p>
        <p style="margin:4px 0"><strong>Temporary Password:</strong> <code style="background:#e5e7eb;padding:2px 6px;border-radius:4px">{temp_password}</code></p>
      </div>
      <p style="color:#dc2626"><strong>⚠ You will be prompted to change your password on first login.</strong></p>
      <p style="color:#6b7280;font-size:13px">If you have any issues, contact your HR team.</p>
    </div>
    """
    return _send_email(to_email, subject, html)