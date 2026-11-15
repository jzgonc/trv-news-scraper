"""Upload files into a Nextcloud *public share link* (no account) via WebDAV.

Endpoints:
  new    (Nextcloud >= 29): <base>/public.php/dav/files/<token>/   auth: anonymous / share password
  legacy:                   <base>/public.php/webdav/              auth: <token> / share password
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

log = logging.getLogger(__name__)


DAV = "{DAV:}"
PROPFIND_BODY = (
    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    "</d:prop></d:propfind>"
)


class NextcloudError(RuntimeError):
    pass


class NextcloudShare:
    def __init__(self, share_url: str, password: str = "", mode: str = "auto", timeout: int = 120):
        if not share_url:
            raise NextcloudError("NEXTCLOUD_SHARE_URL is not set")
        parsed = urlparse(share_url.strip())
        path = parsed.path.rstrip("/")
        if "/s/" not in path:
            raise NextcloudError(f"Not a public share URL (expected .../s/<token>): {share_url}")
        prefix, token = path.rsplit("/s/", 1)
        prefix = prefix.removesuffix("/index.php")
        self.share_url = f"{parsed.scheme}://{parsed.netloc}{path}"
        self.base = f"{parsed.scheme}://{parsed.netloc}{prefix}"
        self.token = token
        self.password = password or ""
        self.mode = mode
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Requested-With": "XMLHttpRequest"})
        self._root = None

    # -- endpoint detection
    def _endpoint(self, mode: str) -> tuple[str, tuple[str, str] | None]:
        if mode == "new":
            root = f"{self.base}/public.php/dav/files/{self.token}"
            auth = ("anonymous", self.password) if self.password else None
        elif mode == "legacy":
            root = f"{self.base}/public.php/webdav"
            auth = (self.token, self.password)
        else:
            raise NextcloudError(f"Unknown NEXTCLOUD_DAV mode {mode!r} (auto|new|legacy)")
        return root, auth

    def _connect(self) -> str:
        if self._root:
            return self._root
        modes = ["new", "legacy"] if self.mode == "auto" else [self.mode]
        errors = []
        for mode in modes:
            root, auth = self._endpoint(mode)
            try:
                resp = self.session.request("PROPFIND", root + "/", auth=auth,
                                            headers={"Depth": "0"}, timeout=self.timeout)
            except requests.RequestException as exc:
                errors.append(f"{mode}: {exc}")
                continue
            if resp.status_code == 207:
                log.info("Nextcloud: using %s WebDAV endpoint %s", mode, root)
                self.session.auth = auth
                self._root = root
                return root
            errors.append(f"{mode}: HTTP {resp.status_code}")
        raise NextcloudError("No working WebDAV endpoint for share (" + "; ".join(errors) + ")")

    def _url(self, remote_path: str) -> str:
        return self._connect() + "/" + quote(remote_path.strip("/"))

    def _request(self, method: str, remote_path: str, ok: tuple[int, ...], **kwargs):
        url = self._url(remote_path)
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if resp.status_code not in ok:
            raise NextcloudError(f"{method} {remote_path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp

    # -- operations
    def mkdirs(self, remote_path: str) -> None:
        """Create a folder and all parents (existing folders are fine)."""
        current = ""
        for part in [p for p in remote_path.strip("/").split("/") if p]:
            current = f"{current}/{part}"
            self._request("MKCOL", current, ok=(201, 405))  # 405 = already exists

    def put_file(self, local: Path, remote_path: str) -> None:
        with open(local, "rb") as fh:
            self._request("PUT", remote_path, ok=(200, 201, 204), data=fh)

    def delete(self, remote_path: str) -> None:
        self._request("DELETE", remote_path, ok=(200, 204, 404))

    def list_files(self, remote_dir: str) -> dict[str, tuple[int, datetime | None]]:
        """Files directly inside `remote_dir`: name -> (size, last modified).
        Returns {} if the folder does not exist."""
        resp = self._request("PROPFIND", remote_dir.strip("/") + "/", ok=(207, 404),
                             headers={"Depth": "1", "Content-Type": "application/xml"},
                             data=PROPFIND_BODY)
        if resp.status_code == 404:
            return {}
        files = {}
        for item in ET.fromstring(resp.content).iter(f"{DAV}response"):
            props = item.find(f"{DAV}propstat/{DAV}prop")
            if props is None or props.find(f"{DAV}resourcetype/{DAV}collection") is not None:
                continue
            name = unquote(item.findtext(f"{DAV}href", "").rstrip("/").rsplit("/", 1)[-1])
            size = int(props.findtext(f"{DAV}getcontentlength") or -1)
            modified = props.findtext(f"{DAV}getlastmodified")
            files[name] = (size, parsedate_to_datetime(modified) if modified else None)
        return files

    def upload_folder(self, local_dir: Path, remote_dir: str, files: list[str],
                      replace_subdirs: tuple[str, ...] = ()) -> None:
        """Upload the given relative files (e.g. 'article.pdf', 'images/x.jpg').

        `replace_subdirs` are deleted remotely first, so stale files (e.g. removed images)
        do not linger after an update.
        """
        self.mkdirs(remote_dir)
        for sub in replace_subdirs:
            self.delete(f"{remote_dir}/{sub}")
        made = set()
        for rel in files:
            parent = str(Path(rel).parent)
            if parent != "." and parent not in made:  # remote_dir itself exists already
                self._request("MKCOL", f"{remote_dir}/{parent}", ok=(201, 405))
                made.add(parent)
            self.put_file(local_dir / rel, f"{remote_dir}/{rel}")

    def folder_link(self, remote_dir: str) -> str:
        """Browser link to a subfolder of the share."""
        return f"{self.share_url}?path={quote('/' + remote_dir.strip('/'))}"
