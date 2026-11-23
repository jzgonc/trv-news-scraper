"""Helpers shared by the article scraper and the newsletter processing."""

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Berlin")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def slugify(value: str, max_len: int = 60) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[\s_-]+", "-", value)
    return value[:max_len].strip("-") or "untitled"


def sanitize_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip()
    return name or "file"


def unique_dir_name(base: str, used_names: set) -> str:
    name, suffix = base, 1
    while name in used_names:
        suffix += 1
        name = f"{base}-{suffix}"
    used_names.add(name)
    return name


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_local(value: str, fmt: str) -> str:
    dt = parse_date(value)
    return dt.astimezone(LOCAL_TZ).strftime(fmt) if dt else ""


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_saved(output_dir: Path) -> dict:
    """Map id -> (directory, metadata) for all completely saved items (metadata.json is
    written last, so a directory without it is an interrupted attempt)."""
    found = {}
    if not output_dir.exists():
        return found
    for meta_path in sorted(output_dir.glob("*/metadata.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "id" in data:
            found[data["id"]] = (meta_path.parent, data)
    return found


def download_file(session: requests.Session, url: str, dest: Path) -> bool:
    """Download one file. Permanent errors (4xx) are skipped (False), others raise (retry
    on the next run)."""
    try:
        with session.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        return True
    except requests.RequestException as exc:
        if dest.exists():
            dest.unlink()
        status = getattr(exc.response, "status_code", None)
        if status and 400 <= status < 500:
            log.warning("    ! skipping image %s: HTTP %s", url.split("?", 1)[0], status)
            return False
        raise
