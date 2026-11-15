"""Settings from environment variables, optionally loaded from a `.env` file.

Docker Compose passes `.env` via `env_file`; for local runs the file is read here so
no extra dependency (python-dotenv) is needed. Real environment variables win.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    min_age_minutes: int
    smtp_host: str
    smtp_port: int
    smtp_security: str  # starttls | ssl | none
    smtp_user: str
    smtp_password: str
    smtp_from: str
    mail_to_redaktion: str
    mail_to_office: str
    mail_to_admin: str
    nextcloud_share_url: str
    nextcloud_share_password: str
    nextcloud_articles_folder: str
    nextcloud_dav: str  # auto | new | legacy


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or Path(__file__).resolve().parent / ".env")
    security = _get("SMTP_SECURITY", "starttls").lower()
    if security not in ("starttls", "ssl", "none"):
        raise ValueError(f"SMTP_SECURITY must be starttls, ssl or none (got {security!r})")
    default_port = {"starttls": "587", "ssl": "465", "none": "25"}[security]
    return Settings(
        data_dir=Path(_get("DATA_DIR", "data")),
        min_age_minutes=int(_get("MIN_AGE_MINUTES", "60")),
        smtp_host=_get("SMTP_HOST"),
        smtp_port=int(_get("SMTP_PORT") or default_port),
        smtp_security=security,
        smtp_user=_get("SMTP_USER"),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=_get("SMTP_FROM") or _get("SMTP_USER"),
        mail_to_redaktion=_get("MAIL_TO_REDAKTION"),
        mail_to_office=_get("MAIL_TO_OFFICE"),
        mail_to_admin=_get("MAIL_TO_ADMIN"),
        nextcloud_share_url=_get("NEXTCLOUD_SHARE_URL"),
        nextcloud_share_password=os.environ.get("NEXTCLOUD_SHARE_PASSWORD", ""),
        # NEXTCLOUD_BASE_FOLDER is the old name, still read so existing .env files work
        nextcloud_articles_folder=(_get("NEXTCLOUD_ARTICLES_FOLDER")
                                   or _get("NEXTCLOUD_BASE_FOLDER", "App-News")).strip("/"),
        nextcloud_dav=_get("NEXTCLOUD_DAV", "auto").lower(),
    )
