"""Wave 13 budgets fix (router plan T155, decision 89): externally declared import spend is outside the caps.

The attacker's repro: an import declared at 60 USD (`POST /projects/{id}/import`, `cost_usd 60`) counted as
today's spend, so with the default 50 USD daily cap every later auto and agent approval was a 409 `daily_cap`
for the rest of the UTC day: a value the caller types turned automation off. Now an `external` reservation
(route provider `external`) is left out of the daily and unattended caps, the 201 carries a `warnings` line
when the declaration is above the cap, and the ledger and `plate spend` still count the price.

The control: a 60 USD run on a vendor route is real spend and still trips `daily_cap`.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plate_daemon import budgets
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.db import tx
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.ledger import Ledger, summarize_records
from plate_daemon.machine import create_run, transition
from plate_daemon.models import QUOTE_TTL, Quote, RunCreate
from plate_daemon.quotes import insert_quote
from plate_daemon.states import RunState
from plate_daemon.timeutil import utcnow

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
S = RunState
WARNING_60 = (
    "declared cost 60.00 USD is above the daily cap 50.00 USD; it is recorded as external spend and "
    "does not count against the automation caps"
)

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings, policy: budgets.Policy | None = None) -> TestClient:
    c = TestClient(create_app(settings, FakeEngine()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    if policy is not None:
        c.app.state.pipeline.policy = policy
    return c


def _setup(c: TestClient, auth: dict) -> str:
    """Project + room_a ingested + Plate -> Edit `e` on the fake engine. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        {"id": "e", "kind": "Edit", "label": "e", "params": {"engine": "fake"}},
    ]
    edges = [{"id": "p->e", "source": "p", "target": "e"}]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _import(c: TestClient, auth: dict, packet_id: str, cost_usd: float, **over):
    body = {"path": str(ROOM_A / "display.tif"), "packet_id": packet_id, "source": "manual", **over}
    if cost_usd is not None:
        body["cost_usd"] = cost_usd
    return c.post(f"/api/v0/projects/{PROJECT}/import", json=body, headers=auth)


def _quote_edit(c: TestClient, auth: dict) -> str:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["quote"]["price_usd"] == pytest.approx(0.04)
    return r.json()["run"]["id"]


def _approve(c: TestClient, auth: dict, run_id: str, **body):
    return c.post(f"/api/v0/runs/{run_id}/approve", json={"start": False, **body}, headers=auth)


def _vendor_run(c: TestClient, packet_id: str, usd: float) -> str:
    """A quoted run on a vendor route (provider `vendor`, not `external`) priced `usd`."""
    now = utcnow()
    route = {
        "engine_id": "vendor-x",
        "provider": "vendor",
        "region": "us",
        "custody_grade": "C0",
        "custody_status": "confirmed",
    }
    index = c.app.state.index
    with index.project(PROJECT) as conn:
        spec = RunCreate(
            project_id=PROJECT,
            packet_id=packet_id,
            inputs={"plate": uuid.uuid4().hex},
            params={},
            route=route,
            attempt=1,
        )
        run = create_run(conn, spec, now=now)
        index.remember(run.id, PROJECT)
        q = Quote(
            run_id=run.id,
            provider="vendor",
            model="vendor-x",
            region="us",
            custody_grade="C0",
            price_usd=usd,
            eta_s=0.0,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
            price_status="vendor",
        )
        with tx(conn):
            insert_quote(conn, q)
            transition(
                conn, run.id, S.QUOTED, actor="test", now=now, quote_id=q.id, quote_json=q.terms_json()
            )
    return run.id


def _snapshot(c: TestClient) -> budgets.Spend:
    return budgets.spend_snapshot_all(c.app.state.index, utcnow(), None)


def _reserve_bodies(c: TestClient, run_id: str) -> list[dict]:
    with c.app.state.index.project(PROJECT) as conn:
        rows = conn.execute(
            "SELECT reason FROM run_events WHERE run_id = ? AND kind = 'spend.reserve' ORDER BY seq",
            (run_id,),
        ).fetchall()
    return [json.loads(r["reason"]) for r in rows]


# ---------------------------------------------------------------------------- the attacker's repro


def test_a_declared_import_above_the_cap_warns_and_blocks_no_approval(sync_settings, auth):
    with _client(sync_settings, budgets.Policy(per_user_daily_cap_usd=50.0)) as c:
        packet_id = _setup(c, auth)
        r = c.post(
            "/api/v0/envelopes",
            json={
                "project_id": PROJECT,
                "owner": "agent:test",
                "max_total_usd": 5,
                "max_per_run_usd": 1,
                "ttl_s": 3600,
            },
            headers=auth,
        )
        assert r.status_code == 201, r.text
        envelope_id = r.json()["id"]

        r = _import(c, auth, packet_id, 60)
        assert r.status_code == 201, r.text
        imported = r.json()
        assert imported["warnings"] == [WARNING_60]
        assert imported["state"] == "DONE" and imported["quote"]["price_usd"] == 60.0
        (reserve,) = _reserve_bodies(c, imported["id"])
        assert reserve["external"] is True and reserve["usd"] == 60.0 and reserve["by"] == "human"

        # an auto approval of a 0.04 USD run: 200, not 409 daily_cap (60 + 0.04 would pass the 50 cap)
        auto = _approve(c, auth, _quote_edit(c, auth), by="auto")
        assert auto.status_code == 200, auto.text
        spent = _snapshot(c)
        assert spent.today_usd == pytest.approx(0.04) and spent.external_today_usd == pytest.approx(60.0)
        assert spent.since_human_usd == pytest.approx(0.04)

        # ... and an agent approval under the envelope
        agent = _approve(c, auth, _quote_edit(c, auth), by="agent", envelope_id=envelope_id)
        assert agent.status_code == 200, agent.text
        assert agent.json()["approved_by"] == f"agent:{envelope_id}"
        spent = _snapshot(c)
        assert spent.today_usd == pytest.approx(0.08) and spent.external_today_usd == pytest.approx(60.0)


def test_the_unattended_cap_does_not_count_a_declared_import_either(sync_settings, auth):
    """No human click since the import is recorded: an auto approval is unattended, capped at 20 USD since the
    last human. The 60 USD declaration is not part of that sum."""
    with _client(sync_settings, budgets.Policy(per_user_daily_cap_usd=500.0)) as c:
        packet_id = _setup(c, auth)
        assert _import(c, auth, packet_id, 60).status_code == 201
        c.app.state.pipeline.last_human_utc = None  # nobody has clicked: every approval is unattended
        run_id = _quote_edit(c, auth)
        r = _approve(c, auth, run_id, by="auto")
        assert r.status_code == 200, r.text
        with c.app.state.index.project(PROJECT) as conn:
            spent = budgets.spend_snapshot(conn, utcnow(), None)
        assert spent.since_human_usd == pytest.approx(0.04) and spent.external_today_usd == pytest.approx(
            60.0
        )


# ---------------------------------------------------------------------------- the control: vendor spend is counted


def test_a_vendor_priced_run_still_trips_the_daily_cap(sync_settings, auth):
    policy = budgets.Policy(per_run_auto_cap_usd=100.0, per_user_daily_cap_usd=50.0)
    with _client(sync_settings, policy) as c:
        packet_id = _setup(c, auth)
        # a 60 USD vendor run reserved by auto: 0 + 60 passes the 50 cap
        r = _approve(c, auth, _vendor_run(c, packet_id, 60.0), by="auto")
        assert r.status_code == 409 and r.json()["error"] == "daily_cap", r.text

        # the same 60 USD, approved by a human, is real spend: it counts, and the next auto approval trips
        r = _approve(c, auth, _vendor_run(c, packet_id, 60.0), by="human")
        assert r.status_code == 200, r.text
        (reserve,) = _reserve_bodies(c, r.json()["id"])
        assert "external" not in reserve
        spent = _snapshot(c)
        assert spent.today_usd == pytest.approx(60.0) and spent.external_today_usd == 0.0
        r = _approve(c, auth, _quote_edit(c, auth), by="auto")
        assert r.status_code == 409 and r.json()["error"] == "daily_cap", r.text
        assert "60.00 + 0.04 USD" in r.json()["detail"]


# ---------------------------------------------------------------------------- the warning


@pytest.mark.parametrize("cost", [10, 50, 0, None])
def test_a_declaration_at_or_under_the_cap_has_no_warnings_key(sync_settings, auth, cost):
    with _client(sync_settings, budgets.Policy(per_user_daily_cap_usd=50.0)) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, packet_id, cost)
        assert r.status_code == 201, r.text
        assert "warnings" not in r.json()
        assert _snapshot(c).external_today_usd == pytest.approx(cost or 0.0)


def test_the_warning_names_the_projects_policy_cap(sync_settings, auth):
    with _client(sync_settings, budgets.Policy(per_user_daily_cap_usd=7.5)) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, packet_id, 8)
        assert r.status_code == 201, r.text
        assert r.json()["warnings"] == [
            "declared cost 8.00 USD is above the daily cap 7.50 USD; it is recorded as external spend and "
            "does not count against the automation caps"
        ]


def test_a_refused_import_has_no_warning_and_no_spend(sync_settings, auth):
    with _client(sync_settings, budgets.Policy(per_user_daily_cap_usd=1.0)) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, packet_id, 60, source="no-such-source")
        assert r.status_code == 422 and "warnings" not in r.json(), r.text
        assert _snapshot(c).external_today_usd == 0.0


# ---------------------------------------------------------------------------- the ledger still carries the price


def test_the_ledger_still_carries_the_declared_price_and_plate_spend_is_unchanged(sync_settings, auth):
    with _client(sync_settings, budgets.Policy(per_user_daily_cap_usd=50.0)) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, packet_id, 60)
        assert r.status_code == 201, r.text
        run_id = r.json()["id"]
        # a second, ordinary run next to it, so the totals are a sum and not one record
        assert _approve(c, auth, _quote_edit(c, auth), by="auto").status_code == 200

    led = Ledger(sync_settings.projects_dir / PROJECT)
    assert led.verify() == (True, None)
    records = led.records()
    (quoted,) = [x for x in records if x["kind"] == "quoted" and x.get("run_id") == run_id]
    (approved,) = [x for x in records if x["kind"] == "approved" and x.get("run_id") == run_id]
    assert quoted["cost_usd_quoted"] == 60.0 and quoted["data"]["imported"] is True
    assert approved["data"]["imported"] is True and approved["by"] == "human"
    # `plate spend` is unchanged by this task. It never counted an import: `ledger.LOCAL_ENGINE_PREFIXES` folds
    # `import:` runs as engines that call no vendor, so only the `quoted`/`approved` records above carry the
    # declaration (the plan expected 60.0 here; the fold has always said 0.0 for imports).
    summary = summarize_records(records)
    assert summary["spent_usd_quoted"] == 0.0 and summary["generations"] == 0, summary
    assert "import:manual" not in summary["by_engine"]
