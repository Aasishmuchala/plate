"""An in-process STAFFAGE for the People node tests (router plan T45).

`FakeStaffage` is a FastAPI app with the routes `staffage_client.StaffageClient` calls, answering the way the real
Next.js routes do (same paths, same JSON, same 400 / 404 / 409 rules, files handed over by path and refused
outside `home`, as `assertUnderHome` does). A person job paints a coloured rectangle (the person), an optional
darker ellipse (her shadow) and writes the plate and the matte as stills, or as video when `video=True`.

    fake = FakeStaffage(home, person_box=(x0, y0, x1, y1))
    client = StaffageClient(transport=fake.transport())

`fake.calls` lists every request as (method, path, json body), `fake.received` the still each job was started
from, so a test can prove what left (and that nothing did, on a refused run). Nothing here touches the network:
`AsgiSyncTransport` runs the app inside the process, one `asyncio.run` per request.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PERSON_COLOUR_RGB = (205, 40, 45)
SHADOW_FACTOR = 0.55
STAGES = ("generating", "downloading", "matting")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AsgiSyncTransport(httpx.BaseTransport):
    """A synchronous httpx transport over an ASGI app (httpx.ASGITransport is async only)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        return asyncio.run(self._call(request))

    async def _call(self, request: httpx.Request) -> httpx.Response:
        response = await httpx.ASGITransport(app=self.app).handle_async_request(request)
        content = await response.aread()
        headers = [(k, v) for k, v in response.headers.raw if k.lower() != b"content-length"]
        return httpx.Response(response.status_code, headers=headers, content=content, request=request)


class FakeStaffage:
    def __init__(
        self,
        home: Path,
        *,
        person_box: tuple[int, int, int, int] = (10, 10, 20, 40),
        shadow_box: tuple[int, int, int, int] | None = None,
        polls_before_done: int = 1,
        out_scale: float = 1.0,
        video: bool = False,
        fail_with: str | None = None,
        casts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.home = Path(home).resolve()
        self.work = self.home / "staffage-workspace"
        self.work.mkdir(parents=True, exist_ok=True)
        self.person_box = person_box
        self.shadow_box = shadow_box
        self.polls_before_done = polls_before_done
        self.out_scale = out_scale
        self.video = video
        self.fail_with = fail_with
        self.casts = casts or []
        self.calls: list[tuple[str, str, Any]] = []
        self.films: dict[str, dict[str, Any]] = {}
        self.plates: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.received: list[dict[str, Any]] = []
        self.takes: list[dict[str, Any]] = []
        self._polls: dict[str, int] = {}
        self.app = self._build()

    # -- helpers -----------------------------------------------------------------------------------------

    def transport(self) -> AsgiSyncTransport:
        return AsgiSyncTransport(self.app)

    def calls_to(self, method: str, path_prefix: str) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == method and c[1].startswith(path_prefix)]

    def _under_home(self, raw: str) -> Path:
        p = Path(raw).resolve()
        if not p.is_relative_to(self.home):
            raise PermissionError(f"path outside home: {p.name}")
        return p

    def _paint(self, still: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(person plate RGB, matte) at the still's size: the person rectangle, her shadow darkened in."""
        plate = still.copy()
        h, w = plate.shape[:2]
        if self.shadow_box is not None:
            x0, y0, x1, y1 = self.shadow_box
            layer = np.zeros((h, w), np.uint8)
            cv2.ellipse(
                layer, ((x0 + x1) // 2, (y0 + y1) // 2), ((x1 - x0) // 2, (y1 - y0) // 2), 0, 0, 360, 255, -1
            )
            layer = cv2.GaussianBlur(layer, (0, 0), 1.5).astype(np.float32) / 255.0
            plate = (plate.astype(np.float32) * (1.0 - layer[..., None] * (1.0 - SHADOW_FACTOR))).astype(
                np.uint8
            )
        x0, y0, x1, y1 = self.person_box
        matte = np.zeros((h, w), np.float32)
        matte[y0:y1, x0:x1] = 1.0
        matte = cv2.GaussianBlur(matte, (0, 0), 0.7)
        matte[matte < 0.01] = 0.0
        person = np.zeros_like(plate)
        person[:] = PERSON_COLOUR_RGB
        plate = (plate.astype(np.float32) * (1 - matte[..., None]) + person * matte[..., None]).astype(
            np.uint8
        )
        return plate, matte

    def _write_result(self, job_id: str, still_path: Path) -> tuple[str, str]:
        raw = cv2.imdecode(np.fromfile(str(still_path), np.uint8), cv2.IMREAD_COLOR)
        still = np.ascontiguousarray(raw[..., ::-1])
        plate, matte = self._paint(still)
        if self.out_scale != 1.0:
            size = (round(plate.shape[1] * self.out_scale), round(plate.shape[0] * self.out_scale))
            plate = cv2.resize(plate, size, interpolation=cv2.INTER_AREA)
            matte = cv2.resize(matte, size, interpolation=cv2.INTER_AREA)
        pha = np.clip(np.rint(matte * 255.0), 0, 255).astype(np.uint8)
        if self.video:
            plate_path, matte_path = (
                self.work / f"person_{job_id}.mp4",
                self.work / f"person_{job_id}_pha.mp4",
            )
            for path, frame in (
                (plate_path, plate[..., ::-1]),
                (matte_path, np.repeat(pha[..., None], 3, axis=2)),
            ):
                writer = cv2.VideoWriter(
                    str(path), cv2.VideoWriter_fourcc(*"mp4v"), 24, (frame.shape[1], frame.shape[0])
                )
                for _ in range(6):
                    writer.write(np.ascontiguousarray(frame))
                writer.release()
        else:
            plate_path, matte_path = (
                self.work / f"person_{job_id}.png",
                self.work / f"person_{job_id}_pha.png",
            )
            ok, buf = cv2.imencode(".png", np.ascontiguousarray(plate[..., ::-1]))
            plate_path.write_bytes(bytes(buf))
            ok, buf = cv2.imencode(".png", pha)
            matte_path.write_bytes(bytes(buf))
        return str(plate_path), str(matte_path)

    # -- the app -----------------------------------------------------------------------------------------

    def _build(self) -> FastAPI:
        app = FastAPI()

        def err(message: str, status: int) -> JSONResponse:
            return JSONResponse({"error": message}, status_code=status)

        async def body_of(request: Request) -> Any:
            try:
                raw = await request.body()
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
            self.calls.append((request.method, request.url.path, body))
            return body

        @app.get("/api/films")
        async def list_films(request: Request):
            await body_of(request)
            return list(self.films.values())

        @app.post("/api/films")
        async def create_film(request: Request):
            b = await body_of(request)
            name = (b or {}).get("name", "").strip() if isinstance(b, dict) else ""
            if not name:
                return err("name is required", 400)
            film = {
                "id": str(uuid.uuid4()),
                "name": name,
                "cast": [dict(c) for c in self.casts],
                "shots": [],
                "createdAt": _now(),
            }
            self.films[film["id"]] = film
            return JSONResponse(film, status_code=201)

        @app.get("/api/films/{film_id}")
        async def get_film(film_id: str, request: Request):
            await body_of(request)
            film = self.films.get(film_id)
            return JSONResponse(film) if film else err("film not found", 404)

        @app.put("/api/films/{film_id}")
        async def put_film(film_id: str, request: Request):
            b = await body_of(request)
            if film_id not in self.films:
                return err("film not found", 404)
            if not isinstance(b, dict) or b.get("id") != film_id:
                return err("body must be the full film with matching id", 400)
            self.films[film_id] = b
            return b

        @app.post("/api/plates")
        async def add_plate(request: Request):
            b = await body_of(request)
            if not isinstance(b, dict) or not b.get("path") or not b.get("name"):
                return err("name and path required", 400)
            try:
                path = self._under_home(str(b["path"]))
            except PermissionError as e:
                return err(str(e), 500)
            plate = {
                "id": str(uuid.uuid4()),
                "name": str(b["name"]),
                "path": str(path),
                "lensMm": b.get("lensMm"),
                "cameraHeightM": b.get("cameraHeightM"),
                "lightDirection": b.get("lightDirection", ""),
                "createdAt": _now(),
            }
            self.plates.append(plate)
            return JSONResponse(plate, status_code=201)

        @app.post("/api/media/ingest")
        async def ingest(request: Request):
            b = await body_of(request)
            raw = (b or {}).get("path", "") if isinstance(b, dict) else ""
            if not raw:
                return err("path is required", 400)
            try:
                path = self._under_home(raw)
            except PermissionError as e:
                return err(str(e), 500)
            if not path.is_file():
                return err("file not found", 500)
            img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_UNCHANGED)
            h, w = img.shape[:2]
            meta = {"width": w, "height": h, "durationSec": 4.0, "fps": 24}
            return {"key": uuid.uuid4().hex, "meta": meta, "proxyPath": str(path), "thumbPath": None}

        @app.post("/api/person")
        async def start_person(request: Request):
            b = await body_of(request)
            if not isinstance(b, dict) or not b.get("filmId") or not b.get("shotId"):
                return err("filmId and shotId required", 400)
            film = self.films.get(b["filmId"])
            shot = next((s for s in (film or {}).get("shots", []) if s.get("id") == b["shotId"]), None)
            if film is None or shot is None:
                return err("project or shot not found", 404)
            if not shot.get("meta"):
                return err("shot not ingested yet - finish intake first", 409)
            mode = b.get("mode")
            if mode in ("i2v", "retake") and not b.get("stillPath"):
                return err(f"{mode} needs a chosen still", 400)
            prompt = b.get("prompt") or ""
            if not prompt:
                cast = next((c for c in film.get("cast", []) if c.get("id") == b.get("castId")), None)
                if not cast or not b.get("behavior") or not b.get("placement"):
                    return err("either a full prompt, or castId + behavior + placement", 400)
                prompt = f"{cast.get('role', '')} {b['behavior']} {b['placement']}"
            still_path = None
            if b.get("stillPath"):
                try:
                    still_path = self._under_home(str(b["stillPath"]))
                except PermissionError as e:
                    return err(str(e), 500)
                if not still_path.is_file():
                    return err("still not found", 500)
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "filmId": b["filmId"],
                "shotId": b["shotId"],
                "castId": b.get("castId"),
                "mode": mode,
                "model": b.get("model") or "fake/wan-2.7",
                "stillPath": str(still_path) if still_path else None,
                "prompt": prompt,
                "status": "preparing",
                "stage": "queued",
                "progress": 0.0,
                "error": None,
                "predictionId": None,
                "resultUrl": None,
                "platePath": None,
                "mattePath": None,
                "layerId": None,
                "estCostUsd": 0.4,
                "createdAt": _now(),
                "updatedAt": _now(),
            }
            self.jobs[job_id] = job
            self._polls[job_id] = 0
            self.received.append(
                {
                    "job": job_id,
                    "path": still_path,
                    "bytes": still_path.read_bytes() if still_path else b"",
                    "prompt": prompt,
                    "body": b,
                }
            )
            return {"job": job}

        @app.get("/api/person/{job_id}")
        async def get_person(job_id: str, request: Request):
            await body_of(request)
            job = self.jobs.get(job_id)
            if job is None:
                return err("job not found", 404)
            if job["status"] not in ("done", "error"):
                self._polls[job_id] += 1
                n = self._polls[job_id]
                if self.fail_with and n > self.polls_before_done:
                    job.update(status="error", error=self.fail_with, stage="failed")
                elif n > self.polls_before_done:
                    plate, matte = self._write_result(job_id, Path(job["stillPath"]))
                    job.update(status="done", stage="placed", progress=1.0, platePath=plate, mattePath=matte)
                else:
                    job.update(status=STAGES[(n - 1) % len(STAGES)], stage="working", progress=0.3)
                job["updatedAt"] = _now()
            return {"job": job}

        @app.post("/api/person/{job_id}")
        async def person_action(job_id: str, request: Request):
            b = await body_of(request)
            job = self.jobs.get(job_id)
            if job is None:
                return err("job not found", 404)
            if (b or {}).get("action") == "accept":
                self.takes.append({"kind": "decision", "jobId": job_id, "decision": "accepted"})
                return {"ok": True}
            return err("action must be accept or reroll", 400)

        @app.get("/api/takes")
        async def takes(request: Request):
            await body_of(request)
            return {"summary": {"accepted": len(self.takes)}, "recent": list(reversed(self.takes))}

        return app
