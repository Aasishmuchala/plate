"""sse.py and GET /api/v0/events: durable events, Last-Event-ID replay, live follow, heartbeat."""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

from plate_daemon.index import Index
from plate_daemon.sse import HEARTBEAT_S, Bus, EventFilter, UnknownProject


class SseReader:
    """Reads one `text/event-stream` response frame by frame (comments kept for the heartbeat test)."""

    def __init__(self, response: httpx.Response):
        self.lines = response.iter_lines()

    def frame(self) -> dict:
        frame: dict = {}
        for line in self.lines:
            if line == "":
                if frame:
                    return frame
                continue
            if line.startswith(":"):
                frame.setdefault("comments", []).append(line[1:].strip())
                continue
            field, _, value = line.partition(":")
            frame[field] = value[1:] if value.startswith(" ") else value
        raise AssertionError("stream ended")

    def event(self) -> dict:
        while True:
            f = self.frame()
            if "data" in f:
                return {"id": int(f["id"]), "rec": json.loads(f["data"])}


def test_publish_stores_events_with_increasing_seq(settings):
    bus = Bus(Index(settings.home))
    a = bus.publish(None, {"kind": "graph.saved"}, project_id="proj")
    b = bus.publish("run_a", {"kind": "run.state", "to": "QUEUED"}, project_id="proj")
    assert b > a >= 1
    recs = bus.read(EventFilter("proj"), after=0)
    assert [(r["seq"], r["run_id"], r["kind"]) for r in recs] == [
        (a, None, "graph.saved"),
        (b, "run_a", "run.state"),
    ]
    assert recs[1]["data"] == {"kind": "run.state", "to": "QUEUED"} and recs[1]["at"].endswith("Z")
    assert bus.heartbeat_s == HEARTBEAT_S == 15.0


def test_project_must_be_known_when_ambiguous(settings):
    bus = Bus(Index(settings.home))
    bus.publish(None, {"kind": "x"}, project_id="one")
    assert bus.resolve_project() == "one"
    bus.publish(None, {"kind": "x"}, project_id="two")
    with pytest.raises(UnknownProject):
        bus.publish(None, {"kind": "x"})
    with pytest.raises(UnknownProject):
        bus.resolve_project(run_id="run_nowhere")


def test_subscribe_replays_then_wakes_on_publish_from_another_thread(settings):
    # poll_s is far longer than the wait below, so the follow-up event can only arrive by the wake-up.
    bus = Bus(Index(settings.home), poll_s=30.0)
    first = bus.publish("run_a", {"kind": "one"}, project_id="proj")
    second = bus.publish("run_a", {"kind": "two"}, project_id="proj")
    bus.publish("run_b", {"kind": "other run"}, project_id="proj")

    async def main() -> list[dict]:
        it = bus.subscribe(EventFilter("proj", run_id="run_a", after=first))
        got = [await asyncio.wait_for(anext(it), 5)]
        threading.Timer(
            0.2, bus.publish, args=("run_a", {"kind": "three"}), kwargs={"project_id": "proj"}
        ).start()
        got.append(await asyncio.wait_for(anext(it), 5))
        await it.aclose()
        return got

    got = asyncio.run(main())
    assert got[0]["seq"] == second and got[0]["kind"] == "two"
    assert got[1]["kind"] == "three" and got[1]["seq"] > second
    assert bus._waiters == set()  # the subscriber unregistered when the consumer stopped


def test_http_replay_by_last_event_id_then_live(settings, live_server):
    bus = Bus(Index(settings.home), poll_s=0.05)
    seqs = [bus.publish("run_a", {"kind": "run.state", "n": n}, project_id="proj") for n in range(3)]
    bus.publish("run_b", {"kind": "run.state", "n": 99}, project_id="proj")
    with live_server(bus=bus) as (s, url):
        q = f"{url}/api/v0/events?project_id=proj&run_id=run_a&token={s.token}"
        with httpx.Client(timeout=10) as c, c.stream("GET", q, headers={"Last-Event-ID": str(seqs[0])}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            reader = SseReader(r)
            replayed = [reader.event(), reader.event()]
            assert [e["id"] for e in replayed] == seqs[1:]
            assert [e["rec"]["data"]["n"] for e in replayed] == [1, 2]
            assert all(e["rec"]["run_id"] == "run_a" and e["rec"]["project_id"] == "proj" for e in replayed)
            live_seq = bus.publish("run_a", {"kind": "run.state", "n": 3}, project_id="proj")
            live = reader.event()
            assert live["id"] == live_seq and live["rec"]["data"]["n"] == 3


def test_http_events_bearer_header_and_query_after(settings, live_server):
    bus = Bus(Index(settings.home), poll_s=0.05)
    seqs = [bus.publish(None, {"kind": "k", "n": n}, project_id="proj") for n in range(3)]
    with live_server(bus=bus) as (s, url):
        hdr = {"Authorization": f"Bearer {s.token}"}
        with httpx.Client(timeout=10) as c:
            with c.stream("GET", f"{url}/api/v0/events?project_id=proj&after={seqs[1]}", headers=hdr) as r:
                assert r.status_code == 200
                assert SseReader(r).event()["id"] == seqs[2]
            assert c.get(f"{url}/api/v0/events?project_id=proj").status_code == 401
            assert c.get(f"{url}/api/v0/events?project_id=absent", headers=hdr).status_code == 404


def test_http_heartbeat_comment(settings, live_server):
    bus = Bus(Index(settings.home), heartbeat_s=0.2, poll_s=0.05)
    bus.publish(None, {"kind": "k"}, project_id="proj")
    with live_server(bus=bus) as (s, url):
        q = f"{url}/api/v0/events?project_id=proj&token={s.token}"
        with httpx.Client(timeout=10) as c, c.stream("GET", q, headers={"Last-Event-ID": "1"}) as r:
            frame = SseReader(r).frame()  # nothing to replay after seq 1: the first frame is a ping
            assert any(cm.startswith("ping") for cm in frame.get("comments", [])), frame
