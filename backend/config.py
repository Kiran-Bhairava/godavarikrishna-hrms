"""Configuration settings for the HRMS application."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    database_url: str = "postgresql://postgres:postgres@localhost:5432/attendance_db"
    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_hours: int = 8
    office_timezone: str = "Asia/Kolkata"
    cors_origins: str = ""
    late_grace_minutes: int = 30
    db_pool_min: int = 5
    db_pool_max: int = 20

    # ── Email / SMTP ───────────────────────────────────────────
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""           # e.g. hr@yourdomain.com
    smtp_password: str = ""       # Gmail app password or SMTP password
    smtp_from_name: str = "SDPL HR"
    app_url: str = ""             # e.g. https://hrms.yourdomain.com

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

if not settings.secret_key:
    raise RuntimeError("SECRET_KEY must be set in .env")