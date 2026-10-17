"""Loopback HTTP server for the labelling UI: stdlib ThreadingHTTPServer bound to 127.0.0.1 only.
Serves one HTML page, a tiny JSON API (next image, save label) and the images of the labelled
folder (never anything outside it). New code for PLATE (no seed script). Host and Origin headers
are checked so a web page elsewhere cannot drive the tool through the browser (DNS rebinding, CSRF).
"""

from __future__ import annotations

import io
import json
import os
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from plate_label import __version__
from plate_label.page import render_page
from plate_label.store import (
    IMAGE_EXTS,
    file_blake3,
    labelled_hashes,
    list_images,
    read_labels,
    record_label,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 4612
MAX_BODY = 1 << 20
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def tiff_preview_png(path: Path) -> bytes | None:
    """Browsers other than Safari cannot show TIFF: re-encode as PNG when Pillow can read it."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            im.load()
            if im.mode.startswith("I;16") or im.mode in {"I", "F"}:
                im = im.point(lambda v: v * (1 / 256)).convert("L")
            elif im.mode not in {"L", "RGB", "RGBA", "LA"}:
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:  # unsupported TIFF flavour (e.g. 16-bit RGB): serve the raw bytes instead
        return None


class LabelApp:
    """State and rules behind the HTTP handlers (no HTTP in here, so it is easy to test)."""

    def __init__(self, folder: Path, out: Path, rater: str, take_id: str | None):
        self.folder = Path(folder).resolve()
        self.out = Path(out)
        self.rater = rater
        self.take_id = take_id
        self._lock = threading.RLock()
        self._images = list_images(self.folder)
        self._hashes: dict[Path, tuple[int, int, str]] = {}
        self._previews: dict[tuple[Path, int, int], bytes | None] = {}

    @property
    def image_count(self) -> int:
        return len(self._images)

    def refresh(self) -> None:
        with self._lock:
            self._images = list_images(self.folder)

    def hash_of(self, path: Path) -> str:
        with self._lock:
            st = path.stat()
            hit = self._hashes.get(path)
            if hit and hit[:2] == (st.st_mtime_ns, st.st_size):
                return hit[2]
            digest = file_blake3(path)
            self._hashes[path] = (st.st_mtime_ns, st.st_size, digest)
            return digest

    def resolve_image(self, rel: object) -> Path:
        """Folder-relative path -> file, or HttpError 403 (outside / not an image) or 404 (missing)."""
        if not isinstance(rel, str) or not rel or "\x00" in rel:
            raise HttpError(400, "image must be a non-empty relative path")
        try:
            path = (self.folder / rel).resolve()
        except (OSError, ValueError) as e:
            raise HttpError(400, f"bad image path: {e}") from e
        if not path.is_relative_to(self.folder):
            raise HttpError(403, "outside the labelled folder")
        if path.suffix.lower() not in IMAGE_EXTS:
            raise HttpError(403, "not an image file")
        if not path.is_file():
            raise HttpError(404, "no such image")
        return path

    def next(self, after: int, unlabelled: bool = False) -> dict:
        """The image at index after+1 (clamped); with `unlabelled`, the next one without a label."""
        with self._lock:
            if after < 0:
                self.refresh()
            images = self._images
            total = len(images)
            if total == 0:
                return {"index": -1, "total": 0, "image": None, "labelled": False}
            done = labelled_hashes(self.out)
            flags = [self.hash_of(p) in done for p in images]
            remaining = flags.count(False)
            start = max(after + 1, 0)
            idx = min(start, total - 1)
            all_done = remaining == 0
            if unlabelled and not all_done:
                order = list(range(start, total)) + list(range(0, min(start, total)))
                idx = next(i for i in order if not flags[i])
            out = {
                "index": idx,
                "total": total,
                "image": images[idx].relative_to(self.folder).as_posix(),
                "labelled": flags[idx],
                "remaining": remaining,
            }
            if all_done:
                out["all_done"] = True
            return out

    def label(self, payload: object) -> dict:
        """Validate and append one label; HttpError 400 for bad input, 403/404 for bad images."""
        if not isinstance(payload, dict):
            raise HttpError(400, "body must be a JSON object")
        verdict, reasons, note = payload.get("verdict"), payload.get("reasons", []), payload.get("note", "")
        if not isinstance(verdict, str):
            raise HttpError(400, "verdict must be a string")
        if not isinstance(note, str):
            raise HttpError(400, "note must be a string")
        path = self.resolve_image(payload.get("image"))
        with self._lock:
            try:
                record_label(self.out, self.folder, path, verdict, reasons, note, self.rater, self.take_id)
            except ValueError as e:
                raise HttpError(400, str(e)) from e
            return {"ok": True, "count": len(read_labels(self.out))}

    def image_bytes(self, path: Path) -> tuple[bytes, str]:
        """File content and content type; TIFFs become PNG previews when Pillow can do it."""
        if path.suffix.lower() in {".tif", ".tiff"}:
            st = path.stat()
            key = (path, st.st_mtime_ns, st.st_size)
            with self._lock:
                if key not in self._previews:
                    if len(self._previews) >= 8:
                        self._previews.clear()
                    self._previews[key] = tiff_preview_png(path)
                png = self._previews[key]
            if png is not None:
                return png, "image/png"
        return path.read_bytes(), CONTENT_TYPES[path.suffix.lower()]


class LabelHTTPServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port; keep it POSIX-only.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    app: LabelApp


class Handler(BaseHTTPRequestHandler):
    server: LabelHTTPServer
    server_version = f"plate-label/{__version__}"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    # --- plumbing -----------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(
            status,
            json.dumps(obj).encode("utf-8"),
            "application/json; charset=utf-8",
            {"Cache-Control": "no-store"},
        )

    def _fail(self, err: HttpError) -> None:
        self._json(err.status, {"ok": False, "error": err.message})

    def _guard(self, check_origin: bool) -> None:
        """Reject requests whose Host (rebinding) or Origin (cross-site POST) is not this loopback server."""
        port = self.server.server_address[1]
        host = urlsplit("//" + self.headers.get("Host", "")).hostname
        if host not in LOOPBACK_NAMES:
            raise HttpError(403, "bad Host header")
        origin = self.headers.get("Origin")
        if check_origin and origin is not None:
            parts = urlsplit(origin)
            if parts.scheme != "http" or parts.hostname not in LOOPBACK_NAMES or parts.port != port:
                raise HttpError(403, "bad Origin header")

    # --- routes -------------------------------------------------------------------------------

    def do_GET(self) -> None:
        try:
            self._guard(check_origin=False)
            path, _, query = self.path.partition("?")
            if path in {"/", "/index.html"}:
                nonce = secrets.token_urlsafe(12)
                csp = (
                    f"default-src 'none'; img-src 'self'; connect-src 'self'; "
                    f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'"
                )
                self._send(
                    HTTPStatus.OK,
                    render_page(nonce).encode("utf-8"),
                    "text/html; charset=utf-8",
                    {"Content-Security-Policy": csp, "Cache-Control": "no-store"},
                )
            elif path == "/api/next":
                q = parse_qs(query)
                try:
                    after = int(q.get("after", ["-1"])[0])
                except ValueError as e:
                    raise HttpError(400, "after must be an integer") from e
                self._json(200, self.server.app.next(after, q.get("unlabelled", ["0"])[0] == "1"))
            elif path.startswith("/img/"):
                file = self.server.app.resolve_image(unquote(path[len("/img/") :]))
                body, ctype = self.server.app.image_bytes(file)
                self._send(HTTPStatus.OK, body, ctype, {"Cache-Control": "private, max-age=300"})
            else:
                raise HttpError(404, "not found")
        except HttpError as e:
            self._fail(e)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        try:
            self._guard(check_origin=True)
            if self.path.partition("?")[0] != "/api/label":
                raise HttpError(404, "not found")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as e:
                raise HttpError(400, "Content-Length required") from e
            if length < 0 or length > MAX_BODY:
                raise HttpError(413, "body too large")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise HttpError(400, f"body is not valid JSON: {e}") from e
            self._json(200, self.server.app.label(payload))
        except HttpError as e:
            self._fail(e)
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(
    folder: Path, out: Path, rater: str, take_id: str | None = None, port: int = DEFAULT_PORT
) -> LabelHTTPServer:
    """Bind 127.0.0.1:port (0 = any free port) and return the server; the caller runs serve_forever()."""
    httpd = LabelHTTPServer((HOST, port), Handler)
    httpd.app = LabelApp(folder, out, rater, take_id)
    return httpd


def serve(
    folder: Path,
    out: Path,
    rater: str,
    take_id: str | None,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    """Run the UI until Ctrl+C. Returns a process exit code (2 when the port is taken)."""
    try:
        httpd = make_server(folder, out, rater, take_id, port)
    except OSError as e:
        print(f"error: cannot listen on {HOST}:{port}: {e}", file=sys.stderr)
        return 2
    url = f"http://{HOST}:{httpd.server_address[1]}/"
    print(f"labelling {httpd.app.image_count} image(s) from {httpd.app.folder} -> {Path(out)}", flush=True)
    print(f"open {url}   (Ctrl+C to stop)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # no browser on this box: the URL is printed above
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
