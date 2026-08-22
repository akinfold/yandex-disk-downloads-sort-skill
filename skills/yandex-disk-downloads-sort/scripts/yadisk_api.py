#!/usr/bin/env python3
"""Minimal Yandex Disk REST API client. Standard library only.

Talks to ``https://cloud-api.yandex.net/v1/disk`` with an OAuth token. Covers
exactly what a Downloads-sorting workflow needs: disk info, folder listing with
pagination, mkdir, move, trash, and the deferred-operation protocol.

Protocol details handled here so callers do not have to:

* ``Authorization: OAuth <token>`` header on every call.
* Pagination of ``GET /resources`` via ``limit``/``offset``/``_embedded.total``.
* ``202 Accepted`` on bulk operations: the returned ``operations/<id>`` link is
  polled until ``success``/``failed`` so callers always see a finished move.
* Retries on 429/5xx honouring ``Retry-After``.
* Error envelope ``{"error", "description", "message"}``: ``description`` is
  English, ``message`` is localised, so ``description`` is preferred.

Usable as a module (``from yadisk_api import YandexDisk``) or as a tiny CLI:

    python yadisk_api.py info
    python yadisk_api.py ls "disk:/Загрузки" --all
    python yadisk_api.py mkdir "disk:/Загрузки/Images"
    python yadisk_api.py move "disk:/Загрузки/a.png" "disk:/Загрузки/Images/a.png"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

API_BASE = "https://cloud-api.yandex.net/v1/disk"
USER_AGENT = "yandex-disk-downloads-sort/1.0 (+https://github.com/akinfold/yandex-disk-downloads-sort-skill)"
TOKEN_ENV_VARS = ("YANDEX_DISK_TOKEN", "YANDEX_DISK_OAUTH_TOKEN")
TOKEN_FILE_ENV = "YANDEX_DISK_TOKEN_FILE"
BASE_URL_ENV = "YANDEX_DISK_BASE_URL"

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 4
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
OPERATION_POLL_INTERVAL = 0.5
OPERATION_TIMEOUT = 120.0
PAGE_SIZE = 200

# Yandex error codes that matter for the sorting workflow.
ERR_ALREADY_EXISTS = "DiskResourceAlreadyExistsError"
ERR_EXISTENT_DIR = "DiskPathPointsToExistentDirectoryError"
ERR_PARENT_MISSING = "DiskPathDoesntExistsError"
ERR_NOT_FOUND = "DiskNotFoundError"
ERR_UNAUTHORIZED = "UnauthorizedError"

DOWNLOADS_FALLBACKS = ("disk:/Загрузки", "disk:/Downloads")


class YandexDiskError(Exception):
    """An API call failed. Carries the HTTP status and Yandex's error code."""

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [self.message]
        if self.status:
            parts.append(f"HTTP {self.status}")
        if self.code:
            parts.append(self.code)
        return " | ".join(parts)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error": self.message}
        if self.code:
            out["code"] = self.code
        if self.status:
            out["status"] = self.status
        return out


class TokenMissing(YandexDiskError):
    """No OAuth token could be found."""


_TOKEN_RE = re.compile(r"[\x21-\x7e]+")  # one line of printable ASCII, no spaces


def _read_token_file(path: str) -> str:
    """Read a token file written by any tool: tolerates UTF-8/UTF-16 BOMs and CRLF."""
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _clean_token(value: str, source: str) -> str:
    value = value.strip().strip("\ufeff\u200b")
    if not value:
        return ""
    if not _TOKEN_RE.fullmatch(value):
        raise TokenMissing(
            f"The OAuth token from {source} contains whitespace, line breaks or non-ASCII characters; "
            "it must be a single line like y0_AgAAAAA... (value not shown)."
        )
    return value


def load_token(token_file: Optional[str] = None) -> str:
    """Find the OAuth token: explicit --token-file, then env vars, then the file named by env.

    The token value is never logged or printed by this module.
    """
    if token_file:
        path = os.path.expanduser(token_file)
        if not os.path.isfile(path):
            raise TokenMissing(f"Token file not found: {token_file}")
        value = _clean_token(_read_token_file(path), token_file)
        if value:
            return value
        raise TokenMissing(f"Token file is empty: {token_file}")
    for name in TOKEN_ENV_VARS:
        value = _clean_token(os.environ.get(name, ""), f"${name}")
        if value:
            return value
    named = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if named:
        # The variable is set on purpose, so a missing or empty file is a mistake worth
        # naming rather than a fall-through to "no token anywhere".
        path = os.path.expanduser(named)
        if not os.path.isfile(path):
            raise TokenMissing(f"${TOKEN_FILE_ENV} points at {named}, which does not exist.")
        value = _clean_token(_read_token_file(path), f"${TOKEN_FILE_ENV}")
        if not value:
            raise TokenMissing(
                f"The token file {named} is empty. Paste your OAuth token into it "
                "(see references/oauth-token.md) and try again."
            )
        return value
    raise TokenMissing(
        "No Yandex Disk OAuth token found. Set YANDEX_DISK_TOKEN (or YANDEX_DISK_OAUTH_TOKEN), "
        "or pass --token-file. See references/oauth-token.md for how to get one."
    )


def encode_params(params: Dict[str, Any]) -> str:
    """Percent-encode query params strictly (space -> %20, '+' -> %2B, ':' -> %3A)."""
    clean = {k: v for k, v in params.items() if v is not None}
    return urllib.parse.urlencode(clean, quote_via=urllib.parse.quote, safe="")


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _error_from(status: int, payload: Any) -> YandexDiskError:
    code = None
    text = f"HTTP {status}"
    if isinstance(payload, dict):
        code = payload.get("error")
        text = payload.get("description") or payload.get("message") or text
    return YandexDiskError(str(text), status=status, code=str(code) if code else None)


def build_ssl_context() -> ssl.SSLContext:
    """System trust store plus certifi's bundle when available.

    python.org builds of Python on macOS ship without root certificates until
    "Install Certificates.command" is run; falling back to certifi (installed by
    most tooling) avoids a confusing CERTIFICATE_VERIFY_FAILED for those users.
    """
    context = ssl.create_default_context()
    try:
        import certifi  # type: ignore

        context.load_verify_locations(cafile=certifi.where())
    except Exception:  # certifi missing or unreadable: keep system defaults
        pass
    return context


class YandexDisk:
    """Thin synchronous client for the endpoints the sorter needs."""

    def __init__(
        self,
        token: str,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep=time.sleep,
    ):
        self._token = token
        self.base_url = (base_url or os.environ.get(BASE_URL_ENV) or API_BASE).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._sleep = sleep
        self.requests_made = 0
        self.last_retried = False
        self._ssl = build_ssl_context() if self.base_url.startswith("https://") else None

    # -- plumbing ---------------------------------------------------------

    def _raw(self, method: str, url: str, body: Optional[bytes] = None) -> Tuple[int, Any, Dict[str, str]]:
        headers = {
            "Authorization": f"OAuth {self._token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        self.requests_made += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl) as response:
                return response.status, _decode(response.read()), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read()), dict(exc.headers)

    def _send(self, method: str, url: str, body: Optional[bytes] = None) -> Tuple[int, Any]:
        """One logical request with retries on transient failures.

        ``last_retried`` tells callers whether the answer came from a retry. That matters
        for non-idempotent calls such as move: if the first attempt reached the server but
        its response was lost, the retry sees a 404 and the caller must reconcile.
        """
        last_error: Optional[Exception] = None
        self.last_retried = False
        for attempt in range(self.max_retries + 1):
            self.last_retried = attempt > 0
            try:
                status, payload, headers = self._raw(method, url, body)
            except (urllib.error.URLError, OSError) as exc:  # DNS, TLS, timeouts
                last_error = exc
                if attempt >= self.max_retries or _is_permanent(exc):
                    break
                self._sleep(min(2.0**attempt, 8.0))
                continue
            if status in RETRY_STATUSES and attempt < self.max_retries:
                self._sleep(_retry_after(headers, attempt))
                continue
            return status, payload
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(last_error):
            hint = (" Python cannot verify TLS certificates. Fix: run 'Install Certificates.command' from "
                    "your Python installation (macOS), or 'pip install certifi', or "
                    "'export SSL_CERT_FILE=/etc/ssl/cert.pem'.")
        raise YandexDiskError(f"Could not reach Yandex Disk: {last_error}{hint}")

    def _api(self, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, body: Any = None) -> Tuple[int, Any]:
        url = self.base_url + endpoint
        if params:
            url += "?" + encode_params(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        status, payload = self._send(method, url, data)
        if status >= 400:
            raise _error_from(status, payload)
        return status, payload

    def move_deferred(self, source: str, destination: str, *, overwrite: bool = False) -> Dict[str, Any]:
        """Move, reporting the outcome instead of raising when the server gives up.

        A folder move answers ``202`` and finishes in the background, and that background
        half can fail after moving part of the tree. Returns ``{"status": ...}`` where the
        status is ``success``, ``failed`` or ``timeout``; only transport and 4xx problems
        still raise, because those happen before anything can have moved.
        """
        params = {"from": source, "path": destination, "overwrite": "true" if overwrite else "false"}
        status, payload = self._api("POST", "/resources/move", params)
        if status != 202:
            return {"status": "success", "deferred": False}
        href = payload.get("href", "") if isinstance(payload, dict) else ""
        result = self.poll_operation(href)
        result["deferred"] = True
        return result

    def poll_operation(self, href: str, *, timeout: float = OPERATION_TIMEOUT) -> Dict[str, Any]:
        """Poll a deferred operation and report how it ended, without raising."""
        if not href:
            return {"status": "success"}
        deadline = time.monotonic() + timeout
        while True:
            status, payload = self._send("GET", href)
            if status >= 400:
                return {"status": "unknown", "error": _error_from(status, payload).message}
            state = str(payload.get("status", "")) if isinstance(payload, dict) else ""
            if state in ("success", "failed"):
                return {"status": state}
            if time.monotonic() >= deadline:
                return {"status": "timeout"}
            self._sleep(OPERATION_POLL_INTERVAL)

    def _run(self, method: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Call an endpoint that may answer 202 and wait for the deferred half."""
        status, payload = self._api(method, endpoint, params)
        if status != 202:
            return {"status": "success", "href": (payload or {}).get("href") if isinstance(payload, dict) else None}
        href = payload.get("href", "") if isinstance(payload, dict) else ""
        return self.wait_operation(href)

    def wait_operation(self, href: str, *, timeout: float = OPERATION_TIMEOUT) -> Dict[str, Any]:
        if not href:
            return {"status": "success"}
        deadline = time.monotonic() + timeout
        while True:
            status, payload = self._send("GET", href)
            if status >= 400:
                raise _error_from(status, payload)
            state = str(payload.get("status", "")) if isinstance(payload, dict) else ""
            if state == "success":
                return {"status": "success"}
            if state == "failed":
                raise YandexDiskError("Yandex Disk reported the operation as failed.", code="OperationFailed")
            if time.monotonic() >= deadline:
                raise YandexDiskError(
                    "Yandex Disk is still processing the operation; it may finish on its own.",
                    code="OperationTimeout",
                )
            self._sleep(OPERATION_POLL_INTERVAL)

    # -- reads ------------------------------------------------------------

    def disk_info(self) -> Dict[str, Any]:
        _, payload = self._api("GET", "")  # GET /v1/disk, no trailing slash
        return payload if isinstance(payload, dict) else {}

    def downloads_path(self) -> str:
        """Discover the real Downloads folder regardless of the account language.

        ``GET /disk`` exposes ``system_folders.downloads`` (e.g. ``disk:/Загрузки``).
        When absent, the usual names are probed.
        """
        info = self.disk_info()
        folders = info.get("system_folders") or {}
        candidates = []
        if folders.get("downloads"):
            candidates.append(normalize_path(folders["downloads"]))
        candidates.extend(p for p in DOWNLOADS_FALLBACKS if p not in candidates)
        for path in candidates:
            # The Disk creates system folders lazily, so the advertised path may not exist yet.
            if self.exists(path):
                return path
        raise YandexDiskError(
            f"No Downloads folder found (looked for {', '.join(candidates)}). The Disk creates it on the "
            "first download; if yours is named differently, pass --path with the exact folder path.",
            status=404,
            code="DownloadsNotFound",
        )

    def get(
        self,
        path: str,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        sort: Optional[str] = None,
        fields: Optional[str] = None,
    ) -> Dict[str, Any]:
        params = {"path": path, "limit": limit, "offset": offset, "sort": sort, "fields": fields}
        _, payload = self._api("GET", "/resources", params)
        return payload if isinstance(payload, dict) else {}

    def resource_type(self, path: str) -> Optional[str]:
        """``"dir"``, ``"file"`` or ``None`` when nothing is there."""
        try:
            return str(self.get(path, limit=0, fields="path,type").get("type") or "")
        except YandexDiskError as exc:
            if exc.status == 404:
                return None
            raise

    def exists(self, path: str) -> bool:
        return self.resource_type(path) is not None

    def is_dir(self, path: str) -> bool:
        return self.resource_type(path) == "dir"

    def iter_children(
        self,
        path: str,
        *,
        fields: Optional[str] = None,
        page_size: int = PAGE_SIZE,
        sort: str = "name",
    ) -> Iterator[Dict[str, Any]]:
        """Yield every direct child of a folder, following pagination."""
        offset = 0
        while True:
            payload = self.get(path, limit=page_size, offset=offset, sort=sort, fields=fields)
            embedded = payload.get("_embedded") or {}
            items = embedded.get("items") or []
            for item in items:
                yield item
            total = embedded.get("total")
            offset += len(items)
            if not items or (isinstance(total, int) and offset >= total):
                return

    # -- writes -----------------------------------------------------------

    def mkdir(self, path: str, *, parents: bool = True) -> str:
        """Create a folder; returns ``"created"`` or ``"exists"``. Creates parents if asked.

        A 409 only counts as "exists" when a *directory* occupies the path; a file with
        that name is an error the caller must handle (moving "into" a file is impossible).
        """
        try:
            self._api("PUT", "/resources", {"path": path})
            return "created"
        except YandexDiskError as exc:
            if exc.code == ERR_EXISTENT_DIR or (exc.status == 409 and exc.code != ERR_PARENT_MISSING and self.is_dir(path)):
                return "created" if self.last_retried and exc.code == ERR_EXISTENT_DIR else "exists"
            if exc.code == ERR_ALREADY_EXISTS:
                raise YandexDiskError(f"A file already occupies the folder name {path}.", status=409, code=exc.code)
            if parents and (exc.code == ERR_PARENT_MISSING or (exc.status == 409 and not self.exists(parent_of(path)))):
                parent = parent_of(path)
                if parent and parent != path:
                    self.mkdir(parent, parents=True)
                    self._api("PUT", "/resources", {"path": path})
                    return "created"
            raise

    def move(self, source: str, destination: str, *, overwrite: bool = False) -> Dict[str, Any]:
        params = {"from": source, "path": destination, "overwrite": "true" if overwrite else "false"}
        return self._run("POST", "/resources/move", params)

    def delete(self, path: str, *, permanently: bool = False) -> Dict[str, Any]:
        params = {"path": path, "permanently": "true" if permanently else "false"}
        return self._run("DELETE", "/resources", params)


def _is_permanent(exc: Exception) -> bool:
    """Errors that a retry cannot fix: TLS verification, refused connections, bad URLs."""
    text = str(exc)
    return any(marker in text for marker in ("CERTIFICATE_VERIFY_FAILED", "Connection refused", "unknown url type", "No address associated", "nodename nor servname"))


def _retry_after(headers: Dict[str, str], attempt: int) -> float:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    try:
        return min(max(float(raw), 0.0), 30.0)
    except ValueError:
        return min(2.0**attempt, 8.0)


# -- path helpers -----------------------------------------------------------


def normalize_path(path: str) -> str:
    """``/Загрузки/`` or ``Загрузки`` or ``disk:/Загрузки/`` -> ``disk:/Загрузки``."""
    text = (path or "").strip()
    scheme = ""
    for candidate in ("disk:", "trash:", "app:"):
        if text.lower().startswith(candidate):
            scheme, text = candidate, text[len(candidate):]
            break
    scheme = scheme or "disk:"
    segments = [s for s in text.split("/") if s and s != "."]
    return scheme + "/" + "/".join(segments)


def parent_of(path: str) -> str:
    norm = normalize_path(path)
    scheme, rest = norm.split(":", 1)
    if rest in ("", "/"):
        return ""
    head = rest.rsplit("/", 1)[0]
    return f"{scheme}:{head or '/'}"


def join_path(folder: str, name: str) -> str:
    return normalize_path(folder).rstrip("/") + "/" + name


def split_name(name: str) -> Tuple[str, str]:
    """``report.final.pdf`` -> (``report.final``, ``.pdf``); ``archive.tar.gz`` -> (``archive``, ``.tar.gz``)."""
    lower = name.lower()
    for multi in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".fb2.zip"):
        if lower.endswith(multi) and len(name) > len(multi):
            return name[: -len(multi)], name[-len(multi):]
    if "." in name[1:]:
        stem, ext = name.rsplit(".", 1)
        return stem, "." + ext
    return name, ""


def with_suffix(name: str, n: int) -> str:
    stem, ext = split_name(name)
    return f"{stem} ({n}){ext}"


# -- CLI --------------------------------------------------------------------


LS_FIELDS = ",".join(f"_embedded.items.{f}" for f in ("name", "path", "type", "size", "media_type", "md5", "created", "modified")) + ",_embedded.total,_embedded.limit,_embedded.offset,name,path,type,size,md5,media_type,created,modified"


def _cli() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Ad-hoc Yandex Disk REST calls (JSON output).")
    parser.add_argument("--token-file", help="file containing the OAuth token")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="disk quota and system folders")
    sub.add_parser("downloads-path", help="print the detected Downloads folder path")
    p_ls = sub.add_parser("ls", help="list a folder (or show a file)")
    p_ls.add_argument("path")
    p_ls.add_argument("--all", action="store_true", help="follow pagination and print every item")
    p_ls.add_argument("--fields", default=LS_FIELDS, help="comma-separated fields (default: a compact set; pass '' for everything)")
    p_mk = sub.add_parser("mkdir", help="create a folder (parents included)")
    p_mk.add_argument("path")
    p_mv = sub.add_parser("move", help="move or rename (never overwrites)")
    p_mv.add_argument("source")
    p_mv.add_argument("destination")
    p_ex = sub.add_parser("exists", help="exit 0 if the path exists, 1 if it does not, 2 on an API error")
    p_ex.add_argument("path")
    args = parser.parse_args()

    try:
        disk = YandexDisk(load_token(args.token_file))
        if args.command == "info":
            out: Any = disk.disk_info()
        elif args.command == "downloads-path":
            out = {"downloads": disk.downloads_path()}
        elif args.command == "ls":
            path = normalize_path(args.path)
            fields = args.fields or None
            if args.all:
                out = list(disk.iter_children(path, fields=fields))
            else:
                out = disk.get(path, limit=PAGE_SIZE, fields=fields)
        elif args.command == "mkdir":
            out = {"path": normalize_path(args.path), "result": disk.mkdir(normalize_path(args.path))}
        elif args.command == "move":
            out = disk.move(normalize_path(args.source), normalize_path(args.destination))
        elif args.command == "exists":
            found = disk.exists(normalize_path(args.path))
            print(json.dumps({"path": normalize_path(args.path), "exists": found}))
            return 0 if found else 1
        else:  # pragma: no cover
            parser.error("unknown command")
            return 2
    except YandexDiskError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2 if args.command == "exists" else 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
