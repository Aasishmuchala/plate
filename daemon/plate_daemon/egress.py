"""Egress: the one door client pixels leave by. Custody, host allowlist, metadata stripping, opaque ids, leak
scan and the outbound log, in that order, on every vendor request (BUILD_PLAN section 7, decisions 9 and 10).

`Egress(settings, log)` is the only place in the daemon that builds an `httpx.Client` for a vendor; engine
adapters never open a connection of their own: they are handed `Egress.transport(run, route)` (an
`httpx.BaseTransport`) and every request they build passes through the same guard as `Egress.send`. An SDK
that wants a client (the Anthropic composer and judge) gets `Egress.client(run, route)`. A test in
`daemon/tests/test_egress.py` fails if `httpx.Client(` or `httpx.AsyncClient(` appears anywhere else in
`daemon/` or `engines/` outside the transports module.

The guard, before a byte leaves:
1. custody: the route's effective grade (pending counts as D) must be in `run.allowed_grades`, else
   `CustodyViolation`; nothing is built, logged or sent.
2. destination: https on the default port, no userinfo, host covered by `route.allowed_hosts` (exact name or
   `.suffix`), else `EgressBlocked`. An empty allowlist sends nothing.
3. sanitise: every image payload (multipart part, raw body, or base64 / data URL inside JSON) must be a PNG
   and is rewritten by `plate_pixels.icc.strip_metadata_png` (no tEXt / iTXt / zTXt / eXIf / tIME); every
   file name becomes `<opaque_id>.png` with a fresh ULID; JPEG, TIFF, WebP, EXR and friends are refused
   (egress is PNG only, never JPEG); unknown binary payloads are refused on NDA runs. Compressed or
   unparseable bodies are refused: what cannot be read cannot be checked.
4. leak scan: the URL, every header and the text of the body (JSON keys and strings, form fields, multipart
   names and text parts, PNG colour-profile names) are searched for the run's identifiers (project and shot
   slugs, packet id and folder, camera, layer, object, material and node names) and for this machine's PLATE
   folders and home path, else `LeakBlocked`. Slugs also match with `-`, `_`, a space or nothing between
   their words; plain single words (a layer called "SKY") are not identifiers and are not scanned for.
5. log: one fsync'd line in `outbound.jsonl` with the BLAKE3 of the exact body, then the request is sent,
   then its status and bytes in. Through `EgressTransport` the response body is not read here: it streams on
   to the adapter (whose download enforces its own byte cap and deadline, router plan T130) and the status and
   the bytes counted are logged when the body ends or is closed.

`fetch(url, route)` applies the same destination rule to result downloads (no credentials are sent).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import time
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import httpx
from blake3 import blake3

from plate_engines.models import host_allowed
from plate_pixels.icc import iter_png_chunks, png_chunk_types, strip_metadata_png

from .custody import CustodyViolation, Grade, effective_grade, project_allowed, require_route
from .outbound_log import OutboundLog

__all__ = [
    "CustodyViolation",
    "Egress",
    "EgressBlocked",
    "EgressRun",
    "EgressTransport",
    "FetchFailed",
    "LeakBlocked",
    "PreparedRequest",
    "new_opaque_id",
]

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DEFAULT_TIMEOUT_S = 60.0
MIN_TERM_LEN = 3
# A pure base64 string at least this long inside JSON is a blob, not text: decoded and sniffed, never scanned.
BLOB_MIN_CHARS = 256
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
METADATA_CHUNKS = frozenset({"tEXt", "iTXt", "zTXt", "eXIf", "tIME"})
_DATA_URL = re.compile(r"^data:([a-z0-9.+/-]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)
_B64 = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
_BOUNDARY = re.compile(r"boundary=(?:\"([^\"]+)\"|([^;\s]+))", re.IGNORECASE)
_DISPOSITION_NAME = re.compile(r'\bname="([^"]*)"', re.IGNORECASE)
_DISPOSITION_FILE = re.compile(r'\bfilename\*?="?([^";]*)"?', re.IGNORECASE)
_SLUG_SEP = "[-_ ]?"  # tula-wot, tula_wot, tula wot, tulawot
_PATH_SEP = r"[\\/]+"
# Raw base64 prefixes of the image formats egress knows (PNG, JPEG, GIF, RIFF/WebP, TIFF, EXR, PSD).
_B64_IMAGE_PREFIXES = ("iVBORw0KGgo", "/9j/", "_9j_", "R0lGOD", "UklGR", "SUkq", "TU0AK", "di8xAQ", "OEJQUw")
# Canvas node kinds (ui/src/nodeTypes.ts): a label that is only a kind ("Mask", "Edit 2") is not an identifier.
NODE_KINDS = frozenset(
    "plate edit mask comp colour lightmix recolour gate judge croppair motion people set compare export "
    "review light scout concept reference frame atmosphere sky".split()
)


class EgressBlocked(RuntimeError):
    """The request may not leave: destination not allowed, no transport, or a body that cannot be checked."""

    code = "egress"


class LeakBlocked(EgressBlocked):
    """The request carries something that identifies the client, the project or this machine."""

    code = "leak"


class FetchFailed(RuntimeError):
    """A result download came back with a non-2xx status."""


def new_opaque_id(now_ms: int | None = None, entropy: bytes | None = None) -> str:
    """A 26-character Crockford base32 ULID (48-bit millisecond time, 80 random bits). Carries no name."""
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    raw = secrets.token_bytes(10) if entropy is None else entropy
    if len(raw) != 10 or not 0 <= ts < 1 << 48:
        raise ValueError("a ULID needs a 48-bit millisecond time and 10 bytes of entropy")
    n = (ts << 80) | int.from_bytes(raw, "big")
    return "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


# ---------------------------------------------------------------------------- the run and the request


def _clean_terms(terms: Iterable[Any]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for t in terms:
        if t is None:
            continue
        s = str(t).strip()
        if len(s) >= MIN_TERM_LEN and s.lower() not in seen:
            seen[s.lower()] = s
    return tuple(seen.values())


def _compound(name: Any) -> bool:
    """An identifier, not a plain word: it has a separator, a digit or a space (CAM_C03, VEG_tree_01)."""
    if not isinstance(name, str):
        return False
    s = name.strip()
    return len(s) >= 4 and bool(re.search(r"[-_.\s\d]", s))


def _generic_node_label(label: str) -> bool:
    s = label.strip().lower()
    return re.sub(r"[\s_-]*\d+$", "", s) in NODE_KINDS or not _compound(s)


def _path_terms(p: Any, *, with_name: bool = False) -> list[str]:
    if p is None or str(p).strip() in ("", ".", "/", "\\"):
        return []
    path = Path(str(p))
    out = [str(path), path.as_posix()]
    if with_name and _compound(path.name):
        out.append(path.name)
    return out


@dataclass(frozen=True)
class EgressRun:
    """What egress needs to know about the run that sends: its id, the grades it may use, and the words that
    never leave (`slugs` match across -, _, space or nothing; `terms` match literally; `paths` with either
    separator). Build it with `for_project` so nothing is forgotten."""

    run_id: str
    allowed_grades: frozenset[Grade]
    slugs: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    nda: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_grades", frozenset(Grade.parse(g) for g in self.allowed_grades))
        object.__setattr__(self, "slugs", _clean_terms(self.slugs))
        object.__setattr__(self, "terms", _clean_terms(self.terms))
        object.__setattr__(self, "paths", _clean_terms(self.paths))

    @classmethod
    def for_project(
        cls,
        project: Any,
        run_id: str,
        *,
        packet: Mapping[str, Any] | None = None,
        legend: Mapping[str, Any] | None = None,
        packet_dir: Path | str | None = None,
        node_names: Iterable[str] = (),
        extra_terms: Iterable[str] = (),
    ) -> EgressRun:
        """`project` is a `projects.Project` or a project.json dict; `packet` the packet manifest; `legend` its
        `id_legend.json`. Allowed grades = project custody ∩ packet `policy.custody` (NDA caps at C0/C1)."""
        doc = project if isinstance(project, Mapping) else (getattr(project, "doc", None) or {})
        project_custody = doc.get("custody", getattr(project, "custody", None))
        packet = packet or {}
        packet_custody = (packet.get("policy") or {}).get("custody")
        allowed = project_allowed(project_custody, packet_custody)
        nda = bool((project_custody or {}).get("nda")) or bool((packet_custody or {}).get("nda"))

        slugs = [
            doc.get("project_id"),
            getattr(project, "project_id", None),
            packet.get("project_id"),
            packet.get("shot_id"),
        ]
        terms: list[Any] = [packet.get("packet_id"), packet.get("supersedes")]
        camera = packet.get("camera") or {}
        if isinstance(camera, Mapping) and _compound(camera.get("name")):
            terms.append(camera["name"])
        for entry in (legend or {}).get("entries") or []:
            if isinstance(entry, Mapping):
                terms.extend(entry[k] for k in ("name", "layer", "material") if _compound(entry.get(k)))
        for node in (doc.get("graph") or {}).get("nodes") or []:
            if not isinstance(node, Mapping):
                continue
            data = node.get("data") if isinstance(node.get("data"), Mapping) else {}
            for label in (node.get("name"), node.get("label"), data.get("label"), data.get("name")):
                if isinstance(label, str) and not _generic_node_label(label):
                    terms.append(label)
        terms.extend(n for n in node_names if isinstance(n, str) and not _generic_node_label(n))
        terms.extend(extra_terms)
        paths = _path_terms(packet_dir, with_name=True) + _path_terms(getattr(project, "root", None))
        return cls(
            run_id=run_id,
            allowed_grades=frozenset(allowed),
            slugs=tuple(s for s in slugs if s),
            terms=tuple(t for t in terms if t),
            paths=tuple(paths),
            nda=nda,
        )


FileSpec = tuple[str | None, bytes, str | None] | tuple[str | None, bytes]


@dataclass
class PreparedRequest:
    """A vendor request before egress builds it. `files` maps (or lists) field -> (filename, bytes, type)."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    json: Any = None
    data: Mapping[str, Any] | None = None
    files: Mapping[str, FileSpec] | Sequence[tuple[str, FileSpec]] | None = None
    content: bytes | None = None


# ---------------------------------------------------------------------------- payload inspection


def _sniff(data: bytes) -> str | None:
    """The image format of a payload, or None when it is not an image egress knows about."""
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:4] == b"\x76\x2f\x31\x01":
        return "exr"
    if data[:4] == b"8BPS":
        return "psd"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"avif"):
        return "heif"
    return None


def _as_text(data: bytes) -> str | None:
    """The payload as text when it is UTF-8 without NULs, else None (binary)."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _png_profile_text(png: bytes) -> str:
    """The iCCP profile name and payload as text, for the leak scan (a profile can carry a machine name)."""
    out = []
    for ctype, payload in iter_png_chunks(png):
        if ctype == b"iCCP":
            name, _, rest = payload.partition(b"\x00")
            out.append(name.decode("latin-1"))
            try:
                out.append(zlib.decompress(rest[1:]).decode("latin-1"))
            except zlib.error:
                pass
        if ctype in (b"IDAT", b"IEND"):
            break
    return "\n".join(out)


@dataclass
class _Scrub:
    """One request's sanitising pass: the opaque ids it handed out and the text the leak scan reads."""

    nda: bool
    opaque_ids: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    def new_id(self) -> str:
        oid = new_opaque_id()
        self.opaque_ids.append(oid)
        return oid

    def payload(self, data: bytes, where: str) -> tuple[bytes, str | None]:
        """(bytes to send, image kind or None). PNGs are stripped, other images refused, text is scanned,
        unknown binary is refused on NDA runs and otherwise passes as it is."""
        kind = _sniff(data)
        if kind is None:
            text = _as_text(data)
            if text is not None:
                self.text.append(text)
            elif self.nda:
                raise LeakBlocked(f"{where}: an unknown binary payload cannot leave on an NDA run")
            return data, None
        if kind != "png":
            raise LeakBlocked(
                f"{where}: a {kind.upper()} image; egress sends metadata-stripped PNG only, never JPEG"
            )
        try:
            clean = strip_metadata_png(data)
        except ValueError as e:
            raise LeakBlocked(f"{where}: unreadable PNG ({e})") from e
        left = METADATA_CHUNKS & set(png_chunk_types(clean))
        if left:  # pragma: no cover - strip_metadata_png keeps an allowlist; this is the belt to its braces
            raise LeakBlocked(f"{where}: metadata chunks {sorted(left)} survived stripping")
        self.text.append(_png_profile_text(clean))
        return clean, kind


def _decode_b64(s: str) -> bytes | None:
    compact = s.strip()
    if not _B64.match(compact):
        return None
    padded = compact + "=" * (-len(compact) % 4)
    try:
        if "-" in compact or "_" in compact:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _scrub_string(s: str, scrub: _Scrub, where: str) -> str:
    """One JSON string: base64 and data-URL images are stripped and re-encoded, other blobs pass unscanned
    (refused on NDA runs), anything else is text for the leak scan."""
    m = _DATA_URL.match(s)
    if m is not None:
        raw = _decode_b64(m.group(2))
        if raw is None:
            raise LeakBlocked(f"{where}: an unreadable base64 data URL")
        clean, kind = scrub.payload(raw, where)
        if kind is None:
            return s
        scrub.new_id()
        return f"data:image/png;base64,{base64.b64encode(clean).decode('ascii')}"
    if (len(s) >= 16 and s.startswith(_B64_IMAGE_PREFIXES)) or len(s) >= BLOB_MIN_CHARS:
        raw = _decode_b64(s)
        if raw is not None and (_sniff(raw) is not None or len(s) >= BLOB_MIN_CHARS):
            clean, kind = scrub.payload(raw, where)
            if kind is None:
                return s
            scrub.new_id()
            enc = base64.urlsafe_b64encode(clean) if ("-" in s or "_" in s) else base64.b64encode(clean)
            return enc.decode("ascii")
    scrub.text.append(s)
    return s


def _scrub_json(obj: Any, scrub: _Scrub, where: str = "json") -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            scrub.text.append(str(k))
            out[k] = _scrub_json(v, scrub, f"{where}.{k}")
        return out
    if isinstance(obj, list):
        return [_scrub_json(v, scrub, f"{where}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str):
        return _scrub_string(obj, scrub, where)
    return obj


def _opaque_name(scrub: _Scrub, kind: str | None, filename: str) -> str:
    if kind == "png":
        ext = "png"
    else:
        suffix = Path(filename).suffix.lower().lstrip(".")
        ext = suffix if re.fullmatch(r"[a-z0-9]{1,5}", suffix) else "bin"
    return f"{scrub.new_id()}.{ext}"


def _scrub_multipart(content_type: str, body: bytes, scrub: _Scrub) -> bytes:
    """Parse, sanitise and re-serialise a multipart body with the same boundary (httpx's layout)."""
    m = _BOUNDARY.search(content_type)
    if m is None:
        raise LeakBlocked("a multipart body with no boundary cannot be checked")
    delimiter = b"--" + (m.group(1) or m.group(2)).encode("latin-1")
    segments = body.split(delimiter)
    if len(segments) < 2 or segments[0].strip() or not segments[-1].startswith(b"--"):
        raise LeakBlocked("an unparseable multipart body cannot be checked")
    out: list[bytes] = []
    for seg in segments[1:-1]:
        head, sep, payload = seg[2:].partition(b"\r\n\r\n")
        if not seg.startswith(b"\r\n") or not sep:
            raise LeakBlocked("an unparseable multipart part cannot be checked")
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        lines = [ln for ln in head.decode("utf-8", errors="replace").split("\r\n") if ln.strip()]
        disposition = [ln for ln in lines if ln.lower().startswith("content-disposition:")]
        if len(disposition) != 1:
            raise LeakBlocked("a multipart part needs exactly one Content-Disposition to be checked")
        others = [ln for ln in lines if not ln.lower().startswith("content-disposition:")]
        name_m = _DISPOSITION_NAME.search(disposition[0])
        file_m = _DISPOSITION_FILE.search(disposition[0])
        name = name_m.group(1) if name_m else ""
        payload, kind = scrub.payload(payload, f"multipart field {name!r}")
        scrub.text.append(name)
        scrub.text.extend(others)
        if file_m is None:
            if kind is not None:
                scrub.new_id()
            new_head = [f'Content-Disposition: form-data; name="{name}"', *others]
        else:  # the original file name never leaves (it is not scanned either: it is replaced)
            new_name = _opaque_name(scrub, kind, file_m.group(1))
            new_head = [f'Content-Disposition: form-data; name="{name}"; filename="{new_name}"', *others]
        out.append(b"\r\n" + "\r\n".join(new_head).encode("utf-8") + b"\r\n\r\n" + payload + b"\r\n")
    return b"".join(delimiter + p for p in out) + delimiter + b"--\r\n"


def _scrub_body(request: httpx.Request, body: bytes, scrub: _Scrub) -> bytes:
    if not body:
        return body
    if request.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
        raise LeakBlocked("a compressed request body cannot be checked; send it uncompressed")
    ctype = request.headers.get("content-type", "")
    low = ctype.lower()
    if low.startswith("multipart/"):
        return _scrub_multipart(ctype, body, scrub)
    if "json" in low:
        try:
            doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise LeakBlocked(f"an unparseable JSON body cannot be checked ({e})") from e
        cleaned = _scrub_json(doc, scrub)
        if cleaned == doc:
            return body
        return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if low.startswith("application/x-www-form-urlencoded"):
        scrub.text.append(unquote_plus(body.decode("utf-8", errors="replace")))
        return body
    clean, kind = scrub.payload(body, "request body")
    if kind is not None:
        scrub.new_id()
    return clean


# ---------------------------------------------------------------------------- the leak scanner


@lru_cache(maxsize=256)
def _scanner(
    slugs: tuple[str, ...], terms: tuple[str, ...], paths: tuple[str, ...]
) -> re.Pattern[str] | None:
    alts: list[str] = []
    for s in slugs:
        words = [re.escape(w) for w in re.split(r"[-_ ]+", s) if w]
        if words:
            alts.append(_SLUG_SEP.join(words))
    alts.extend(re.escape(t) for t in terms)
    for p in paths:
        pieces = [re.escape(x) for x in re.split(r"[\\/]+", p) if x]
        if pieces:
            alts.append((_PATH_SEP if p[:1] in "\\/" else "") + _PATH_SEP.join(pieces))
    if not alts:
        return None
    alts.sort(key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])", re.IGNORECASE)


def _find_leak(pattern: re.Pattern[str] | None, texts: Iterable[str]) -> str | None:
    if pattern is None:
        return None
    for text in texts:
        m = pattern.search(text) if text else None
        if m is not None:
            return m.group(0)
    return None


# ---------------------------------------------------------------------------- egress


def default_transport() -> httpx.BaseTransport:
    """Recorded fixtures unless PLATE_LIVE=1 (decision 8); the live transport refuses to exist without it."""
    from plate_engines.adapters.transport import FixtureTransport, LiveTransport

    if os.environ.get("PLATE_LIVE") == "1":
        return LiveTransport()
    return FixtureTransport()


def _route_id(route: Any) -> str:
    return str(getattr(route, "route_id", None) or getattr(route, "provider", None) or "?")


def _bytes_in(response: httpx.Response) -> int:
    return response.num_bytes_downloaded or len(response.content)


class Egress:
    """The vendor door. `settings` (a `config.Settings`, optional) adds this machine's PLATE folders to the
    words that never leave; `log` records every request; `transport` is the network (a MockTransport or
    FixtureTransport in tests, `LiveTransport` only under PLATE_LIVE=1; default `default_transport()`)."""

    def __init__(
        self,
        settings: Any,
        log: OutboundLog,
        transport: httpx.BaseTransport | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.settings = settings
        self.log = log
        self.timeout_s = timeout_s
        self._inner = transport
        self._client: httpx.Client | None = None
        self.machine_paths = self._paths_from(settings)

    @staticmethod
    def _paths_from(settings: Any) -> tuple[str, ...]:
        paths: list[str] = []
        if settings is not None:
            for attr in ("home", "projects_dir", "config_path"):
                paths.extend(_path_terms(getattr(settings, attr, None)))
            for p in getattr(settings, "packets_dirs", None) or []:
                paths.extend(_path_terms(p))
        try:
            paths.extend(_path_terms(Path.home()))
        except RuntimeError:  # pragma: no cover - no home directory
            pass
        return _clean_terms(paths)

    # ------------------------------------------------------------------ plumbing

    @property
    def inner(self) -> httpx.BaseTransport:
        if self._inner is None:
            self._inner = default_transport()
        return self._inner

    def _http(self) -> httpx.Client:
        if self._client is None:
            # trust_env=False: no proxy or netrc from the environment can reroute a request or add credentials.
            self._client = httpx.Client(
                transport=self.inner, timeout=self.timeout_s, follow_redirects=False, trust_env=False
            )
        return self._client

    def transport(self, run: EgressRun, route: Any) -> EgressTransport:
        """The transport an adapter is built with: every request it makes is guarded and logged."""
        return EgressTransport(self, run, route)

    def client(self, run: EgressRun, route: Any, *, timeout_s: float | None = None) -> httpx.Client:
        """An httpx.Client over `transport(run, route)`, for SDKs that take one (the Anthropic client)."""
        return httpx.Client(
            transport=self.transport(run, route),
            timeout=self.timeout_s if timeout_s is None else timeout_s,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ the guard

    @staticmethod
    def _destination(route: Any, url: httpx.URL) -> str:
        host = (url.host or "").lower()
        allowed = list(getattr(route, "allowed_hosts", None) or [])
        if url.scheme != "https":
            raise EgressBlocked(f"{url.scheme}:// never leaves the box; vendors are reached over https only")
        if url.userinfo:
            raise EgressBlocked("credentials inside a URL are refused")
        if url.port not in (None, 443):
            raise EgressBlocked(f"port {url.port} is not the https default")
        if not allowed:
            raise EgressBlocked(f"route {_route_id(route)!r} lists no allowed_hosts: nothing leaves")
        if not host_allowed(host, allowed):
            raise EgressBlocked(f"host {host!r} is not in route {_route_id(route)!r} allowed_hosts {allowed}")
        return host

    def guard(self, run: EgressRun | None, route: Any, request: httpx.Request) -> tuple[httpx.Request, str]:
        """Steps 1 to 5 of the module docstring: returns the sanitised request and its log id, or raises
        before anything is logged or sent. `run` is None only for result downloads."""
        route_id = _route_id(route)
        if run is not None:
            grade = require_route(route, run.allowed_grades, what=route_id)
        else:
            grade = effective_grade(route)
        host = self._destination(route, request.url)
        scrub = _Scrub(nda=bool(run is not None and run.nda))
        clean = _scrub_body(request, request.read(), scrub)
        headers = [
            (k, v)
            for k, v in request.headers.multi_items()
            if k.lower() not in ("content-length", "transfer-encoding")
        ]
        texts = [unquote_plus(str(request.url)), *(f"{k}: {v}" for k, v in headers), *scrub.text]
        hit = _find_leak(_scanner((), (), self.machine_paths), texts)
        if hit is None and run is not None:
            hit = _find_leak(_scanner(run.slugs, run.terms, run.paths), texts)
        if hit is not None:
            raise LeakBlocked(f"route {route_id!r}: the request carries {hit!r}, which never leaves the box")
        sanitised = httpx.Request(
            request.method, request.url, headers=headers, content=clean, extensions=request.extensions
        )
        entry_id = self.log.request(
            entry_id=new_opaque_id(),
            run_id=run.run_id if run is not None else None,
            route_id=route_id,
            host=host,
            method=request.method,
            path=request.url.path,
            bytes_out=len(clean),
            custody_grade=grade.value,
            opaque_ids=scrub.opaque_ids,
            body_hash=blake3(clean).hexdigest(),
        )
        return sanitised, entry_id

    # ------------------------------------------------------------------ the door

    def _build(self, request: PreparedRequest | httpx.Request) -> httpx.Request:
        if isinstance(request, httpx.Request):
            return request
        files = None
        if request.files is not None:
            items = request.files.items() if isinstance(request.files, Mapping) else request.files
            files = [(name, spec) for name, spec in items]
        return self._http().build_request(
            request.method,
            request.url,
            headers=dict(request.headers),
            json=request.json,
            data=dict(request.data) if request.data is not None else None,
            files=files,
            content=request.content,
        )

    def _round_trip(self, request: httpx.Request, entry_id: str) -> httpx.Response:
        try:
            response = self._http().send(request)
        except Exception as e:
            self.log.error(entry_id, error=f"{type(e).__name__}: {e}")
            raise
        self.log.response(entry_id, status=response.status_code, bytes_in=_bytes_in(response))
        return response

    def send(self, run: EgressRun, route: Any, request: PreparedRequest | httpx.Request) -> httpx.Response:
        """Custody first (before the request is even built), then guard, log, send, log. Body is read."""
        if run is None:
            raise CustodyViolation("a vendor request needs a run: its allowed grades decide the route")
        require_route(route, run.allowed_grades, what=_route_id(route))
        sanitised, entry_id = self.guard(run, route, self._build(request))
        return self._round_trip(sanitised, entry_id)

    def fetch(self, url: str, route: Any, *, run: EgressRun | None = None) -> bytes:
        """Download a result from a host the route allows; no credentials. Non-2xx raises `FetchFailed`."""
        if run is not None:
            require_route(route, run.allowed_grades, what=_route_id(route))
        sanitised, entry_id = self.guard(run, route, self._http().build_request("GET", url))
        response = self._round_trip(sanitised, entry_id)
        if response.status_code // 100 != 2:
            raise FetchFailed(f"download from {response.url.host} failed: {response.status_code}")
        return response.content


class EgressTransport(httpx.BaseTransport):
    """What an engine adapter is handed instead of a network: every request goes through `Egress.guard`."""

    def __init__(self, egress: Egress, run: EgressRun, route: Any) -> None:
        if run is None:
            raise CustodyViolation("an egress transport needs a run: its allowed grades decide the route")
        require_route(route, run.allowed_grades, what=_route_id(route))
        self.egress = egress
        self.run = run
        self.route = route
        self.allowed_hosts = tuple(getattr(route, "allowed_hosts", None) or ())

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        sanitised, entry_id = self.egress.guard(self.run, self.route, request)
        try:
            response = self.egress.inner.handle_request(sanitised)
        except Exception as e:
            self.egress.log.error(entry_id, error=f"{type(e).__name__}: {e}")
            raise
        # The body is not buffered here: a 300 MB result would sit in memory before any cap could see it. It
        # streams through, counted, and the outbound log gets its status and bytes when it ends or is closed.
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_LoggedStream(response.stream, self.egress.log, entry_id, response.status_code),
            extensions=response.extensions,
        )


class _LoggedStream(httpx.SyncByteStream):
    """A vendor response body passed through unread. Counts the bytes as they go by and writes the outbound
    log's `response` line once (end of body or close); a body that fails or is abandoned midway also gets an
    `error` line saying how far it got."""

    def __init__(self, inner: Any, log: OutboundLog, entry_id: str, status: int) -> None:
        self._inner = inner
        self._log = log
        self._entry_id = entry_id
        self._status = status
        self.bytes_in = 0
        self._ended = False
        self._logged = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._inner:
                self.bytes_in += len(chunk)
                yield chunk
        except Exception as e:
            self._write(error=f"{type(e).__name__}: {e}")
            raise
        self._ended = True
        self._write()

    def close(self) -> None:
        try:  # logged first: whoever sees the body closed sees its log lines too
            if not self._ended:
                self._write(error=f"the body was closed after {self.bytes_in} bytes, before it ended")
            else:
                self._write()
        finally:
            close = getattr(self._inner, "close", None)
            if close is not None:
                close()

    def _write(self, *, error: str | None = None) -> None:
        if self._logged:
            return
        self._logged = True
        self._log.response(self._entry_id, status=self._status, bytes_in=self.bytes_in)
        if error is not None:
            self._log.error(self._entry_id, error=error)
