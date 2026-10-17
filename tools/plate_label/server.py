"""Loopback HTTP server for the labelling UI: stdlib ThreadingHTTPServer bound to 127.0.0.1 only.
Serves one HTML page, a tiny JSON API (next pair, save label, undo) and the plate / candidate images
of the loaded pairs, streamed from where they are (never copied). New code for PLATE. Images are
served by pair index and role only, and only from the pair roots (the folders that hold the listed
files), so no request can name an arbitrary path. Host and Origin headers are checked so a web page
elsewhere cannot drive the tool through the browser (DNS rebinding, CSRF).
"""

from __future__ import annotations

import io
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from plate_label import __version__
from plate_label.page import render_page
from plate_label.pairs import IMAGE_EXTS, Pair
from plate_label.store import LabelRecord, append_label, labelled_keys, read_labels, remove_line

HOST = "127.0.0.1"
DEFAULT_PORT = 4612
MAX_BODY = 1 << 20
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
ROLES = ("plate", "candidate")
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


def _pillow_preview(path: Path) -> bytes | None:
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
    except Exception:  # a TIFF flavour Pillow cannot read (16-bit RGB, the usual VFB master)
        return None


def _cv2_preview(path: Path) -> bytes | None:
    """16-bit RGB TIFF: OpenCV reads it; keep the top byte of each sample for an 8-bit display copy."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    try:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        if img.dtype == np.uint16:
            img = (img >> 8).astype(np.uint8)
        elif img.dtype != np.uint8:
            return None
        ok, buf = cv2.imencode(".png", img)
        return buf.tobytes() if ok else None
    except Exception:
        return None


def tiff_preview_png(path: Path) -> bytes | None:
    """Browsers other than Safari cannot show TIFF: re-encode as an 8-bit PNG when Pillow or OpenCV can
    read it, else None (the raw bytes are served and the browser does what it can)."""
    return _pillow_preview(path) or _cv2_preview(path)


class LabelApp:
    """State and rules behind the HTTP handlers (no HTTP in here, so it is easy to test)."""

    def __init__(self, pairs: list[Pair], out: Path, rater: str, clock: Callable[[], float] = time.monotonic):
        if not pairs:
            raise ValueError("no pairs to label")
        self.pairs = list(pairs)
        self.out = Path(out)
        self.rater = rater
        self._clock = clock
        self._lock = threading.RLock()
        # Allowlist: the folders holding the listed files. A file that resolves outside them (a link
        # swapped in after start-up) is refused.
        self.roots = sorted(
            {f.resolve().parent for p in self.pairs for f in (p.plate, p.candidate) if f is not None}
        )
        self._shown: dict[int, float] = {}
        self._session: list[tuple[str, int]] = []  # (line written, pair index): what `u` may remove
        self._previews: dict[tuple[Path, int, int], bytes | None] = {}

    @property
    def pair_count(self) -> int:
        return len(self.pairs)

    # --- images ---------------------------------------------------------------------------

    def image_path(self, index: object, role: object) -> Path:
        """File behind /img/<index>/<role>, or HttpError 404 (no such pair / role / file) or 403."""
        try:
            i = int(index)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            raise HttpError(404, "no such pair") from None
        if not 0 <= i < len(self.pairs) or role not in ROLES:
            raise HttpError(404, "no such image")
        path = getattr(self.pairs[i], role)
        if path is None:
            raise HttpError(404, "this pair has no plate")
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            raise HttpError(404, "no such image") from None
        if resolved.suffix.lower() not in IMAGE_EXTS:
            raise HttpError(403, "not an image file")
        if not any(resolved.is_relative_to(root) for root in self.roots):
            raise HttpError(403, "outside the pair roots")
        if not resolved.is_file():
            raise HttpError(404, "no such image")
        return resolved

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

    @staticmethod
    def _version(path: Path | None) -> str:
        """Cache-busting token: changes when the file on disk does."""
        try:
            st = path.stat() if path else None
        except OSError:
            st = None
        return f"{st.st_mtime_ns:x}-{st.st_size:x}" if st else "0"

    # --- navigation -----------------------------------------------------------------------

    def _flags(self) -> list[bool]:
        done = labelled_keys(self.out, self.rater)
        return [p.key in done for p in self.pairs]

    def next(self, after: int, unlabelled: bool = False, peek: bool = False) -> dict:
        """The pair at index after+1 (clamped); with `unlabelled`, the next one without a label from this rater.
        `peek` is a prefetch: it does not start that pair's timer."""
        with self._lock:
            total = len(self.pairs)
            flags = self._flags()
            remaining = flags.count(False)
            start = max(after + 1, 0)
            idx = min(start, total - 1)
            all_done = remaining == 0
            if unlabelled and not all_done:
                order = list(range(start, total)) + list(range(0, min(start, total)))
                idx = next(i for i in order if not flags[i])
            pair = self.pairs[idx]
            if not peek:
                self._shown[idx] = self._clock()
            out = {
                "index": idx,
                "total": total,
                "take_id": pair.take_id,
                "candidate": pair.candidate_ref,
                "plate": pair.plate_ref,
                "has_plate": pair.plate is not None,
                "candidate_v": self._version(pair.candidate),
                "plate_v": self._version(pair.plate),
                "labelled": flags[idx],
                "remaining": remaining,
            }
            if all_done:
                out["all_done"] = True
            return out

    # --- labels ---------------------------------------------------------------------------

    def label(self, payload: object) -> dict:
        """Validate and append one label; HttpError 400 for bad input, 404 unknown pair, 409 stale page."""
        if not isinstance(payload, dict):
            raise HttpError(400, "body must be a JSON object")
        index = payload.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise HttpError(400, "index must be an integer")
        if not 0 <= index < len(self.pairs):
            raise HttpError(404, "no such pair")
        pair = self.pairs[index]
        if payload.get("take_id") != pair.take_id:
            raise HttpError(409, "the page is out of date: reload it")
        verdict = payload.get("verdict")
        if not isinstance(verdict, str):
            raise HttpError(400, "verdict must be a string")
        with self._lock:
            seconds = payload.get("seconds")
            if seconds is None:
                seconds = max(self._clock() - self._shown.get(index, self._clock()), 0.0)
            try:
                rec = LabelRecord.for_pair(pair, verdict, payload.get("reasons", []), seconds, self.rater)
            except ValueError as e:
                raise HttpError(400, str(e)) from e
            line = append_label(self.out, rec)
            self._session.append((line, index))
            return {"ok": True, "count": len(read_labels(self.out))}

    def undo(self) -> dict:
        """Remove the label this server session wrote last; 409 when there is none."""
        with self._lock:
            if not self._session:
                raise HttpError(409, "nothing to undo in this session")
            line, index = self._session.pop()
            if not remove_line(self.out, line):
                raise HttpError(409, "that label is no longer in the file")
            return {"ok": True, "index": index, "count": len(read_labels(self.out))}


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

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as e:
            raise HttpError(400, "Content-Length required") from e
        if length < 0 or length > MAX_BODY:
            raise HttpError(413, "body too large")
        return self.rfile.read(length)

    # --- routes -------------------------------------------------------------------------------

    def do_GET(self) -> None:
        try:
            self._guard(check_origin=False)
            path, _, query = self.path.partition("?")
            app = self.server.app
            if path in {"/", "/index.html"}:
                nonce = secrets.token_urlsafe(12)
                csp = (
                    f"default-src 'none'; img-src 'self'; connect-src 'self'; "
                    f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'"
                )
                self._send(
                    HTTPStatus.OK,
                    render_page(nonce, app.rater).encode("utf-8"),
                    "text/html; charset=utf-8",
                    {"Content-Security-Policy": csp, "Cache-Control": "no-store"},
                )
            elif path == "/favicon.ico":  # browsers ask for it unprompted: answer quietly
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            elif path == "/api/next":
                q = parse_qs(query)
                try:
                    after = int(q.get("after", ["-1"])[0])
                except ValueError as e:
                    raise HttpError(400, "after must be an integer") from e
                self._json(
                    200,
                    app.next(after, q.get("unlabelled", ["0"])[0] == "1", q.get("peek", ["0"])[0] == "1"),
                )
            elif path.startswith("/img/"):
                parts = path[len("/img/") :].split("/")
                if len(parts) != 2:
                    raise HttpError(404, "not found")
                body, ctype = app.image_bytes(app.image_path(parts[0], parts[1]))
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
            route = self.path.partition("?")[0]
            if route not in {"/api/label", "/api/undo"}:
                raise HttpError(404, "not found")
            raw = self._body()
            if route == "/api/undo":
                self._json(200, self.server.app.undo())
                return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise HttpError(400, f"body is not valid JSON: {e}") from e
            self._json(200, self.server.app.label(payload))
        except HttpError as e:
            self._fail(e)
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(pairs: list[Pair], out: Path, rater: str, port: int = DEFAULT_PORT) -> LabelHTTPServer:
    """Bind 127.0.0.1:port (0 = any free port) and return the server; the caller runs serve_forever()."""
    httpd = LabelHTTPServer((HOST, port), Handler)
    httpd.app = LabelApp(pairs, out, rater)
    return httpd


def serve(
    pairs: list[Pair], out: Path, rater: str, port: int = DEFAULT_PORT, open_browser: bool = True
) -> int:
    """Run the UI until Ctrl+C. Returns a process exit code (2 when the port is taken)."""
    try:
        httpd = make_server(pairs, out, rater, port)
    except OSError as e:
        print(f"error: cannot listen on {HOST}:{port}: {e}", file=sys.stderr)
        return 2
    url = f"http://{HOST}:{httpd.server_address[1]}/"
    print(f"labelling {httpd.app.pair_count} pair(s) as {rater} -> {Path(out)}", flush=True)
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
