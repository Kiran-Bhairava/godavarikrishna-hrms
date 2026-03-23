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
    subject = "Welcome to SDPL HR Portal — Your Login Credentials"
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#f0f4f8;font-family:Arial,Helvetica,sans-serif">

  <!-- Wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f4f8;padding:40px 16px">
    <tr><td align="center">

      <!-- Card -->
      <table width="520" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#1a56db 0%,#1e429f 100%);padding:36px 40px;text-align:center">
            <p style="margin:0 0 4px 0;font-size:13px;color:#93c5fd;letter-spacing:1.5px;text-transform:uppercase;font-weight:600">SDPL</p>
            <h1 style="margin:0;font-size:26px;color:#ffffff;font-weight:700;letter-spacing:-0.3px">HR Portal</h1>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:36px 40px">

            <h2 style="margin:0 0 8px 0;font-size:22px;color:#111827;font-weight:700">Welcome, {first_name}! 👋</h2>
            <p style="margin:0 0 24px 0;font-size:15px;color:#6b7280;line-height:1.6">
              Your SDPL HR Portal account is ready. Use the credentials below to sign in for the first time.
            </p>

            <!-- Credentials Box -->
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;margin-bottom:28px">
              <tr>
                <td style="padding:20px 24px">
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding:6px 0;border-bottom:1px solid #e2e8f0">
                        <span style="font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.8px;font-weight:600">Email</span><br>
                        <span style="font-size:15px;color:#111827;font-weight:500">{to_email}</span>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding:12px 0 6px 0">
                        <span style="font-size:12px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.8px;font-weight:600">Temporary Password</span><br>
                        <code style="font-size:18px;color:#1a56db;font-weight:700;letter-spacing:2px;font-family:monospace">{temp_password}</code>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>

            <!-- CTA Button -->
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px">
              <tr>
                <td align="center">
                  <a href="{settings.app_url}"
                     style="display:inline-block;background:#1a56db;color:#ffffff;font-size:15px;font-weight:600;
                            text-decoration:none;padding:14px 40px;border-radius:8px;letter-spacing:0.3px">
                    Sign In to HR Portal →
                  </a>
                </td>
              </tr>
            </table>

            <!-- Warning -->
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px">
              <tr>
                <td style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:14px 18px">
                  <p style="margin:0;font-size:13px;color:#92400e;line-height:1.5">
                    <strong>⚠ First login action required:</strong> You will be prompted to set a new password immediately after signing in.
                  </p>
                </td>
              </tr>
            </table>

          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:20px 40px;text-align:center">
            <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.6">
              This is an automated message from SDPL HR. If you didn't expect this email, please contact your HR team.<br>
              © 2026 SDPL. All rights reserved.
            </p>
          </td>
        </tr>

      </table>
      <!-- End Card -->

    </td></tr>
  </table>

</body>
</html>
"""
    return _send_email(to_email, subject, html)