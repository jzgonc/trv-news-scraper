"""Newsletter mails (CleverReach): read them via IMAP and save them as HTML and print PDF.

The newsletter is subscribed with the account that sends the scraper's mails. Each run
reads that mailbox read-only (nothing is marked, moved or deleted). Every mail from
NEWSLETTER_FROM whose Message-ID is not saved yet is written to

  <DATA_DIR>/newsletters/<date>_<title-slug>/
    newsletter.html   # full newsletter, images local, without tracking pixels and the
                      # personal unsubscribe / online-version links
    newsletter.pdf    # print version: only the content (no header, no footer, no
                      # "Anstehende Termine"), images inline
    newsletter.eml    # original mail (local only, used to re-render the PDF)
    metadata.json     # id (= Message-ID), title, date, images, delivery status
    images/

Delivery (Nextcloud, then office mail with the PDF) is done by scraper.py.
"""

import email
import hashlib
import html
import imaplib
import logging
import re
import shutil
import ssl
from datetime import datetime, timezone
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import pdf
from common import (download_file, format_local, load_saved, sanitize_filename, slugify,
                    unique_dir_name, write_json)

log = logging.getLogger(__name__)

STEPS = ("nextcloud", "mail_office")
PDF_NAME = "newsletter.pdf"
HTML_NAME = "newsletter.html"
EML_NAME = "newsletter.eml"
LABEL = "Newsletter"
PDF_FOOTER = "Newsletter des TRV Fidelia"
ACCENT, ACCENT_CONTRAST = "#d20a10", "#ffffff"  # TRV red (RGB 210/10/16): label + header line
LAYOUT_WIDTH_PX = 540  # CleverReach: 580 px container minus 2 x 20 px text padding

# "TRV News: Rettungswesten in der Bootshalle" -> "Rettungswesten in der Bootshalle"
TITLE_PREFIX = re.compile(r"^\s*TRV[\s-]*News(?:letter)?\s*:\s*", re.I)
# First block "Newsletter im Tübinger Ruderverein Fidelia ..." is the header: not printed
HEADER_TEXT = re.compile(r"^Newsletter\b", re.I)
# The content ends at the dates block or, if there is none, at the footer
STOP_TEXT = (re.compile(r"^Anstehende Termine", re.I),
             re.compile(r"Alle News und Termine", re.I))

# CleverReach template markers
LOOPITEM = re.compile(r"<!--#loopitem#-->(.*?)<!--#/loopitem#-->", re.S)
BLOCK = re.compile(r"<!--#(html|image)\b[^>]*-->(.*?)<!--#/\1#-->", re.S)
PERSONAL = (re.compile(r"<!--#online_version#-->.*?<!--#/online_version#-->", re.S),
            re.compile(r"<!--#unsubscribe#-->.*?(?=</p>)", re.S))

IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
ATTR = re.compile(r'([\w:-]+)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)')
SRC_ATTR = re.compile(r'(?<![\w-])src\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', re.I)
H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.S | re.I)

MAIL_OFFICE_SUBJECT = "Bitte aushängen: Newsletter „{title}“"
MAIL_OFFICE_BODY = """Hallo Heinrich,

anbei die Druckversion des neuen Newsletters „{title}“ ({date}).
Kannst du diesen bitte ausdrucken und am Infoboard unter "Aktuelles" aushängen.

Vielen Dank!

Diese Nachricht wurde automatisch erstellt.
"""


# --------------------------------------------------------------------------- parsing

def plain_text(fragment: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", fragment, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("‌", "")
    return " ".join(text.split())


def _attrs(tag: str) -> dict:
    return {k.lower(): html.unescape(v.strip("\"'")) for k, v in ATTR.findall(tag)}


def _is_tracking(attrs: dict) -> bool:
    host = urlparse(attrs.get("src", "")).netloc.lower()
    return (attrs.get("width"), attrs.get("height")) == ("1", "1") or host.startswith("stats")


def _rewrite_images(fragment: str, target) -> str:
    """Drop tracking pixels; set each img src to target(old_src), or drop the img if None."""
    def sub(match):
        tag = match.group(0)
        attrs = _attrs(tag)
        if _is_tracking(attrs):
            return ""
        new = target(attrs.get("src", ""))
        if new is None:
            return ""
        return SRC_ATTR.sub(lambda _: f'src="{html.escape(new)}"', tag, count=1)
    return IMG_TAG.sub(sub, fragment)


def message_key(msg) -> str:
    """Message-ID, or a hash of Date/From/Subject for mails without one."""
    mid = str(msg.get("Message-ID") or "").strip()
    if mid:
        return mid
    raw = "|".join(str(msg.get(h) or "") for h in ("Date", "From", "Subject"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sender_of(msg) -> str:
    return parseaddr(str(msg.get("From") or ""))[1].lower()


def extract_content(doc: str) -> str:
    """The newsletter content without header, dates block and footer (HTML fragment).

    Works on CleverReach's loop items (one per block in the editor). Dividers between
    content blocks become <hr>. Falls back to the whole body if the markers are missing.
    """
    units = LOOPITEM.findall(doc) or [m.group(0) for m in BLOCK.finditer(doc)]
    parts, divider = [], False
    for unit in units:
        blocks = [m.group(2) for m in BLOCK.finditer(unit)]
        if not blocks:
            divider = divider or ("<!--#divider" in unit and bool(parts))
            continue
        fragment = "\n".join(blocks)
        text = plain_text(fragment)
        if any(p.search(text) for p in STOP_TEXT):
            break
        if not parts and HEADER_TEXT.match(text) and len(text) < 200:
            continue
        if divider:
            parts.append("<hr>")
            divider = False
        parts.append(fragment)
    if not parts:
        log.warning("newsletter: no CleverReach content blocks found, printing the whole body")
        match = re.search(r"<body\b[^>]*>(.*)</body>", doc, re.S | re.I)
        return match.group(1) if match else doc
    return "\n".join(parts)


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def parse(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.default)
    part = msg.get_body(("html",))
    if part is None:
        raise ValueError("mail has no HTML part")
    doc = part.get_content()
    subject = " ".join(str(msg.get("Subject") or "").split())
    try:
        date = parsedate_to_datetime(str(msg.get("Date")))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        date = datetime.now(timezone.utc)

    content = extract_content(doc)
    first_h1 = H1.search(content)
    title = TITLE_PREFIX.sub("", subject).strip() or (plain_text(first_h1.group(1)) if first_h1
                                                       else "") or LABEL
    if first_h1 and _norm(plain_text(first_h1.group(1))) == _norm(title):
        content = content[:first_h1.start()] + content[first_h1.end():]  # title is in the header
    return {"id": message_key(msg), "subject": subject, "title": title, "from": sender_of(msg),
            "date": date.isoformat(), "html": doc, "content": content}


# --------------------------------------------------------------------------- saving

def download_images(session, doc: str, path: Path) -> dict:
    """Download all non-tracking images into images/. Returns {url: filename}."""
    urls = []
    for tag in IMG_TAG.findall(doc):
        attrs = _attrs(tag)
        src = attrs.get("src", "")
        if not _is_tracking(attrs) and urlparse(src).scheme in ("http", "https") and src not in urls:
            urls.append(src)
    images_dir, tmp_dir = path / "images", path / "images.new"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir()
    names, used = {}, set()
    for i, url in enumerate(urls):
        name = sanitize_filename(unquote(Path(urlparse(url).path).name) or f"image_{i}")
        if name in used:
            name = f"{Path(name).stem}_{i}{Path(name).suffix}"
        used.add(name)
        if download_file(session, url, tmp_dir / name):
            names[url] = name
    shutil.rmtree(images_dir, ignore_errors=True)
    if names:
        tmp_dir.rename(images_dir)
    else:
        shutil.rmtree(tmp_dir)
    return names


def export_html(doc: str, images: dict) -> str:
    """Full newsletter for the archive: local images, no tracking/personal links."""
    for pattern in PERSONAL:
        doc = pattern.sub("", doc)
    return _rewrite_images(doc, lambda url: f"images/{images[url]}" if url in images else url)


def render_pdf(path: Path, meta: dict, item: dict | None = None) -> int:
    if item is None:
        item = parse((path / EML_NAME).read_bytes())
    images = meta.get("images") or {}

    def local(url):
        name = images.get(url)
        return (path / "images" / name).resolve().as_uri() if name else None

    return pdf.render_flow_pdf(
        path / PDF_NAME,
        title=meta.get("title") or "",
        label=LABEL,
        meta=format_local(meta.get("published_date"), "%d.%m.%Y"),
        body_html=_rewrite_images(item["content"], local),
        image_ref_width=LAYOUT_WIDTH_PX,
        accent=ACCENT,
        accent_contrast=ACCENT_CONTRAST,
        footer=PDF_FOOTER,
    )


def save(session, item: dict, raw: bytes, path: Path, delivery: dict) -> dict:
    """Write images, HTML, eml, PDF and (last, as commit marker) metadata.json."""
    path.mkdir(parents=True, exist_ok=True)
    images = download_images(session, item["html"], path)
    (path / HTML_NAME).write_text(export_html(item["html"], images), encoding="utf-8")
    (path / EML_NAME).write_bytes(raw)
    meta = {
        "id": item["id"],
        "title": item["title"],
        "subject": item["subject"],
        "from": item["from"],
        "published_date": item["date"],
        "images": images,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "delivery": delivery,
    }
    pages = render_pdf(path, meta, item)
    log.info("    PDF: %d page(s), %d KB", pages, (path / PDF_NAME).stat().st_size // 1024)
    write_json(path / "metadata.json", meta)
    return meta


def upload_files(path: Path) -> list[str]:
    files = [PDF_NAME, HTML_NAME]
    images_dir = path / "images"
    if images_dir.exists():
        files += [f"images/{p.name}" for p in sorted(images_dir.iterdir()) if p.is_file()]
    return files


def mail_texts(meta: dict) -> tuple[str, str]:
    fields = {"title": meta.get("title") or "",
              "date": format_local(meta.get("published_date"), "%d.%m.%Y")}
    return MAIL_OFFICE_SUBJECT.format(**fields), MAIL_OFFICE_BODY.format(**fields)


# --------------------------------------------------------------------------- IMAP

def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _connect(s) -> imaplib.IMAP4:
    if not s.imap_host or not s.imap_user:
        raise ValueError("IMAP_HOST/IMAP_USER (or SMTP_HOST/SMTP_USER) must be set")
    context = ssl.create_default_context()
    where = f"IMAP {s.imap_host}:{s.imap_port} ({s.imap_security})"
    try:
        if s.imap_security == "ssl":
            imap = imaplib.IMAP4_SSL(s.imap_host, s.imap_port, ssl_context=context, timeout=30)
        else:
            imap = imaplib.IMAP4(s.imap_host, s.imap_port, timeout=30)
            imap.starttls(context)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ConnectionError(f"{where}: cannot connect: {exc}") from exc
    try:
        imap.login(s.imap_user, s.imap_password)
    except imaplib.IMAP4.error as exc:
        raise ConnectionError(f"{where}: login as {s.imap_user} failed: {exc}") from exc
    return imap


def _fetched(data):
    """(uid, bytes) pairs from an IMAP UID FETCH response."""
    for item in data or []:
        if isinstance(item, tuple):
            match = re.search(rb"UID (\d+)", item[0])
            if match:
                yield match.group(1).decode(), item[1]


def fetch_new(s, known_ids: set) -> list[bytes]:
    """Raw mails from NEWSLETTER_FROM that are not in `known_ids` (mailbox read-only)."""
    imap = _connect(s)
    try:
        typ, data = imap.select(_quote(s.imap_folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"IMAP folder {s.imap_folder!r}: {data}")
        typ, data = imap.uid("SEARCH", None, "FROM", _quote(s.newsletter_from))
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {data}")
        uids = (data[0] or b"").split()
        if not uids:
            return []
        typ, data = imap.uid("FETCH", b",".join(uids).decode(),
                             "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)])")
        if typ != "OK":
            raise RuntimeError(f"IMAP fetch failed: {data}")
        wanted = s.newsletter_from.lower()
        new = []
        for uid, header in _fetched(data):
            msg = email.message_from_bytes(header, policy=policy.default)
            if sender_of(msg) == wanted and message_key(msg) not in known_ids:
                new.append(uid)
        result = []
        for uid in new:
            typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK":
                raise RuntimeError(f"IMAP fetch of UID {uid} failed: {data}")
            result.extend(raw for _, raw in _fetched(data))
        return result
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# --------------------------------------------------------------------------- sync

def sync(session, settings, output_dir: Path, baseline: bool,
         eml_files=()) -> tuple[int, list[str]]:
    """Save newsletters not seen yet. Returns (number saved, failures).

    With `baseline`, new and not yet delivered newsletters are marked as mailed (they are
    still uploaded)."""
    failures = []
    local = load_saved(output_dir)
    log.info("Found %d saved newsletters in %s", len(local), output_dir)
    if eml_files:
        raws = [Path(p).read_bytes() for p in eml_files]
    else:
        raws = fetch_new(settings, set(local))
    log.info("%d new newsletter mail(s)", len(raws))

    used_names = {path.name for path, _ in local.values()}
    saved = 0
    for raw in raws:
        title = "?"
        try:
            item = parse(raw)
            title = item["title"]
            if item["id"] in local:
                log.info("Newsletter already saved: %s", title)
                continue
            date_part = format_local(item["date"], "%Y-%m-%d")
            name = unique_dir_name(f"{date_part}_{slugify(title)}", used_names)
            path = output_dir / name
            if path.exists():  # leftover of an interrupted earlier attempt
                shutil.rmtree(path)
            log.info("%s newsletter: %s -> %s", "Baseline" if baseline else "New", title, name)
            delivery = {"nextcloud": False, "mail_office": baseline}
            local[item["id"]] = (path, save(session, item, raw, path, delivery))
            saved += 1
        except Exception as exc:
            log.exception("Failed to save newsletter %s", title)
            failures.append(f"save newsletter {title!r}: {exc}")

    if baseline:
        for path, meta in local.values():
            delivery = meta.setdefault("delivery", {})
            if not delivery.get("mail_office"):
                delivery["mail_office"] = True
                write_json(path / "metadata.json", meta)
    return saved, failures
