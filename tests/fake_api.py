"""In-memory fake of the Yandex Disk REST API for tests. Standard library only.

Speaks the real protocol as far as the sorter cares: the OAuth header, the
``{error, description, message}`` envelope, ``disk:/`` paths, pagination with
``limit``/``offset``/``_embedded.total``, 409 codes for existing targets and
missing parents, optional ``202 Accepted`` + ``/operations/<id>`` for moves,
and injectable 429/5xx responses with ``Retry-After``.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

TOKEN = "test-token"


def _norm(path: str) -> str:
    text = path.strip()
    if text.startswith("disk:"):
        text = text[5:]
    segments = [s for s in text.split("/") if s]
    return "disk:/" + "/".join(segments)


def _parent(path: str) -> str:
    rest = path[5:]
    if rest in ("", "/"):
        return ""
    head = rest.rsplit("/", 1)[0]
    return "disk:" + (head or "/")


class FakeDisk:
    def __init__(self, downloads: str = "disk:/Загрузки", token: str = TOKEN):
        self.lose_response_for: set = set()  # source paths whose move succeeds but answers 502 once
        self.echo_twice: Optional[str] = None  # a path the listing repeats (simulates a page shift)
        self.token = token
        self.downloads = downloads
        self.tree: Dict[str, Dict[str, Any]] = {"disk:/": {"type": "dir", "name": "", "path": "disk:/"}}
        self.async_moves = False
        self.inject: List[Tuple[int, Dict[str, Any], Dict[str, str]]] = []
        self.inject_routes: Optional[Tuple[str, ...]] = None  # None = inject on any route
        self.log: List[Tuple[str, str, Dict[str, str]]] = []
        self.operations: Dict[str, int] = {}
        self.lock = threading.Lock()
        self.add_dir(downloads)

    # -- fixtures -----------------------------------------------------------

    def add_dir(self, path: str) -> None:
        path = _norm(path)
        parent = _parent(path)
        if parent and parent not in self.tree:
            self.add_dir(parent)
        self.tree.setdefault(path, {"type": "dir", "name": path.rsplit("/", 1)[-1], "path": path, "created": "2026-01-01T00:00:00+00:00", "modified": "2026-01-01T00:00:00+00:00"})

    def add_file(self, path: str, size: int = 100, md5: Optional[str] = None, media_type: str = "unknown", created: str = "2026-05-01T10:00:00+00:00", modified: Optional[str] = None, **extra: Any) -> None:
        path = _norm(path)
        self.add_dir(_parent(path))
        name = path.rsplit("/", 1)[-1]
        item = {
            "type": "file",
            "name": name,
            "path": path,
            "size": size,
            "md5": md5 or f"md5-{name}-{size}",
            "sha256": f"sha-{name}",
            "media_type": media_type,
            "mime_type": "application/octet-stream",
            "created": created,
            "modified": modified or created,
            "resource_id": f"rid-{name}",
        }
        item.update(extra)
        self.tree[path] = item

    def children(self, path: str) -> List[Dict[str, Any]]:
        prefix = path.rstrip("/") + "/"
        out = [v for k, v in self.tree.items() if k.startswith(prefix) and "/" not in k[len(prefix):]]
        return sorted(out, key=lambda v: v["name"])

    def files_under(self, path: str) -> List[str]:
        prefix = _norm(path).rstrip("/") + "/"
        return sorted(k for k, v in self.tree.items() if k.startswith(prefix) and v["type"] == "file")

    def move(self, src: str, dst: str) -> None:
        src, dst = _norm(src), _norm(dst)
        moved = {k: v for k, v in self.tree.items() if k == src or k.startswith(src + "/")}
        for k, v in moved.items():
            del self.tree[k]
            new_key = dst + k[len(src):]
            v = dict(v, path=new_key, name=new_key.rsplit("/", 1)[-1])
            self.tree[new_key] = v


class _Handler(BaseHTTPRequestHandler):
    fake: FakeDisk

    def log_message(self, *_args: Any) -> None:  # silence
        pass

    def _send(self, status: int, payload: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: int, code: str, description: str) -> None:
        self._send(status, {"error": code, "description": description, "message": "Локализованное сообщение."})

    def _dispatch(self, method: str) -> None:
        fake = self.fake
        parsed = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        with fake.lock:
            fake.log.append((method, parsed.path, query))
            if fake.inject and (fake.inject_routes is None or parsed.path in fake.inject_routes):
                status, payload, headers = fake.inject.pop(0)
                self._send(status, payload, headers)
                return
            if self.headers.get("Authorization") != f"OAuth {fake.token}":
                self._error(401, "UnauthorizedError", "Unauthorized")
                return
            route = parsed.path
            if route in ("/v1/disk", "/v1/disk/") and method == "GET":
                self._send(200, {
                    "total_space": 10 * 1024**3, "used_space": 3 * 1024**3, "trash_size": 1024**2,
                    "system_folders": {"downloads": fake.downloads + "/", "applications": "disk:/Приложения", "photostream": "disk:/Фотокамера/"},
                })
                return
            if route in ("/v1/disk/operations",) or route.startswith("/v1/disk/operations/"):
                op_id = query.get("id") or route.rsplit("/", 1)[-1]
                polls = fake.operations.get(op_id)
                if polls is None:
                    self._error(404, "DiskNotFoundError", "Operation not found")
                    return
                fake.operations[op_id] = polls + 1
                self._send(200, {"status": "in-progress" if polls == 0 else "success"})
                return
            if route == "/v1/disk/resources":
                if method == "GET":
                    self._get_resource(query)
                elif method == "PUT":
                    self._mkdir(query)
                elif method == "DELETE":
                    self._delete(query)
                else:
                    self._error(405, "MethodNotAllowed", "Method not allowed")
                return
            if route == "/v1/disk/resources/move" and method == "POST":
                self._move(query)
                return
            self._error(404, "DiskNotFoundError", f"No route {method} {route}")

    def _get_resource(self, query: Dict[str, str]) -> None:
        fake = self.fake
        path = _norm(query.get("path", ""))
        item = fake.tree.get(path)
        if item is None:
            self._error(404, "DiskNotFoundError", "Resource not found.")
            return
        out = dict(item)
        if item["type"] == "dir":
            limit = int(query.get("limit", 20))
            offset = int(query.get("offset", 0))
            kids = fake.children(path)
            sort = query.get("sort", "name")
            if sort.lstrip("-") in ("name", "size", "created", "modified"):
                kids.sort(key=lambda v: str(v.get(sort.lstrip("-"), "")), reverse=sort.startswith("-"))
            page = kids[offset: offset + limit]
            if fake.echo_twice and offset == 0:
                page = page + [v for v in kids if v["path"] == fake.echo_twice]
            out["_embedded"] = {"items": page, "total": len(kids), "limit": limit, "offset": offset, "path": path, "sort": sort}
        self._send(200, out)

    def _mkdir(self, query: Dict[str, str]) -> None:
        fake = self.fake
        path = _norm(query.get("path", ""))
        if path in fake.tree:
            if fake.tree[path]["type"] == "dir":
                self._error(409, "DiskPathPointsToExistentDirectoryError", "Specified path points to existent directory.")
            else:
                self._error(409, "DiskResourceAlreadyExistsError", "Resource already exists.")
            return
        parent = _parent(path)
        if parent not in fake.tree:
            self._error(409, "DiskPathDoesntExistsError", "Specified path doesn't exists.")
            return
        fake.add_dir(path)
        self._send(201, {"href": f"http://fake/v1/disk/resources?path={urllib.parse.quote(path)}", "method": "GET", "templated": False})

    def _move(self, query: Dict[str, str]) -> None:
        fake = self.fake
        src = _norm(query.get("from", ""))
        dst = _norm(query.get("path", ""))
        overwrite = query.get("overwrite", "false") == "true"
        if src not in fake.tree:
            self._error(404, "DiskNotFoundError", "Resource not found.")
            return
        if _parent(dst) not in fake.tree:
            self._error(409, "DiskPathDoesntExistsError", "Specified path doesn't exists.")
            return
        if dst in fake.tree and not overwrite:
            self._error(409, "DiskResourceAlreadyExistsError", "Resource already exists.")
            return
        fake.move(src, dst)
        if src in fake.lose_response_for:
            fake.lose_response_for.discard(src)
            self._send(502, None, {})  # the backend committed the move, the client never learns
            return
        if fake.async_moves:
            op_id = f"op{len(fake.operations) + 1}"
            fake.operations[op_id] = 0
            self._send(202, {"href": f"http://{self.headers.get('Host')}/v1/disk/operations?id={op_id}", "method": "GET", "templated": False})
        else:
            self._send(201, {"href": f"http://fake/v1/disk/resources?path={urllib.parse.quote(dst)}", "method": "GET", "templated": False})

    def _delete(self, query: Dict[str, str]) -> None:
        fake = self.fake
        path = _norm(query.get("path", ""))
        if path not in fake.tree:
            self._error(404, "DiskNotFoundError", "Resource not found.")
            return
        for key in [k for k in fake.tree if k == path or k.startswith(path + "/")]:
            del fake.tree[key]
        self._send(204)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")


def start_server(fake: FakeDisk) -> Tuple[ThreadingHTTPServer, str]:
    handler = type("Handler", (_Handler,), {"fake": fake})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/v1/disk"
