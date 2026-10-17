"""FakeEngine: a scripted, deterministic provider for the state machine, crash and API tests.

`FakeEngine(seed=0, behaviour="ok")` implements the `adapter.EngineAdapter` protocol with no network and no
sleeping. `behaviour` scripts the provider: `ok`; `fail_submit` (submit raises `SubmitRejected` and no job
exists); `refuse` (poll -> refused); `slow` (a job needs `SLOW_POLLS` polls); `expire_url` (fetch raises
ResultExpired). The older keyword flags still work: `refuse`, `timeout` (poll never succeeds),
`expire_result_url`, `polls_until_done` (read at poll time, so a restarted daemon can finish a job the
previous one left running), `supports_lookup` (adopt a lost job id by idempotency key),
`crash_after_submit_before_persist` (the provider accepted the job, then the daemon died before writing
the id), `crash_at` + `crash_phase` (raise `SimulatedCrash` inside the worker step that would leave
`crash_at`: "before" it does anything, "after_effect" once its provider call or CAS write is done but before
the commit, "after_persist" once the provider id is persisted but before the SUBMITTED commit, or
"after_commit") and `hang_at` + `hang_phase` (the same points, but the process blocks after writing a
`<journal>.hang` marker, so a test can kill a real process exactly there).

A result is a PNG (`size`, default 64 x 64, RGB) whose pixels are BLAKE3 output over the seed, the hash of
the payload's inputs, the hash of its params and its `variant` (the sibling index of a best-of-N run): the
same request always gives the same pixels and a different input gives different ones. `output="text"`
keeps the seed's one-line `PLATE-FAKE-RESULT <job> <key>` body for tests written against it. The provider
dedupes on the idempotency key (`idempotent_submit`), as the plan requires of real adapters that claim it.

`echo=True` makes the result the PNG the submit payload carried (`inputs.image.b64`, decoded), so the gates
score real pixels instead of noise; `shift_px` then moves it that many pixels to the right with edge
replication (a negative value moves it left), which is how a test or a demo makes a camera-drift GATE_FAIL.
A payload that carried no image has nothing to echo and gets the deterministic noise. The echoed bytes are
held in memory only (the newest `ECHO_KEEP` jobs), never in the journal: a restarted daemon echoes again once
the pipeline re-submits, and a fetch for a job whose image was lost raises `ResultExpired`.

Jobs, the call counters and the call log (`calls`: one `{op, job, key}` per submit, poll, fetch, cancel and
lookup) persist to a JSON `journal` with atomic replaces, so a daemon killed mid-run restarts against the
same provider state: that is what makes "exactly one submit" across a real process restart an honest
assertion. `PLATE_FAKE_ENGINE` configures the daemon's in-process instance: a JSON object of these flags, or
`1` (also `true`, `yes`, `on`) for the echoing fake (`{"echo": true}`); unset, empty, `0` or `false` mean the
defaults.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import struct
import threading
import time
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from blake3 import blake3

from .adapter import PollResult, ResultExpired, SimulatedCrash
from .fsutil import read_json, write_json_atomic
from .idempotency import canonical_json
from .states import RunState

Behaviour = Literal["ok", "fail_submit", "refuse", "slow", "expire_url"]
BEHAVIOURS = ("ok", "fail_submit", "refuse", "slow", "expire_url")
CRASH_PHASES = ("before", "after_effect", "after_persist", "after_commit")
RESULT_SCHEME = "fake://results/"
SLOW_POLLS = 6
DEFAULT_SIZE = (64, 64)
ECHO_KEEP = 32
TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"", "0", "false", "no", "off"})
FLAG_NAMES = (
    "seed",
    "behaviour",
    "crash_at",
    "crash_phase",
    "hang_at",
    "hang_phase",
    "crash_after_submit_before_persist",
    "refuse",
    "timeout",
    "expire_result_url",
    "polls_until_done",
    "supports_lookup",
    "size",
    "output",
    "echo",
    "shift_px",
)


class SubmitRejected(RuntimeError):
    """The fake provider rejected the request outright (behaviour "fail_submit")."""


def _empty_state() -> dict[str, Any]:
    return {
        "submit_count": 0,
        "poll_count": 0,
        "fetch_count": 0,
        "cancel_count": 0,
        "lookup_count": 0,
        "next_job": 0,
        "jobs": {},
        "by_key": {},
        "calls": [],
    }


def png_rgb(width: int, height: int, rgb: bytes) -> bytes:
    """A minimal 8-bit RGB PNG (stdlib only, byte-identical for identical pixels)."""
    if len(rgb) != width * height * 3:
        raise ValueError("rgb must hold width * height * 3 bytes")
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def payload_digest(payload: dict[str, Any]) -> tuple[str, str, int]:
    """(inputs hash, params hash, variant) of a provider payload."""
    inputs_hash = blake3(canonical_json(payload.get("inputs") or {})).hexdigest()
    params_hash = blake3(canonical_json(payload.get("params") or {})).hexdigest()
    variant = int(payload.get("variant") or 0)
    return inputs_hash, params_hash, variant


def payload_image(payload: dict[str, Any]) -> bytes | None:
    """The bytes of `inputs.image.b64` in a provider payload, or None when it carries no decodable image."""
    image = (payload.get("inputs") or {}).get("image")
    b64 = image.get("b64") if isinstance(image, dict) else None
    if not isinstance(b64, str) or not b64:
        return None
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


def shift_right(png: bytes, shift_px: int) -> bytes:
    """`png` moved `shift_px` pixels to the right (left when negative), the uncovered columns filled by
    replicating the edge column. Zero returns the bytes untouched; the dtype and channels are kept."""
    if not shift_px:
        return png
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("echo: the submitted image is not a decodable PNG")
    w = img.shape[1]
    k = min(abs(int(shift_px)), w)
    if shift_px > 0:
        out = np.concatenate([np.repeat(img[:, :1], k, axis=1), img[:, : w - k]], axis=1)
    else:
        out = np.concatenate([img[:, k:], np.repeat(img[:, -1:], k, axis=1)], axis=1)
    ok, enc = cv2.imencode(".png", out)
    if not ok:
        raise ValueError("echo: could not re-encode the shifted image")
    return bytes(enc)


def render(
    seed: int, inputs_hash: str, params_hash: str, variant: int, size: tuple[int, int] = DEFAULT_SIZE
) -> bytes:
    w, h = size
    pixels = blake3(f"plate-fake|{seed}|{inputs_hash}|{params_hash}|{variant}".encode()).digest(
        length=w * h * 3
    )
    return png_rgb(w, h, pixels)


class FakeEngine:
    stub = True
    engine_id = "fake"
    idempotent_submit = True

    def __init__(
        self,
        seed: int = 0,
        behaviour: Behaviour = "ok",
        *,
        crash_at: RunState | str | None = None,
        crash_phase: str = "before",
        hang_at: RunState | str | None = None,
        hang_phase: str = "before",
        crash_after_submit_before_persist: bool = False,
        refuse: bool = False,
        timeout: bool = False,
        expire_result_url: bool = False,
        polls_until_done: int = 2,
        supports_lookup: bool = False,
        size: tuple[int, int] | list[int] = DEFAULT_SIZE,
        output: Literal["png", "text"] = "png",
        echo: bool = False,
        shift_px: int = 0,
        journal: Path | None = None,
    ):
        if behaviour not in BEHAVIOURS:
            raise ValueError(f"behaviour must be one of {BEHAVIOURS}")
        if crash_phase not in CRASH_PHASES or hang_phase not in CRASH_PHASES:
            raise ValueError(f"crash_phase and hang_phase must be one of {CRASH_PHASES}")
        if output not in ("png", "text"):
            raise ValueError("output must be 'png' or 'text'")
        if polls_until_done < 1:
            raise ValueError("polls_until_done must be >= 1")
        w, h = (int(x) for x in size)
        if w < 1 or h < 1:
            raise ValueError("size must be at least 1 x 1")
        self.seed = int(seed)
        self.behaviour = behaviour
        self.crash_at = RunState(crash_at) if crash_at is not None else None
        self.crash_phase = crash_phase
        self.hang_at = RunState(hang_at) if hang_at is not None else None
        self.hang_phase = hang_phase
        self.crash_after_submit_before_persist = crash_after_submit_before_persist
        self.refuse = refuse or behaviour == "refuse"
        self.timeout = timeout
        self.expire_result_url = expire_result_url or behaviour == "expire_url"
        self.fail_submit = behaviour == "fail_submit"
        self.polls_until_done = max(polls_until_done, SLOW_POLLS) if behaviour == "slow" else polls_until_done
        self.supports_lookup = supports_lookup
        self.size = (w, h)
        self.output = output
        self.echo = bool(echo)
        self.shift_px = int(shift_px)
        self.journal = Path(journal) if journal is not None else None
        self._lock = threading.RLock()
        self._echo_src: OrderedDict[str, bytes] = OrderedDict()
        self._crashed = False
        self._state = _empty_state()
        if self.journal is not None and self.journal.exists():
            self._state.update(read_json(self.journal))

    # ------------------------------------------------------------------ construction helpers

    @classmethod
    def from_env(cls, journal: Path | None, env_var: str = "PLATE_FAKE_ENGINE") -> FakeEngine:
        raw = os.environ.get(env_var, "").strip()
        word = raw.lower()
        if word in TRUE_WORDS:
            flags: Any = {"echo": True}
        elif word in FALSE_WORDS:
            flags = {}
        else:
            try:
                flags = json.loads(raw)
            except ValueError as e:
                raise ValueError(f"{env_var} must be 1, 0 or a JSON object of FakeEngine flags ({e})") from e
        if not isinstance(flags, dict):
            raise ValueError(f"{env_var} must be 1, 0 or a JSON object of FakeEngine flags")
        unknown = set(flags) - set(FLAG_NAMES)
        if unknown:
            raise ValueError(f"{env_var}: unknown FakeEngine flags {sorted(unknown)}")
        return cls(journal=journal, **flags)

    def restarted(self, **flags: Any) -> FakeEngine:
        """A fresh instance over the same provider state, as a restarted daemon would see it. The seed,
        behaviour and size carry over unless overridden; scripted crashes and hangs do not."""
        flags.setdefault("seed", self.seed)
        flags.setdefault("behaviour", self.behaviour)
        flags.setdefault("size", self.size)
        flags.setdefault("output", self.output)
        flags.setdefault("echo", self.echo)
        flags.setdefault("shift_px", self.shift_px)
        new = FakeEngine(journal=self.journal, **flags)
        if self.journal is None:
            with self._lock:
                new._state = copy.deepcopy(self._state)
        return new

    @property
    def hang_marker(self) -> Path | None:
        return None if self.journal is None else self.journal.with_name(self.journal.name + ".hang")

    # ------------------------------------------------------------------ persistence

    def _log(self, op: str, job: str | None = None, key: str | None = None) -> None:
        self._state["calls"].append({"op": op, "job": job, "key": key})

    def _save(self) -> None:
        if self.journal is not None:
            write_json_atomic(self.journal, self._state, indent=1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    @property
    def calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._state["calls"])

    def call_count(self, op: str) -> int:
        with self._lock:
            return sum(1 for c in self._state["calls"] if c["op"] == op)

    @property
    def submit_count(self) -> int:
        return self._state["submit_count"]

    @property
    def poll_count(self) -> int:
        return self._state["poll_count"]

    @property
    def fetch_count(self) -> int:
        return self._state["fetch_count"]

    @property
    def cancel_count(self) -> int:
        return self._state["cancel_count"]

    @property
    def job_count(self) -> int:
        return len(self._state["jobs"])

    # ------------------------------------------------------------------ fault injection

    def fault_point(self, state: RunState, phase: str) -> None:
        """Called by the worker at each phase of a step; raises (crash) or blocks (hang) once when scripted."""
        if self.hang_at is not None and state == self.hang_at and phase == self.hang_phase:
            marker = self.hang_marker
            if marker is not None:
                write_json_atomic(marker, {"state": state.value, "phase": phase, "pid": os.getpid()})
            while True:  # the test kills this process here
                time.sleep(3600)
        if (
            self.crash_at is not None
            and not self._crashed
            and state == self.crash_at
            and phase == self.crash_phase
        ):
            self._crashed = True
            raise SimulatedCrash(f"simulated crash leaving {state.value} ({phase})")

    # ------------------------------------------------------------------ EngineAdapter

    def render(self, payload: dict[str, Any]) -> bytes:
        """The PNG this engine returns for `payload` (what fetch serves)."""
        return render(self.seed, *payload_digest(payload), size=self.size)

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        with self._lock:
            st = self._state
            st["submit_count"] += 1
            if self.fail_submit:
                self._log("submit", None, idempotency_key)
                self._save()
                raise SubmitRejected("fake provider rejected the request (HTTP 400)")
            job_id = st["by_key"].get(idempotency_key)
            if job_id is None:
                st["next_job"] += 1
                job_id = f"fake-{st['next_job']:06d}"
                inputs_hash, params_hash, variant = payload_digest(payload)
                st["jobs"][job_id] = {
                    "idempotency_key": idempotency_key,
                    "payload_bytes": len(canonical_json(payload)),
                    "inputs_hash": inputs_hash,
                    "params_hash": params_hash,
                    "variant": variant,
                    "has_image": payload_image(payload) is not None,
                    "polls": 0,
                    "cancelled": False,
                }
                st["by_key"][idempotency_key] = job_id
            self._log("submit", job_id, idempotency_key)
            self._save()
            self._keep_echo_source(job_id, payload)
        if self.crash_after_submit_before_persist and not self._crashed:
            self._crashed = True
            raise SimulatedCrash(
                "simulated crash after the provider accepted the job, before the id was persisted"
            )
        return job_id

    def _keep_echo_source(self, job_id: str, payload: dict[str, Any]) -> None:
        """Hold the PNG a submit carried so `fetch` can hand it back. Every submit does this, deduped ones
        included: a restarted daemon that re-submits the same key refills what it lost."""
        data = payload_image(payload) if self.echo else None
        if data is None:
            return
        self._echo_src[job_id] = data
        self._echo_src.move_to_end(job_id)
        while len(self._echo_src) > ECHO_KEEP:
            self._echo_src.popitem(last=False)

    def poll(self, provider_job_id: str) -> PollResult:
        with self._lock:
            st = self._state
            st["poll_count"] += 1
            self._log("poll", provider_job_id)
            job = st["jobs"].get(provider_job_id)
            if job is None:
                self._save()
                return PollResult("failed", detail=f"unknown job {provider_job_id}")
            job["polls"] += 1
            self._save()
            if job["cancelled"]:
                return PollResult("failed", detail="cancelled")
            if self.refuse:
                return PollResult("refused", detail="fake moderation refusal")
            if self.timeout or job["polls"] < self.polls_until_done:
                return PollResult("running")
            return PollResult("succeeded", result_url=f"{RESULT_SCHEME}{provider_job_id}")

    def fetch(self, result_url: str) -> bytes:
        with self._lock:
            self._state["fetch_count"] += 1
            job_id = result_url[len(RESULT_SCHEME) :] if result_url.startswith(RESULT_SCHEME) else None
            self._log("fetch", job_id)
            self._save()
            if self.expire_result_url:
                raise ResultExpired(f"{result_url} expired")
            if job_id is None:
                raise ResultExpired(f"{result_url} is not a fake result URL")
            job = self._state["jobs"].get(job_id)
            if job is None:
                raise ResultExpired(f"{result_url}: no such job")
            if self.output == "text":
                return f"PLATE-FAKE-RESULT {job_id} {job['idempotency_key']}\n".encode()
            if self.echo and job.get("has_image"):
                sent = self._echo_src.get(job_id)
                if sent is None:
                    raise ResultExpired(f"{result_url}: the image to echo is gone (resubmit the job)")
                return shift_right(sent, self.shift_px)
            return render(
                self.seed,
                job.get("inputs_hash", ""),
                job.get("params_hash", ""),
                int(job.get("variant", 0)),
                size=self.size,
            )

    def cancel(self, provider_job_id: str) -> None:
        with self._lock:
            self._state["cancel_count"] += 1
            self._log("cancel", provider_job_id)
            job = self._state["jobs"].get(provider_job_id)
            if job is not None:
                job["cancelled"] = True
            self._save()

    def lookup(self, idempotency_key: str) -> str | None:
        if not self.supports_lookup:
            raise NotImplementedError("this FakeEngine was built with supports_lookup=False")
        with self._lock:
            self._state["lookup_count"] += 1
            found = self._state["by_key"].get(idempotency_key)
            self._log("lookup", found, idempotency_key)
            self._save()
            return found
