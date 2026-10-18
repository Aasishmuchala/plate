"""The dock's job poller: the work the canvas asks Max to do (Light match, Vantage preview, Scout, Receive).

A Light or Scout node cannot run in the daemon: it queues a plugin job (daemon nodes/plugin_queue.py)
and this poller, on the dock's timer, picks it up, does the work in the open scene and answers:

    GET  /api/v0/plugin/jobs?claim=1&since=<cursor>&limit=1    claim at most one queued job
    light.match            the reference picture GET /api/v0/assets/{reference_hash} into
                           <app-data>/cache/references, then actions.light.run: light.match (MaxGaffer,
                           with the job's locks and deep), apply, and rerender_packet with supersedes =
                           the job's packet, sent into the job's project
    light.vantage_preview  actions.light.vantage_preview(camera)
    scout                  actions.scout.run with views 0..count-1 and keep; the kept cameras are also
                           sent as full packets (scout_poses: real V-Ray cameras in the real scene)
    receive                a take pushed into Max: GET /api/v0/runs/{run_id} -> outputs[0], that picture from
                           GET /api/v0/assets/{hash} into <app-data>/cache/receive/<hash>.png, then
                           actions.receive.load_result(path, mode) (VFB history or the viewport background);
                           display only, the scene is not touched
    POST /api/v0/plugin/jobs/{id}/result   {ok, detail?, packet_id?, metrics?, poses?, packets?}

Rules: one job at a time (a tick that finds a job running, or the dock busy, does nothing); an action
that fails or raises answers ok false with the text, and nothing raises into Max; a Light or Scout job
whose packet came from another .max than the open one, or from a .max of that name bound to another
project than the job's, fails with that reason rather than relighting the wrong scene (when the
packet's manifest is not in this dock's packets folder, or names no .max, nothing can be compared: the
log says "packet <id>: scene check skipped (<why>)" and the job runs); an answer the daemon did not take
because it was away is posted again first on the next tick; a daemon that does not answer (or refuses
the token, or a token that cannot be sent) is asked less often (up to once a minute) until it does,
and said once per change of message.

Lost claims. The daemon marks a job claimed in the same transaction that lists it, so a claim whose
answer never arrives (the poll timed out while the daemon was slow) leaves the job claimed with nobody
working on it. On start, and after any poll that timed out, the poller first asks
    GET  /api/v0/plugin/jobs?state=claimed&since=0
and takes up every job claimed after this poller was made (started_utc): one it never ran is run (one
per tick, before any new claim); one it already ran, whose answer the daemon never took, is answered
ok false with "claim recovered after a lost answer" (a re-render is never repeated). Claims older than
the dock are the daemon's lease's (plugin_queue.CLAIM_LEASE_S): they go back to the queue on their own.

Standard library only, no Qt (tests/test_stdlib_only.py): the dock hands start() its QTimer, and the
daemon calls go through the controller's io (the dock's off-thread runner), so Max stays responsive
while pymxs work stays on the main thread.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import transport
from .config import DEFAULT_DAEMON_URL, app_data_dir, load_config, packets_dir
from .fsutil import atomic_write_bytes

DEFAULT_INTERVAL_S = 5.0
CLAIM_LIMIT = 1  # one job at a time: the others stay queued for the next tick (or another dock)
JOBS_PATH = "/api/v0/plugin/jobs"
POLL_TIMEOUT_S = 3.0
RECOVER_LIMIT = 50  # claimed jobs asked for when recovering lost claims
RECOVERED_DETAIL = "claim recovered after a lost answer"
JOBS_KEYS = ("jobs",)  # what a GET /plugin/jobs answer must hold to be the daemon's
RESULT_TIMEOUT_S = 10.0
RUN_TIMEOUT_S = 10.0
ASSET_TIMEOUT_S = 120.0
DETAIL_MAX = 1900  # the daemon caps detail at 2000 characters
MAX_BACKOFF_TICKS = 12  # 12 x 5 s: a dead daemon is asked about once a minute
HISTORY = 50
SCOUT_DEFAULT_COUNT = 6
SCOUT_MAX_COUNT = 24
ASSET_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
METRIC_KEYS = ("de00", "score", "iterations", "polish_gain")
ESSENTIAL_KEYS = ("ok", "detail", "packet_id")

ACTION_LIGHT = "light.match"
ACTION_VANTAGE = "light.vantage_preview"
ACTION_SCOUT = "scout"
ACTION_RECEIVE = "receive"
ACTIONS = (ACTION_LIGHT, ACTION_VANTAGE, ACTION_SCOUT, ACTION_RECEIVE)
DEFAULT_RECEIVE_MODE = "vfb_history"

# Content-Type of GET /assets/{hash} -> the extension MaxGaffer reads the reference by.
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/tiff": ".tif",
    "image/x-exr": ".exr",
    "image/aces": ".exr",
    "image/webp": ".webp",
}
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"v/1\x01", ".exr"),
)

Log = Callable[..., None]


class JobFailed(Exception):
    """Stops one job with the text its ok-false answer carries."""


def _direct(fn: Callable[[], Any]) -> Any:
    return fn()


def _no_log(message: str, err: bool = False) -> None:
    return None


def _never_busy() -> bool:
    return False


def extension_for(content_type: str, data: bytes) -> str:
    """The file extension of a downloaded picture: from its Content-Type, else its first bytes."""
    kind = str(content_type or "").split(";")[0].strip().lower()
    if kind in _EXTENSIONS:
        return _EXTENSIONS[kind]
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return ".bin"


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def match_metrics(matched: Any) -> dict[str, Any]:
    """The numbers of a MaxGaffer match worth keeping with the run (de00, score, iterations,
    polish_gain); NaN, infinities and non-numbers are left out (the daemon refuses NaN)."""
    if not isinstance(matched, Mapping):
        return {}
    return {k: matched[k] for k in METRIC_KEYS if _finite(matched.get(k))}


def _clip(text: str) -> str:
    text = str(text)
    return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1] + "…"


def _jsonable(value: Any) -> Any:
    """value with every non-finite float replaced by None, tuples as lists and unknown objects as
    their text, so json.dumps(allow_nan=False) never refuses an answer."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def clean_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """The answer as the daemon takes it: ok a bool, detail clipped, None values dropped."""
    out = {k: _jsonable(v) for k, v in body.items() if v is not None}
    out["ok"] = bool(body.get("ok"))
    if "detail" in out:
        out["detail"] = _clip(out["detail"])
    return out


def _fail(detail: str) -> dict[str, Any]:
    return {"ok": False, "detail": _clip(detail)}


def _utc(text: Any) -> datetime | None:
    """An ISO-8601 stamp of the daemon's (2026-10-17T09:41:07.123456Z) as an aware datetime, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _errors(result: Any) -> list[str]:
    errors = getattr(result, "errors", None)
    if errors is None and isinstance(result, Mapping):
        errors = result.get("errors")
    return [str(e) for e in errors or []]


class Poller:
    """See the module docstring. config None reads config.json at every poll (a new token applies at
    once); controller is the dock's (LiveController): its io runs the daemon calls, its
    job_options(cameras, project_id, progress) gives the send options (recipes, custody) and its
    note_packet(camera, packet_id) learns the relit packet. Hooks: log(message, err=False), busy()
    (True skips the tick: the artist is in the middle of something), on_start(job) and
    on_finish(job, body) around each job. started_utc (when the poller was made) marks the claims
    that can be this dock's when a claim's answer was lost (module docstring)."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        controller: Any = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        *,
        io: Callable[[Callable[[], Any]], Any] | None = None,
        log: Log | None = None,
        busy: Callable[[], bool] | None = None,
        on_start: Callable[[dict[str, Any]], None] | None = None,
        on_finish: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
        cache_dir: Path | str | None = None,
    ):
        self.config = config
        self.controller = controller
        self.interval_s = float(interval_s)
        self.cursor = 0
        self.active = False  # inside a tick (its daemon waits spin Qt events: the dock checks this)
        self.last_error: str | None = None
        self.handled: list[dict[str, Any]] = []
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._io = io
        self._log = log or _no_log
        self._busy = busy or _never_busy
        self._on_start = on_start
        self._on_finish = on_finish
        self._timer: Any = None
        self._skip = 0
        self._failures = 0
        self._unsent: list[tuple[str, dict[str, Any]]] = []
        self.started_utc = datetime.now(timezone.utc)
        self._recovery_due = False  # ask for claimed jobs before the next claim (start, a poll timeout)
        self._recovery: list[dict[str, Any]] = []  # claimed jobs taken back, run one per tick
        self._handlers: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
            ACTION_LIGHT: self._light_match,
            ACTION_VANTAGE: self._vantage_preview,
            ACTION_SCOUT: self._scout,
            ACTION_RECEIVE: self._receive,
        }

    # ------------------------------------------------------------------ the timer
    def start(self, timer: Any) -> bool:
        """Drive the poller from timer (a QTimer, or anything with timeout.connect, start(ms) and
        stop()) every interval_s. Starting again with the same timer only restarts it."""
        if timer is None:
            return False
        if self._timer is not timer:
            self.stop()
            timer.timeout.connect(self.tick)
            self._timer = timer
        self._recovery_due = True  # a claim answered while the dock was away may be waiting
        timer.start(max(1, int(round(self.interval_s * 1000))))
        return True

    def stop(self) -> None:
        if self._timer is None:
            return
        try:
            self._timer.stop()
        except RuntimeError:  # the C++ timer went with its dock
            pass

    @property
    def running(self) -> bool:
        timer = self._timer
        if timer is None:
            return False
        try:
            return bool(timer.isActive())
        except (RuntimeError, AttributeError):
            return False

    # ------------------------------------------------------------------ one tick
    def tick(self) -> list[dict[str, Any]]:
        """One timer tick. Skipped while a tick runs (the daemon waits spin events), while backing off
        from a daemon that did not answer, and while busy() says so. Never raises."""
        if self.active:
            return []
        if self._skip > 0:
            self._skip -= 1
            return []
        try:
            if self._busy():
                return []
        except Exception:  # noqa: BLE001 a broken hook must not stop the timer
            return []
        self.active = True
        try:
            return self.poll_once()
        except (transport.TransportError, ValueError) as exc:  # a token, an answer: ask less often
            self._backoff(f"job poller: {exc}")
            return []
        except Exception as exc:  # noqa: BLE001 a timer callback must never raise into Max
            why = f"job poller: {type(exc).__name__}: {exc}"
            if why != self.last_error:
                self._log(why, err=True)
            self.last_error = why
            return []
        finally:
            self.active = False

    def poll_once(self) -> list[dict[str, Any]]:
        """Post any answer still owed, take up lost claims when due (module docstring), then claim at
        most one queued job, run it and post its answer. Returns a record {id, action, body, posted}
        per job handled."""
        cfg = self._cfg()
        if self._unsent and not self._flush(cfg):
            return []
        if self._recovery_due and not self._recover_claims(cfg):
            return []
        if self._recovery:
            return [self._take_up(self._recovery.pop(0), cfg)]
        params = {"claim": True, "since": self.cursor, "limit": CLAIM_LIMIT}
        try:
            answer = self._call(
                lambda: transport.get_json(JOBS_PATH, cfg, POLL_TIMEOUT_S, params, keys=JOBS_KEYS)
            )
        except transport.DaemonTimeout as exc:  # the daemon may have claimed a job for this lost answer
            self._recovery_due = True
            self._backoff(f"job poll: {exc}")
            return []
        except (transport.TransportError, ValueError) as exc:
            self._backoff(f"job poll: {exc}")
            return []
        self._recovered()
        jobs = answer.get("jobs") if isinstance(answer, dict) else None
        cursor = answer.get("next") if isinstance(answer, dict) else None
        if isinstance(cursor, int) and cursor > self.cursor:
            self.cursor = cursor
        out = []
        for job in [j for j in jobs or [] if isinstance(j, dict) and j.get("id")][:CLAIM_LIMIT]:
            seq = job.get("seq")
            if isinstance(seq, int) and seq > self.cursor:
                self.cursor = seq
            out.append(self.run_job(job, cfg))
        return out

    def run_job(self, job: dict[str, Any], cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one claimed job and post its answer. An action that fails or raises is an ok-false
        answer; on_finish always follows on_start (the dock is never left busy)."""
        cfg = dict(cfg) if cfg is not None else self._cfg()
        job_id, action = str(job.get("id")), str(job.get("action") or "")
        self._hook(self._on_start, job)
        body: dict[str, Any] = {"ok": False, "detail": "the job did not run"}
        record = {"id": job_id, "action": action, "body": body, "posted": "not posted"}
        try:
            try:
                handler = self._handlers.get(action)
                if handler is None:
                    body = _fail(f"this dock does not handle {action!r} (it handles {', '.join(ACTIONS)})")
                else:
                    body = handler(job, cfg)
            except JobFailed as exc:
                body = _fail(f"{action}: {exc}")
            except Exception as exc:  # noqa: BLE001 an action that raises is an ok-false answer, never a crash
                body = _fail(f"{action}: {type(exc).__name__}: {exc}")
            body = clean_body(body)
            record["body"] = body
            record["posted"] = self._post_result(job_id, body, cfg)
        finally:
            self.handled = [*self.handled, record][-HISTORY:]
            self._hook(self._on_finish, job, body)
        return record

    # ------------------------------------------------------------------ lost claims
    def _claimed_here(self, job: Mapping[str, Any]) -> bool:
        """The job was claimed after this poller was made (so the lost answer may have been ours)."""
        claimed = _utc(job.get("claimed_utc"))
        return claimed is not None and claimed > self.started_utc

    def _recover_claims(self, cfg: Mapping[str, Any]) -> bool:
        """GET the claimed jobs and keep the ones claimed since started_utc for _take_up; False (backing
        off, the recovery still due) when the daemon does not answer."""
        params = {"state": "claimed", "since": 0, "limit": RECOVER_LIMIT}
        try:
            answer = self._call(
                lambda: transport.get_json(JOBS_PATH, cfg, POLL_TIMEOUT_S, params, keys=JOBS_KEYS)
            )
        except (transport.TransportError, ValueError) as exc:
            self._backoff(f"job recovery: {exc}")
            return False
        self._recovery_due = False
        self._recovered()
        owed = {job_id for job_id, _body in self._unsent}
        waiting = {str(job.get("id")) for job in self._recovery}
        listed = answer.get("jobs") if isinstance(answer, dict) else None
        found = [
            job
            for job in (listed if isinstance(listed, list) else [])
            if isinstance(job, dict)
            and job.get("id")
            and str(job.get("id")) not in owed | waiting
            and self._claimed_here(job)
        ]
        if found:
            self._log(f"job poller: {len(found)} claimed job(s) taken up again after a lost answer")
        self._recovery.extend(found)
        return True

    def _take_up(self, job: dict[str, Any], cfg: Mapping[str, Any]) -> dict[str, Any]:
        """A recovered claim: run it when this poller never did, else answer it ok false."""
        seq = job.get("seq")
        if isinstance(seq, int) and seq > self.cursor:
            self.cursor = seq
        job_id = str(job.get("id"))
        if any(record.get("id") == job_id for record in self.handled):
            body = clean_body(_fail(RECOVERED_DETAIL))
            self._log(f"job {job_id}: {RECOVERED_DETAIL} (it already ran here)", err=True)
            record = {
                "id": job_id,
                "action": str(job.get("action") or ""),
                "body": body,
                "posted": "not posted",
            }
            record["posted"] = self._post_result(job_id, body, cfg)
            self.handled = [*self.handled, record][-HISTORY:]
            return record
        return self.run_job(job, cfg)

    # ------------------------------------------------------------------ the daemon
    def _cfg(self) -> dict[str, Any]:
        cfg = dict(self.config) if self.config is not None else load_config()
        cfg.setdefault("daemon_url", DEFAULT_DAEMON_URL)
        return cfg

    def _call(self, fn: Callable[[], Any]) -> Any:
        """fn through the io runner, with its exception re-raised as itself (the dock's runner turns
        exceptions into text, which would lose the transport error's type)."""
        io = self._io or getattr(self.controller, "io", None) or _direct

        def safe() -> tuple[bool, Any]:
            try:
                return True, fn()
            except Exception as exc:  # noqa: BLE001 handed back to the caller below
                return False, exc

        ok, value = io(safe)
        if not ok:
            raise value
        return value

    def _backoff(self, why: str) -> None:
        if why != self.last_error:
            self._log(why, err=True)
        self.last_error = why
        self._failures += 1
        self._skip = min(MAX_BACKOFF_TICKS, 2 ** min(self._failures - 1, 4) - 1)

    def _recovered(self) -> None:
        if self.last_error is not None:
            self._log("job poller: the daemon answers again")
        self.last_error = None
        self._failures = 0
        self._skip = 0

    def _send_result(self, job_id: str, body: dict[str, Any], cfg: Mapping[str, Any]) -> str:
        """POST the answer once: posted | away (the daemon did not answer) | dropped (404/409: the
        daemon no longer wants it) | refused: <why>. A 422 is retried once with only ok, detail and
        packet_id (the extras were what it refused)."""
        path = f"{JOBS_PATH}/{urllib.parse.quote(job_id, safe='')}/result"
        try:
            self._call(lambda: transport.post_json(path, body, cfg, RESULT_TIMEOUT_S))
            return "posted"
        except transport.DaemonUnreachable:
            return "away"
        except transport.UnexpectedStatus as exc:
            if exc.status_code in (404, 409):
                self._log(f"job {job_id}: the daemon no longer takes an answer (HTTP {exc.status_code})")
                return "dropped"
            slim = {k: body[k] for k in ESSENTIAL_KEYS if k in body}
            if exc.status_code == 422 and slim != body:
                return self._send_result(job_id, slim, cfg)
            refused = str(exc)
        except (transport.TransportError, ValueError) as exc:
            refused = str(exc)
        self._log(f"job {job_id}: answer refused: {refused}", err=True)
        return f"refused: {refused}"

    def _post_result(self, job_id: str, body: dict[str, Any], cfg: Mapping[str, Any]) -> str:
        """_send_result, keeping an answer the daemon was away for (posted again next tick: queued)."""
        sent = self._send_result(job_id, body, cfg)
        if sent != "away":
            return sent
        self._unsent.append((job_id, body))
        self._log(f"job {job_id}: the answer waits for the daemon", err=True)
        return "queued"

    def _flush(self, cfg: Mapping[str, Any]) -> bool:
        """Post the answers still owed, oldest first; False while the daemon is still away."""
        while self._unsent:
            job_id, body = self._unsent[0]
            if self._send_result(job_id, body, cfg) == "away":
                self._backoff("job poller: the daemon is still away")
                return False
            self._unsent.pop(0)
        return True

    def fetch_reference(self, key: str, cfg: Mapping[str, Any], project_id: Any = None) -> Path:
        """The job's reference picture as a local file: <cache>/references/<hash>.<ext>, downloaded
        from GET /api/v0/assets/{hash} once (a hash names immutable bytes, so a cached file is reused)."""
        if not ASSET_KEY_RE.match(str(key or "")):
            raise JobFailed(f"reference_hash {key!r} is not a BLAKE3 key")
        return self._download(key, cfg, project_id, "references", "reference picture")

    def fetch_take(self, key: str, cfg: Mapping[str, Any], project_id: Any = None) -> Path:
        """The take a receive job shows as a local file: <cache>/receive/<hash>.png (the extension follows the
        daemon's Content-Type; a take is a PNG), downloaded from GET /api/v0/assets/{hash} once."""
        if not ASSET_KEY_RE.match(str(key or "")):
            raise JobFailed(f"the run's output {key!r} is not a BLAKE3 key")
        return self._download(key, cfg, project_id, "receive", "take", fallback_ext=".png")

    def _download(
        self,
        key: str,
        cfg: Mapping[str, Any],
        project_id: Any,
        folder_name: str,
        what: str,
        fallback_ext: str | None = None,
    ) -> Path:
        """GET /api/v0/assets/{key} into <cache>/<folder_name>/<key>.<ext>; a cached file is reused (a hash
        names immutable bytes)."""
        folder = (self.cache_dir or app_data_dir() / "cache") / folder_name
        for cached in sorted(folder.glob(f"{key}.*")) if folder.is_dir() else []:
            if cached.is_file() and cached.stat().st_size > 0:
                return cached
        params = {"project_id": str(project_id)} if project_id else None
        try:
            data, content_type = self._call(
                lambda: transport.get_bytes(f"/api/v0/assets/{key}", cfg, ASSET_TIMEOUT_S, params)
            )
        except transport.TransportError as exc:
            raise JobFailed(f"{what} {key[:12]}: {exc}") from exc
        if not data:
            raise JobFailed(f"{what} {key[:12]}: the daemon sent no bytes")
        ext = extension_for(content_type, data)
        if ext == ".bin" and fallback_ext:
            ext = fallback_ext
        return atomic_write_bytes(folder / f"{key}{ext}", data)

    # ------------------------------------------------------------------ the actions
    def _send_options(self, cameras: list[str], job: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
        """What a packet this job sends carries: the job's project (the relit packet must land where
        the run looks for it), the dock's recipes and custody when the controller has them, the config."""
        project_id = job.get("project_id")
        maker = getattr(self.controller, "job_options", None)
        opts: dict[str, Any] = {}
        if callable(maker):
            try:
                opts = dict(maker(cameras, project_id, self._progress))
            except Exception as exc:  # noqa: BLE001 fall back to the job's own project
                self._log(f"job options: {type(exc).__name__}: {exc}", err=True)
        opts.setdefault("progress", self._progress)
        if project_id:
            opts["project_id"] = str(project_id)
        opts["config"] = dict(cfg)
        return opts

    def _progress(self, done: int, total: int, message: str) -> None:
        if message:
            self._log(str(message))

    def _scene_mismatch(self, job: Mapping[str, Any], cfg: Mapping[str, Any]) -> str | None:
        """Why the open scene is not the job's, or None. The packet's manifest names the .max it came
        from (compared by file name, case-folded as Windows spells it); when that matches, the job's
        project must be the one the open scene is bound to (_project_mismatch), because two client
        folders can each hold a master.max. When the manifest is not in this dock's packets folder, or
        names no .max (sent unsaved), nothing can be compared: the log says the check was skipped and
        the job runs."""
        from .bridge.scene import rt_attr, scene_path

        packet_id = str(job.get("packet_id") or "")
        if not packet_id:
            return self._unchecked(packet_id, "the job names no packet")
        if "/" in packet_id or "\\" in packet_id or packet_id in (".", ".."):
            return self._unchecked(packet_id, "the packet id is not a folder name")
        root = packets_dir(dict(cfg))
        try:
            manifest = json.loads((root / packet_id / "manifest.json").read_text(encoding="utf-8-sig"))
        except OSError:
            return self._unchecked(
                packet_id, f"its manifest.json is not in this dock's packets folder {root}"
            )
        except ValueError:
            return self._unchecked(packet_id, "its manifest.json is not readable JSON")
        scene_hash = manifest.get("scene_hash") if isinstance(manifest, dict) else None
        max_file = scene_hash.get("max_file") if isinstance(scene_hash, dict) else None
        wanted = max_file.get("name") if isinstance(max_file, dict) else None
        if not isinstance(wanted, str) or not wanted:
            return self._unchecked(packet_id, "it was sent from an unsaved scene: its manifest names no .max")
        open_now = str(rt_attr("maxFileName") or "")
        if open_now.casefold() != wanted.casefold():
            shown = open_now or "an unsaved scene"
            return f"the open scene is {shown}, but packet {packet_id} came from {wanted}: open it and run the node again"
        return self._project_mismatch(job, cfg, scene_path(), open_now)

    def _unchecked(self, packet_id: str, why: str) -> None:
        self._log(f"packet {packet_id or '?'}: scene check skipped ({why})")
        return None

    def _project_mismatch(
        self, job: Mapping[str, Any], cfg: Mapping[str, Any], scene: str, name: str
    ) -> str | None:
        """Why the open scene's project (resolve_project_id: its sidecar, config.json's project_id, its
        stem) is not the job's, or None. A binding that cannot be read refuses the job too: the dock
        never guesses which project a scene belongs to."""
        from .ui.controller import resolve_project_id

        wanted = job.get("project_id")
        if not isinstance(wanted, str) or not wanted:
            return None  # the daemon always names it; an older one may not: the file name matched
        try:
            bound = resolve_project_id(scene or None, cfg)
        except Exception as exc:  # noqa: BLE001 an unreadable sidecar, a stem that names no project
            return f"cannot tell which project the open scene {name} belongs to ({exc}): fix it and run the node again"
        if bound != wanted:
            return (
                f"the open scene {name} is bound to project {bound}, but the job is for project {wanted}: "
                "open that project's scene (or bind this one in the dock) and run the node again"
            )
        return None

    def _note_packet(self, camera: str, packet_id: Any) -> None:
        note = getattr(self.controller, "note_packet", None)
        if callable(note) and packet_id:
            try:
                note(camera, str(packet_id))
            except Exception:  # noqa: BLE001 bookkeeping only
                pass

    def _light_match(self, job: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        from .actions import light
        from .bridge.scene import get_camera

        camera = str(job.get("camera") or "")
        if not camera:
            raise JobFailed("the job names no camera")
        ok, why = light.available()
        if not ok:
            raise JobFailed(why)
        mismatch = self._scene_mismatch(job, cfg)
        if mismatch:
            raise JobFailed(mismatch)
        if get_camera(camera) is None:
            raise JobFailed(f"camera {camera!r} is not in the open scene")
        reference = self.fetch_reference(str(job.get("reference_hash") or ""), cfg, job.get("project_id"))
        supersedes = str(job.get("supersedes") or job.get("packet_id") or "") or None
        locks = [str(k) for k in job.get("locks") or []]
        self._log(f"light.match {camera}: matching the light to {reference.name}")
        opts = self._send_options([camera], job, cfg)
        opts.update(
            reference_path=str(reference),
            locks=locks or None,
            deep=bool(job.get("deep")),
            log=lambda message: self._log(str(message)),
        )
        if supersedes:
            opts["supersedes"] = {camera: supersedes}
        cfg_url = str(cfg.get("daemon_url") or DEFAULT_DAEMON_URL)
        [result] = light.run([camera], packets_dir(cfg), cfg_url, opts)
        metrics = match_metrics(result.match)
        if not result.ok:
            reason = "; ".join(result.errors) or "no reason given"
            return {
                "ok": False,
                "detail": f"{result.code or result.stage}: {reason}",
                "metrics": metrics or None,
            }
        self._note_packet(camera, result.packet_id)
        notes = [f"supersedes {result.supersedes}"] if result.supersedes else []
        ingest = (result.send or {}).get("ingest")
        if ingest:
            notes.append(f"ingest {ingest}")
        notes.extend(str(w) for w in result.warnings)
        return {
            "ok": True,
            "packet_id": result.packet_id,
            "metrics": metrics or None,
            "detail": "; ".join(notes) or None,
        }

    def _vantage_preview(self, job: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        from .actions import light

        camera = str(job.get("camera") or "")
        if not camera:
            raise JobFailed("the job names no camera")
        answer = light.vantage_preview(camera)
        warnings = [str(w) for w in answer.get("warnings") or []]
        if not answer.get("ok"):
            return _fail(f"{answer.get('code') or 'LIVE_LINK_FAILED'}: {answer.get('error')}")
        return {"ok": True, "detail": "; ".join([str(answer.get("how") or "live link started"), *warnings])}

    def _scout(self, job: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        from .actions import scout

        mismatch = self._scene_mismatch(job, cfg)
        if mismatch:
            raise JobFailed(mismatch)
        try:
            count = int(job.get("count") or SCOUT_DEFAULT_COUNT)
        except (TypeError, ValueError) as exc:
            raise JobFailed(f"count {job.get('count')!r} is not a whole number") from exc
        count = max(1, min(SCOUT_MAX_COUNT, count))
        keep = bool(job.get("keep"))
        opts = self._send_options([], job, cfg)
        opts.update(views=list(range(count)), keep=keep, packets=keep)
        cfg_url = str(cfg.get("daemon_url") or DEFAULT_DAEMON_URL)
        results = scout.run(packets_dir(cfg), cfg_url, opts)
        views = [r.as_dict() if hasattr(r, "as_dict") else dict(r) for r in results]
        poses = [v for v in views if int(v.get("view_id", -1)) >= 0 and v.get("ok")]
        packets = [str(v["packet_id"]) for v in poses if v.get("packet_id")]
        failed = [v for v in views if not v.get("ok")]
        detail = "; ".join(
            f"{v.get('camera') or v.get('label')}: {'; '.join(_errors(v)) or v.get('code')}" for v in failed
        )
        if not poses:
            return _fail(detail or "the scout placed no camera")
        if keep:
            self._note_scout(poses)
        return {"ok": True, "poses": poses, "packets": packets or None, "detail": detail or None}

    def _note_scout(self, poses: list[dict[str, Any]]) -> None:
        for view in poses:
            if view.get("packet_id"):
                self._note_packet(str(view.get("camera")), view["packet_id"])

    def _receive(self, job: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        from .actions import receive

        run_id = str(job.get("run_id") or "")
        if not run_id:
            raise JobFailed("the job names no run")
        mode = str(job.get("mode") or DEFAULT_RECEIVE_MODE)
        if mode not in receive.MODES:
            raise JobFailed(f"unknown receive mode {mode!r}: use {' or '.join(receive.MODES)}")
        path = f"/api/v0/runs/{urllib.parse.quote(run_id, safe='')}"
        try:
            run = self._call(lambda: transport.get_json(path, cfg, RUN_TIMEOUT_S))
        except transport.TransportError as exc:
            raise JobFailed(f"run {run_id}: {exc}") from exc
        outputs = run.get("outputs") if isinstance(run, Mapping) else None
        if not isinstance(outputs, list) or not outputs:
            raise JobFailed(f"run {run_id} has no take to show")
        project_id = job.get("project_id") or (run.get("project_id") if isinstance(run, Mapping) else None)
        take = self.fetch_take(str(outputs[0]), cfg, project_id)
        self._log(f"receive {run_id}: {take.name} into {mode}")
        ok, detail = receive.load_result(take, mode)
        return {"ok": bool(ok), "detail": str(detail)}

    # ------------------------------------------------------------------ hooks
    def _hook(self, fn: Callable[..., Any] | None, *args: Any) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 the dock's bookkeeping never stops a job
            self._log(f"job poller hook: {type(exc).__name__}: {exc}", err=True)
