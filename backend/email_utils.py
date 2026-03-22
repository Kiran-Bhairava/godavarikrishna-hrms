"""Simple email utility using Resend SDK."""
import resend
import logging
from config import settings

logger = logging.getLogger("hrms.email")


def _send_email(to: str, subject: str, html_body: str) -> bool:
    """Send an HTML email via Resend. Returns True on success, False on failure (never raises)."""
    if not settings.resend_api_key or not settings.from_email:
        logger.warning("Email not configured — skipping send to %s", to)
        return False
    try:
        resend.api_key = settings.resend_api_key
        resend.Emails.send({
            "from": f"{settings.from_name} <{settings.from_email}>",
            "to": [to],
            "subject": subject,
            "html": html_body,
        })
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