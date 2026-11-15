#!/usr/bin/env python3
"""Scrape all news articles from the AppPack news widget embedded on trv-fidelia.de.

The site embeds its news feed via an <iframe src="https://shorturl.appack.de/..."/>.
That short URL redirects to the AppPack Angular app (application.appack.de), which in
turn talks to a GraphQL API at https://api.appack.de/graphql, authenticated with a JWT
that is baked into the redirected URL. This script replays that same flow:

  1. Fetch the site homepage and find the appack.de iframe.
  2. Follow its redirect to recover the `component` id and `jwt` query params.
  3. Query the GraphQL API for the news component's data sources.
  4. Query the GraphQL API for every news entry across those data sources.
  5. Save each article into its own directory: rich-text HTML body, metadata
     (title/author/date/link/category/content hash/delivery state), all attached images
     at original quality and a print PDF.
  6. Deliver new articles: upload to a Nextcloud public share, mail the Redaktion (link)
     and the office (PDF attached). Delivery state lives in each metadata.json, so failed
     steps are retried on the next run and successful steps are never repeated.

Run modes: normal (default), --baseline (once, marks everything as delivered),
--dry-run (no upload/mail, prints what would be sent), --test-to ADDRESS (end-to-end test).
"""

import argparse
import hashlib
import html
import json
import logging
import re
import shutil
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
from zoneinfo import ZoneInfo

import requests

import pdf
from config import load_settings
from mailer import Mailer
from nextcloud import NextcloudShare

log = logging.getLogger("scraper")

GRAPHQL_URL = "https://api.appack.de/graphql"
SITE_URL = "https://trv-fidelia.de/"
FETCH_LIMIT = 1000
LOCAL_TZ = ZoneInfo("Europe/Berlin")
DELIVERY_STEPS = ("nextcloud", "mail_redaktion", "mail_office")
BASELINE_MARKER = ".baseline_done"
LAST_FAILURE_FILE = ".last_failure"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

COMPONENT_QUERY = """
query ApplicationComponent($id: ID!) {
  applicationComponent(id: $id) {
    id
    applicationType
    ... on NewsApplicationComponent {
      datasourcesUsed
    }
  }
}
"""

NEWS_FEED_QUERY = """
query NewsFeed($datasources: [ID]!, $limit: Int) {
  newsFeed(datasources: $datasources, limit: $limit, showInactive: false) {
    id
    datasourceId
    title
    description
    body
    author
    publishedDate
    updatedDate
    link
    mediaRefs {
      id
      url
      mimeType
      altText
      name
    }
  }
}
"""


DATASOURCE_QUERY = """
query Datasource($id: ID!) {
  newsFeedDatasource(datasourceId: $id) {
    id
    title
  }
}
"""

MAIL_REDAKTION_SUBJECT = "Neuer News-Artikel: {title}"
MAIL_REDAKTION_BODY = """Hallo Redaktion,

in der TRV App wurde ein neuer News-Artikel veröffentlicht:

Titel:     {title}
Autor:     {author}
Datum:     {datetime}
Kategorie: {category}

Artikel (HTML + Druckversion) in der Nextcloud:
{link}

Diese Nachricht wurde automatisch erstellt.
"""

MAIL_OFFICE_SUBJECT = "Bitte aushängen: {title}"
MAIL_OFFICE_BODY = """Hallo,

anbei die Druckversion des neuen News-Artikels „{title}“ ({date}).
Bitte ausdrucken und am Infoboard unter "Berichte" aushängen.

Vielen Dank!

Diese Nachricht wurde automatisch erstellt.
"""


def slugify(value: str, max_len: int = 60) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[\s_-]+", "-", value)
    return value[:max_len].strip("-") or "untitled"


def sanitize_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip()
    return name or "file"


def find_appack_iframe(session: requests.Session, site_url: str) -> str:
    resp = session.get(site_url, timeout=30)
    resp.raise_for_status()
    matches = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', resp.text)
    for src in matches:
        full = urljoin(site_url, src)
        if "appack.de" in urlparse(full).netloc:
            return full
    raise RuntimeError("No appack.de iframe found on the page")


def resolve_component_and_jwt(session: requests.Session, iframe_url: str) -> tuple[str, str]:
    resp = session.get(iframe_url, timeout=30)
    resp.raise_for_status()
    final_url = resp.url
    qs = parse_qs(urlparse(final_url).query)
    component = qs.get("component", [None])[0]
    jwt = qs.get("jwt", [None])[0]
    if not component or not jwt:
        raise RuntimeError(f"Could not extract component/jwt from redirected URL: {final_url}")
    return component, jwt


def graphql(session: requests.Session, jwt: str, query: str, variables: dict) -> dict:
    resp = session.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


def fetch_datasource_ids(session: requests.Session, jwt: str, component_id: str) -> list[str]:
    data = graphql(session, jwt, COMPONENT_QUERY, {"id": component_id})
    component = data.get("applicationComponent")
    if not component:
        raise RuntimeError(f"applicationComponent not found for id {component_id}")
    ids = component.get("datasourcesUsed") or []
    if not ids:
        raise RuntimeError("Component has no news data sources")
    return ids


def fetch_news_entries(session: requests.Session, jwt: str, datasource_ids: list[str], limit: int) -> list[dict]:
    data = graphql(session, jwt, NEWS_FEED_QUERY, {"datasources": datasource_ids, "limit": limit})
    entries = data.get("newsFeed") or []
    seen = set()
    unique = []
    for entry in entries:
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        unique.append(entry)
    return unique


def fetch_categories(session: requests.Session, jwt: str, datasource_ids: list[str]) -> dict:
    """Map datasource id -> title (shown as "category" on the website)."""
    result = {}
    for ds_id in datasource_ids:
        data = graphql(session, jwt, DATASOURCE_QUERY, {"id": ds_id})
        result[ds_id] = ((data.get("newsFeedDatasource") or {}).get("title") or "").strip()
    return result


def stable_media_url(url: str) -> str:
    """Media URLs are presigned S3 URLs that change on every API call: drop the query."""
    parsed = urlparse(url or "")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def content_hash(entry: dict) -> str:
    payload = json.dumps({
        "title": entry.get("title") or "",
        "body": entry.get("body") or "",
        "images": [stable_media_url(m.get("url")) for m in entry.get("mediaRefs") or []],
    }, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def load_local_articles(output_dir: Path) -> dict:
    """Map article id -> (directory, metadata) for all completely saved articles."""
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


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def article_dir_name(entry: dict, used_names: set) -> str:
    date_part = (entry.get("publishedDate") or "")[:10] or "unknown-date"
    slug = slugify(entry.get("title") or entry.get("id"))
    base = f"{date_part}_{slug}"
    name = base
    suffix = 1
    while name in used_names:
        suffix += 1
        name = f"{base}-{suffix}"
    used_names.add(name)
    return name


def download_image(session: requests.Session, url: str, dest: Path) -> bool:
    """Download one image. Permanent errors (4xx) are skipped, others raise (retry next run)."""
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
            log.warning("    ! skipping image %s: HTTP %s", stable_media_url(url), status)
            return False
        raise


def guess_extension(media: dict) -> str:
    name = media.get("name") or ""
    if "." in name:
        return ""  # name already has an extension
    mime = media.get("mimeType") or ""
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime, "")


def write_article_html(article_path: Path, title: str, published: str, author: str, body: str) -> None:
    html_doc = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
  header {{ margin-bottom: 1.5rem; border-bottom: 1px solid #ccc; padding-bottom: 1rem; }}
  header h1 {{ margin: 0 0 0.5rem 0; }}
  header .meta {{ color: #666; font-size: 0.9rem; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <div class="meta">
    {html.escape(published)}{f" &middot; {html.escape(author)}" if author else ""}
  </div>
</header>
<article>
{body}
</article>
</body>
</html>
"""
    (article_path / "article.html").write_text(html_doc, encoding="utf-8")


def read_article_body(article_path: Path) -> str:
    text = (article_path / "article.html").read_text(encoding="utf-8")
    match = re.search(r"<article>\n?(.*?)\n?</article>", text, re.S)
    return match.group(1) if match else ""


def article_images(article_path: Path, meta: dict) -> list[Path]:
    images_dir = article_path / "images"
    if "images" in meta:
        return [images_dir / name for name in meta["images"] if (images_dir / name).exists()]
    return sorted(images_dir.glob("*")) if images_dir.exists() else []


def render_article_pdf(article_path: Path, meta: dict, body: str | None = None) -> int:
    meta_line = " · ".join(x for x in (format_local(meta.get("published_date"), "%d.%m.%Y"),
                                        meta.get("author")) if x)
    return pdf.render_pdf(
        article_path / "article.pdf",
        title=meta.get("title") or "",
        label=meta.get("category") or "",
        meta=meta_line,
        body_html=read_article_body(article_path) if body is None else body,
        images=article_images(article_path, meta),
    )


def download_images(session: requests.Session, entry: dict, article_path: Path) -> list[str]:
    """Download all media into images/ (replacing an old images/ only on success)."""
    media_refs = [m for m in entry.get("mediaRefs") or [] if m.get("url")]
    images_dir = article_path / "images"
    if not media_refs:
        shutil.rmtree(images_dir, ignore_errors=True)
        return []
    tmp_dir = article_path / "images.new"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir()
    names = []
    for i, media in enumerate(media_refs):
        url = media["url"]
        raw_name = media.get("name") or Path(urlparse(url).path).name or f"image_{i}"
        filename = sanitize_filename(raw_name) + guess_extension(media)
        dest = tmp_dir / filename
        if dest.exists():
            dest = tmp_dir / f"{dest.stem}_{i}{dest.suffix}"
        if download_image(session, url, dest):
            names.append(dest.name)
    shutil.rmtree(images_dir, ignore_errors=True)
    tmp_dir.rename(images_dir)
    return names


def save_article(session: requests.Session, entry: dict, article_path: Path, category: str,
                 delivery: dict) -> dict:
    """Write images, article.html, article.pdf and (last, as commit marker) metadata.json."""
    article_path.mkdir(parents=True, exist_ok=True)

    title = entry.get("title") or "(untitled)"
    published = entry.get("publishedDate") or ""
    author = entry.get("author") or ""
    body = entry.get("body") or ""

    metadata = {
        "id": entry["id"],
        "title": title,
        "author": author,
        "published_date": published,
        "updated_date": entry.get("updatedDate") or "",
        "link": entry.get("link") or "",
        "description": entry.get("description") or "",
        "datasource_id": entry.get("datasourceId"),
        "category": category,
        "content_hash": content_hash(entry),
        "images": download_images(session, entry, article_path),
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "delivery": delivery,
    }
    write_article_html(article_path, title, published, author, body)
    pages = render_article_pdf(article_path, metadata, body)
    size_kb = (article_path / "article.pdf").stat().st_size // 1024
    log.info("    PDF: %d page(s), %d KB", pages, size_kb)
    write_json(article_path / "metadata.json", metadata)
    return metadata


# --------------------------------------------------------------------------- delivery

def remote_dir_for(settings, article_path: Path) -> str:
    return f"{settings.nextcloud_base_folder}/{article_path.name}".strip("/")


def upload_files(article_path: Path) -> list[str]:
    files = ["article.pdf", "article.html"]
    images_dir = article_path / "images"
    if images_dir.exists():
        files += [f"images/{p.name}" for p in sorted(images_dir.iterdir()) if p.is_file()]
    return files


def mail_texts(meta: dict, link: str) -> dict:
    fields = {
        "title": meta.get("title") or "",
        "author": meta.get("author") or "-",
        "category": meta.get("category") or "-",
        "datetime": format_local(meta.get("published_date"), "%d.%m.%Y %H:%M"),
        "date": format_local(meta.get("published_date"), "%d.%m.%Y"),
        "link": link,
    }
    return {
        "mail_redaktion": (MAIL_REDAKTION_SUBJECT.format(**fields), MAIL_REDAKTION_BODY.format(**fields)),
        "mail_office": (MAIL_OFFICE_SUBJECT.format(**fields), MAIL_OFFICE_BODY.format(**fields)),
    }


class Delivery:
    """Lazily connects to Nextcloud/SMTP so dry runs need no credentials."""

    def __init__(self, settings, dry_run: bool):
        self.settings = settings
        self.dry_run = dry_run
        self._share = None
        self._mailer = None

    @property
    def share(self) -> NextcloudShare:
        if self._share is None:
            s = self.settings
            self._share = NextcloudShare(s.nextcloud_share_url, s.nextcloud_share_password,
                                         s.nextcloud_dav)
        return self._share

    @property
    def mailer(self) -> Mailer:
        if self._mailer is None:
            self._mailer = Mailer.from_settings(self.settings)
        return self._mailer

    def link(self, remote_dir: str) -> str:
        url = self.settings.nextcloud_share_url
        if self.dry_run and not url:
            return f"<NEXTCLOUD_SHARE_URL>?path=/{remote_dir}"
        return self.share.folder_link(remote_dir)

    def deliver(self, article_path: Path, meta: dict) -> None:
        """Run the pending steps in order; stop at the first failure (raises)."""
        s = self.settings
        state = meta.setdefault("delivery", {})
        remote_dir = remote_dir_for(s, article_path)
        if not (article_path / "article.pdf").exists():
            render_article_pdf(article_path, meta)
        texts = mail_texts(meta, self.link(remote_dir))
        recipients = {"mail_redaktion": s.mail_to_redaktion, "mail_office": s.mail_to_office}

        for step in DELIVERY_STEPS:
            if state.get(step):
                continue
            if step == "nextcloud":
                files = upload_files(article_path)
                if self.dry_run:
                    log.info("  [dry-run] would upload %d file(s) to %s/", len(files), remote_dir)
                    continue
                self.share.upload_folder(article_path, remote_dir, files, replace_subdirs=("images",))
                log.info("  uploaded %d file(s) to Nextcloud %s/", len(files), remote_dir)
            else:
                subject, body = texts[step]
                attachments = [article_path / "article.pdf"] if step == "mail_office" else []
                if self.dry_run:
                    log.info("  [dry-run] would send %s to %s%s\n----\nSubject: %s\n\n%s----",
                             step, recipients[step] or "<not set>",
                             " (attachment: article.pdf)" if attachments else "", subject, body)
                    continue
                self.mailer.send(recipients[step], subject, body, attachments)
                log.info("  sent %s to %s", step, recipients[step])
            state[step] = True
            write_json(article_path / "metadata.json", meta)


# --------------------------------------------------------------------------- run modes

def sync_articles(session, settings, output_dir: Path, baseline: bool) -> tuple[int, list[str]]:
    """Scrape and save new/edited articles. Returns (number saved, failures)."""
    failures = []
    local = load_local_articles(output_dir)
    log.info("Found %d existing articles in %s", len(local), output_dir)

    iframe_url = find_appack_iframe(session, SITE_URL)
    component_id, jwt = resolve_component_and_jwt(session, iframe_url)
    datasource_ids = fetch_datasource_ids(session, jwt, component_id)
    categories = fetch_categories(session, jwt, datasource_ids)
    log.info("Categories: %s", ", ".join(categories.values()))
    entries = fetch_news_entries(session, jwt, datasource_ids, FETCH_LIMIT)
    log.info("Found %d articles online", len(entries))

    used_names = {path.name for path, _ in local.values()}
    now = datetime.now(timezone.utc)
    min_age = timedelta(minutes=settings.min_age_minutes)
    all_done = {step: True for step in DELIVERY_STEPS}
    saved = 0

    for entry in sorted(entries, key=lambda e: e.get("publishedDate") or ""):
        title = entry.get("title") or entry["id"]
        category = categories.get(entry.get("datasourceId"), "")
        try:
            if entry["id"] in local:
                path, meta = local[entry["id"]]
                new_hash = content_hash(entry)
                if meta.get("content_hash") is None and not baseline:
                    # saved by an older scraper version: backfill fields, don't re-deliver
                    meta.update(category=category, content_hash=new_hash)
                    meta.setdefault("delivery", dict(all_done))
                    write_json(path / "metadata.json", meta)
                    continue
                changed = meta.get("content_hash") != new_hash or meta.get("category") != category
                if baseline:
                    if changed or not (path / "article.pdf").exists():
                        log.info("Baseline update: %s", title)
                        save_article(session, entry, path, category, dict(all_done))
                        saved += 1
                    elif meta.get("delivery") != all_done:
                        meta["delivery"] = dict(all_done)
                        write_json(path / "metadata.json", meta)
                    continue
                if not changed:
                    continue
                log.info("Edited: %s -> re-generating, will re-upload (no emails)", title)
                delivery = {step: bool((meta.get("delivery") or all_done).get(step))
                            for step in DELIVERY_STEPS}
                delivery["nextcloud"] = False
                save_article(session, entry, path, category, delivery)
                saved += 1
                continue

            published = parse_date(entry.get("publishedDate"))
            if not baseline and published and now - published < min_age:
                log.info("Waiting (published < %d min ago): %s", settings.min_age_minutes, title)
                continue
            name = article_dir_name(entry, used_names)
            path = output_dir / name
            if path.exists():  # leftover of an interrupted earlier attempt (no metadata.json)
                shutil.rmtree(path)
            log.info("%s: %s -> %s", "Baseline" if baseline else "New", title, name)
            delivery = dict(all_done) if baseline else {step: False for step in DELIVERY_STEPS}
            save_article(session, entry, path, category, delivery)
            saved += 1
        except Exception as exc:
            log.exception("Failed to save %s", title)
            failures.append(f"save {title!r}: {exc}")
    return saved, failures


def deliver_pending(settings, output_dir: Path, dry_run: bool) -> list[str]:
    failures = []
    delivery = Delivery(settings, dry_run)
    pending = [(path, meta) for path, meta in load_local_articles(output_dir).values()
               if not all((meta.get("delivery") or {}).get(s, True) for s in DELIVERY_STEPS)]
    pending.sort(key=lambda pm: pm[1].get("published_date") or "")
    log.info("%d article(s) with pending delivery", len(pending))
    for path, meta in pending:
        log.info("Delivering %s (%s)", path.name, ", ".join(
            s for s in DELIVERY_STEPS if not meta["delivery"].get(s)))
        try:
            delivery.deliver(path, meta)
        except Exception as exc:
            log.exception("Delivery failed for %s", path.name)
            failures.append(f"deliver {path.name}: {exc}")
    return failures


def run_test(settings, output_dir: Path, address: str) -> int:
    """Upload the newest local article and send both mails to `address` (state unchanged)."""
    local = load_local_articles(output_dir)
    if not local:
        log.error("No saved articles in %s - run with --dry-run first", output_dir)
        return 1
    path, meta = max(local.values(), key=lambda pm: pm[1].get("published_date") or "")
    log.info("Test with article %s", path.name)
    delivery = Delivery(settings, dry_run=False)
    ok = True
    try:
        pages = render_article_pdf(path, meta)
        log.info("PDF OK: %d page(s), %d KB", pages, (path / "article.pdf").stat().st_size // 1024)
        remote_dir = remote_dir_for(settings, path)
        delivery.share.upload_folder(path, remote_dir, upload_files(path), replace_subdirs=("images",))
        log.info("Nextcloud OK: %s", delivery.link(remote_dir))
    except Exception:
        log.exception("Upload test failed")
        ok = False
    texts = mail_texts(meta, delivery.link(remote_dir_for(settings, path))
                       if settings.nextcloud_share_url else "<NEXTCLOUD_SHARE_URL fehlt>")
    for step, attach in (("mail_redaktion", []), ("mail_office", [path / "article.pdf"])):
        subject, body = texts[step]
        try:
            delivery.mailer.send(address, "[TEST] " + subject, body, attach)
            log.info("Mail OK: %s -> %s", step, address)
        except Exception:
            log.exception("Mail test (%s) failed", step)
            ok = False
    return 0 if ok else 1


def notify_admin(settings, output_dir: Path, failures: list[str]) -> None:
    """Mail a failure summary to MAIL_TO_ADMIN - only when the set of failures changes,
    so a longer outage does not produce a mail every 30 minutes."""
    marker = output_dir / LAST_FAILURE_FILE
    if not failures:
        marker.unlink(missing_ok=True)
        return
    key = "\n".join(sorted(f.split(":", 1)[0] for f in failures))
    if marker.exists() and marker.read_text(encoding="utf-8") == key:
        log.info("Same failures as last run - admin already notified")
        return
    if settings.mail_to_admin:
        try:
            body = ("Beim letzten Lauf des TRV-News-Scrapers sind Fehler aufgetreten:\n\n"
                    + "\n".join(f"- {f}" for f in failures)
                    + "\n\nFehlgeschlagene Schritte werden beim nächsten Lauf wiederholt.\n"
                      "Details: journalctl -u trv-news-scraper\n")
            Mailer.from_settings(settings).send(
                settings.mail_to_admin, f"TRV News-Scraper: {len(failures)} Fehler", body)
            log.info("Sent failure summary to %s", settings.mail_to_admin)
        except Exception:
            log.exception("Could not send failure summary")
            return
    marker.write_text(key, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", help="Article directory (default: $DATA_DIR/articles)")
    parser.add_argument("--env-file", type=Path, help="Path to .env (default: next to scraper.py)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--baseline", action="store_true",
                      help="Save everything online and mark it delivered (first run only)")
    mode.add_argument("--dry-run", action="store_true",
                      help="Everything except upload and emails; print what would be sent")
    mode.add_argument("--test-to", metavar="ADDRESS",
                      help="Upload the newest saved article and send both mails to ADDRESS")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    for noisy in ("weasyprint", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    settings = load_settings(args.env_file)
    output_dir = Path(args.output_dir) if args.output_dir else settings.data_dir / "articles"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.test_to:
        sys.exit(run_test(settings, output_dir, args.test_to))

    if not args.baseline and not args.dry_run and not (output_dir / BASELINE_MARKER).exists():
        log.error("No baseline yet in %s. Run once with --baseline first (otherwise every "
                  "article currently online would be emailed).", output_dir)
        sys.exit(2)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    failures = []
    try:
        saved, failures = sync_articles(session, settings, output_dir, args.baseline)
        log.info("Saved %d new/updated article(s)", saved)
    except Exception as exc:
        log.exception("Scraping failed")
        failures.append(f"scrape: {exc}")

    if args.baseline:
        if not failures:
            (output_dir / BASELINE_MARKER).write_text(
                datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
            log.info("Baseline complete - from now on only new articles are delivered")
    else:
        failures += deliver_pending(settings, output_dir, args.dry_run)

    if not args.dry_run:
        notify_admin(settings, output_dir, failures)
    if failures:
        log.error("%d failure(s):\n  %s", len(failures), "\n  ".join(failures))
        sys.exit(1)
    log.info("Done")


if __name__ == "__main__":
    main()
