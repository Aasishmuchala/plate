"""Docs routes (router plan T88): the repo's own `docs/**.md`, served as plain Markdown.

- `GET /docs/` answers an index: `{docs: [{name, title, href, bytes}]}`, `name` being the path under `docs/`
  with forward slashes (`artist-manual.md`, `runbooks/nightly.md`), `title` the file's first `# ` heading.
- `GET /docs/{name}.md` answers that file as `text/markdown; charset=utf-8`. The path is confined to the
  docs folder: only `.md` files, no `..`, no absolute or drive-letter names, no hidden names, and a link that
  resolves outside the folder is refused, and so is a name the filesystem cannot take (255+ characters); every
  refusal is the same 404, so a client learns nothing about what else is on disk. There is no renderer (P5); a client shows the raw file.

Both sit under the API prefix like every route (`/api/v0/docs/...`) and need the bearer token. They are not
served at a bare `/docs`: only `/api/` is token-gated (`auth.AuthMiddleware`), and a tokenless `/docs` is what
`test_auth.py` pins to 404. A browser tab cannot carry the bearer header, so a client opening a manual in a new
tab fetches it with the token first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from . import API, ApiError, require_token

DOCS_DIR = Path(__file__).resolve().parents[3] / "docs"  # <repo>/docs (daemon/plate_daemon/routes/docs.py)
MARKDOWN = "text/markdown; charset=utf-8"
TITLE_BYTES = 8192

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])


def _not_found(name: str) -> ApiError:
    return ApiError(404, {"error": "not_found", "detail": f"no doc {name!r}"})


def resolve_doc(name: str, root: Path | None = None) -> Path | None:
    """The `.md` file `name` names inside the docs folder, or None for anything else."""
    root = (root or DOCS_DIR).resolve()
    if not name or not name.endswith(".md") or "\x00" in name or "\\" in name or ":" in name:
        return None
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return None
    try:
        path = (root / Path(*parts)).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
    except (OSError, ValueError):  # a 255+ character name: ENAMETOOLONG is the same 404 as a missing file
        return None
    return path


def _title(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            head = fh.read(TITLE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return path.stem
    for line in head.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or path.stem
    return path.stem


def list_docs(root: Path | None = None) -> list[Path]:
    root = (root or DOCS_DIR).resolve()
    if not root.is_dir():
        return []
    found = []
    for p in sorted(root.rglob("*.md")):
        name = p.relative_to(root).as_posix()
        if resolve_doc(name, root) is not None:
            found.append(p)
    return found


@router.get("/docs/")
def docs_index() -> dict[str, Any]:
    base = f"{API}/docs/"
    root = DOCS_DIR.resolve()
    entries = []
    for p in list_docs():
        name = p.relative_to(root).as_posix()
        entries.append({"name": name, "title": _title(p), "href": base + name, "bytes": p.stat().st_size})
    return {"docs": entries}


@router.get("/docs/{name:path}")
def docs_file(name: str) -> Response:
    path = resolve_doc(name)
    if path is None:
        raise _not_found(name)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise _not_found(name) from e
    return Response(content=data, media_type=MARKDOWN, headers={"X-Content-Type-Options": "nosniff"})
