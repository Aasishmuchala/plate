"""The two P1 ingest hooks (router plan T65; PACKET.md 4.3 and 7): legend coverage recomputed from the
Cryptomatte, and the project's own class rules and lint threshold re-applied.

Everything runs on the committed Blender packet `fixtures/packets/room_a` by reference; the failing cases
use tmp copies (the fixture itself is never written to). Library-level tests drive `begin_ingest` /
`finish_ingest` on a real per-project index; the route-level ones post to the app.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import plate_spec
from plate_daemon import ingest
from plate_daemon.api import create_app
from plate_daemon.cas import CAS, hash_file
from plate_daemon.config import Settings
from plate_daemon.index import Index
from plate_daemon.pipeline import PacketView
from plate_daemon.projects import Project

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
SHRUB_O, SHRUB_M, SKY_O = "o:55cbe981", "m:b844e457", "o:929c9e95"

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file(), reason="fixtures/packets/room_a not built yet (T2)"
)


def _manifest(root: Path = ROOM_A) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _write_json(path: Path, doc: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def _copy_without_shrub(tmp_path: Path) -> Path:
    """room_a with `VEG_shrub_A` dropped from id_legend.json, the manifest re-hashed and its policy and
    coverage kept consistent, so the copy is a valid packet whose legend only lies about the EXR."""
    dest = tmp_path / "room_a_no_shrub"
    shutil.copytree(ROOM_A, dest)
    legend = json.loads((dest / "id_legend.json").read_text(encoding="utf-8"))
    dropped = next(e for e in legend["entries"] if e["name"] == "VEG_shrub_A")
    legend["entries"] = [e for e in legend["entries"] if e is not dropped]
    _write_json(dest / "id_legend.json", legend)

    m = _manifest(dest)
    digest, size = hash_file(dest / "id_legend.json")
    ref = m["files"]["id_legend"]
    ref["hash"]["hex"], ref["bytes"] = digest, size
    m["id_legend"]["entry_count"] = len(legend["entries"])
    cov = m["id_legend"]["coverage"]
    cov["labelled_share"] = round(cov["labelled_share"] - dropped["pixel_share"], 4)
    cov["uncovered_share"] = round(cov["uncovered_share"] + dropped["pixel_share"], 4)
    m["policy"]["editable_ids"].remove(dropped["id"])
    _write_json(dest / "manifest.json", m)
    assert (
        plate_spec.validate_packet_dir(dest, check_files=True) == []
    )  # a valid packet; only the EXR disagrees
    return dest


def _set_project_policy(settings: Settings, policy: dict | None) -> Project:
    project = Project.create(settings.projects_dir / PROJECT, PROJECT, {"nda": False})
    if policy is not None:
        project.doc["settings"]["policy"] = policy
        project.save()
    return project


def _ingest(settings: Settings, packet: Path) -> tuple[dict, list[dict], dict]:
    """begin + finish on the project's real index; returns (record, events, effective policy or None)."""
    index = Index(settings.home)
    with index.project(PROJECT) as conn:
        ingest.begin_ingest(conn, packet)
        pid = _manifest(packet)["packet_id"]
        rec = ingest.finish_ingest(conn, CAS(settings.projects_dir / PROJECT / "assets"), pid)
        return rec, ingest.packet_events(conn, pid), ingest.effective_policy(conn, pid)


# ---------------------------------------------------------------------------- recompute_coverage_from_cryptomatte


def test_room_a_ingests_clean(settings):
    rec, events, policy = _ingest(settings, ROOM_A)
    assert rec["status"] == "ingested" and rec["error"] is None
    assert [e["type"] for e in events][0] == "ingest.started" and events[-1]["type"] == "ingest.done"
    assert "notes" not in events[-1]["data"] and "policy" not in events[-1]["data"]
    assert policy is None  # no project.json, so no project policy to record


def test_recompute_agrees_with_the_fixture_legend():
    notes: list[str] = []
    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    before = copy.deepcopy(legend)
    assert ingest.recompute_coverage_from_cryptomatte(ROOM_A, _manifest(), legend=legend, notes=notes) == []
    assert notes == [] and legend == before  # shares agree within 0.001: nothing replaced


def test_recompute_skips_what_has_no_cryptomatte():
    m = _manifest()
    assert ingest.recompute_coverage_from_cryptomatte(ROOM_A, {**m, "tier": "C"}) == []
    no_exr = {**m, "files": {k: v for k, v in m["files"].items() if k != "beauty_exr"}}
    assert ingest.recompute_coverage_from_cryptomatte(ROOM_A, no_exr) == []
    segmented = {**m, "id_legend": {**m["id_legend"], "source": "segmentation"}}
    assert ingest.recompute_coverage_from_cryptomatte(ROOM_A, segmented) == []


def test_placeholder_payload_is_skipped_but_a_corrupt_exr_is_an_error(tmp_path):
    placeholder = plate_spec.materialize_example("tierA_still_clubhouse", tmp_path / "ph")
    notes: list[str] = []
    assert ingest.recompute_coverage_from_cryptomatte(placeholder, _manifest(placeholder), notes=notes) == []
    assert len(notes) == 1 and "placeholder payload" in notes[0]

    corrupt = tmp_path / "corrupt"
    shutil.copytree(ROOM_A, corrupt)
    (corrupt / "beauty.exr").write_bytes(b"not an exr file" * 20)
    errors = ingest.recompute_coverage_from_cryptomatte(corrupt, _manifest(corrupt))
    assert len(errors) == 1 and "not a readable EXR" in errors[0]


def test_legend_that_omits_a_visible_id_fails_the_ingest(settings, tmp_path):
    packet = _copy_without_shrub(tmp_path)
    errors = ingest.recompute_coverage_from_cryptomatte(packet, _manifest(packet))
    assert len(errors) == 1 and errors[0].startswith(f"legend omits visible id {SHRUB_O} (")
    assert errors[0].endswith("%)") and "3.2" in errors[0]  # VEG_shrub_A covers about 3.2 % of the frame

    rec, events, _ = _ingest(settings, packet)
    assert rec["status"] == "failed" and "legend omits visible id" in rec["error"]
    assert events[-1]["type"] == "ingest.failed"
    assert any(SHRUB_O in err for err in events[-1]["data"]["errors"])


def test_wrong_pixel_share_is_replaced_with_a_note_not_an_error():
    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    sky = next(e for e in legend["entries"] if e["id"] == SKY_O)
    near = next(e for e in legend["entries"] if e["name"] == "ARCH_wall_N")
    true_sky, true_near = sky["pixel_share"], near["pixel_share"]
    sky["pixel_share"] = true_sky + 0.01  # off by more than 0.001: replaced
    near["pixel_share"] = true_near + 0.0005  # inside the tolerance: kept
    notes: list[str] = []
    assert ingest.recompute_coverage_from_cryptomatte(ROOM_A, _manifest(), legend=legend, notes=notes) == []
    assert sky["pixel_share"] == pytest.approx(true_sky, abs=1e-4)
    assert near["pixel_share"] == pytest.approx(true_near + 0.0005)
    assert len(notes) == 1 and notes[0].startswith(f"pixel_share of {SKY_O} recomputed")


# ---------------------------------------------------------------------------- reapply_project_policy


def test_policy_is_a_no_op_without_project_or_settings(settings):
    index = Index(settings.home)
    with index.project(PROJECT) as conn:
        m = _manifest()
        assert ingest.reapply_project_policy(conn, ROOM_A, m) == [] and m == _manifest()
        _set_project_policy(settings, None)  # project.json without settings.policy
        assert ingest.reapply_project_policy(conn, ROOM_A, m) == [] and m == _manifest()


def test_locked_class_removes_ids_from_editable_ids(settings):
    _set_project_policy(settings, {"locked_classes": ["vegetation"]})
    rec, events, policy = _ingest(settings, ROOM_A)
    assert rec["status"] == "ingested"
    done = events[-1]["data"]
    assert done["policy"] == policy
    assert f"policy: {SHRUB_O} locked by project class rule" in done["notes"]
    assert f"policy: {SHRUB_M} locked by project class rule" in done["notes"]  # the material narrows it too

    # What the pipeline reads: PacketView.editable_ids() on the manifest carrying the effective policy.
    view = PacketView.load(rec["packet_id"], ROOM_A)
    assert SHRUB_O in view.editable_ids()  # the packet on disk is untouched
    effective = PacketView(
        view.packet_id, view.root, ingest.apply_effective_policy(view.manifest, policy), view.legend
    )
    assert SHRUB_O not in effective.editable_ids() and SHRUB_M not in effective.editable_ids()
    assert SKY_O in effective.editable_ids()  # other classes are untouched
    assert SHRUB_O in effective.locked_ids() and set(effective.locked_ids()) >= set(view.locked_ids())
    assert not set(effective.editable_ids()) & set(effective.locked_ids())
    assert _manifest() == view.manifest  # decision: packets are immutable


def test_reapply_updates_the_manifest_in_memory(settings):
    _set_project_policy(settings, {"locked_classes": ["vegetation", "sky"]})
    m = _manifest()
    with Index(settings.home).project(PROJECT) as conn:
        notes: list[str] = []
        assert ingest.reapply_project_policy(conn, ROOM_A, m, notes=notes) == []
    editable = set(m["policy"]["editable_ids"])
    assert editable == {"o:ebb52806", "o:c8a9bf52", "m:ebb52806", "m:c8a9bf52"}  # people and water stay
    assert len(notes) == 4


def test_lint_threshold_over_the_unlabelled_share_is_an_error(settings):
    _set_project_policy(settings, {"lint_threshold": 0.05})
    m = _manifest()
    with Index(settings.home).project(PROJECT) as conn:
        assert ingest.reapply_project_policy(conn, ROOM_A, m) == []  # room_a has no unlabelled ids
        m["id_legend"]["coverage"]["unlabelled_share"] = 0.2
        errors = ingest.reapply_project_policy(conn, ROOM_A, m)
    assert len(errors) == 1 and "unlabelled share 0.2000" in errors[0] and "lint threshold 0.05" in errors[0]

    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    sign = next(e for e in legend["entries"] if e["id"] == "o:d5cf2e02")
    sign["class_source"], sign["pixel_share"] = "unlabelled", 0.12  # the legend itself says it
    with Index(settings.home).project(PROJECT) as conn:
        assert ingest.reapply_project_policy(conn, ROOM_A, _manifest(), legend=legend)


def test_malformed_project_policy_fails_closed(settings):
    project = _set_project_policy(settings, {"locked_classes": "vegetation"})
    m = _manifest()
    with Index(settings.home).project(PROJECT) as conn:
        assert "locked_classes must be a list" in ingest.reapply_project_policy(conn, ROOM_A, m)[0]
        for bad in (True, "0.1", 2, -0.5):
            project.doc["settings"]["policy"] = {"lint_threshold": bad}
            project.save()
            assert "lint_threshold must be a number" in ingest.reapply_project_policy(conn, ROOM_A, m)[0]
        project.project_json.write_text("{not json", encoding="utf-8")
        assert "unreadable" in ingest.reapply_project_policy(conn, ROOM_A, m)[0]
    assert m == _manifest()


def test_explicit_project_root_wins(settings, tmp_path):
    elsewhere = Project.create(tmp_path / "nas" / "p", PROJECT, {"nda": False})
    elsewhere.doc["settings"]["policy"] = {"locked_classes": ["water"]}
    elsewhere.save()
    m = _manifest()
    with Index(settings.home).project(PROJECT) as conn:
        assert ingest.reapply_project_policy(conn, ROOM_A, m, project_root=elsewhere.root) == []
    assert "o:c8a9bf52" not in m["policy"]["editable_ids"]


# ---------------------------------------------------------------------------- through the app


@pytest.fixture
def sync_client(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    with TestClient(create_app(settings), base_url=BASE) as c:
        yield c, settings, {"Authorization": f"Bearer {settings.token}", "X-Plate-Packet-Version": "0.1"}


def _post(c: TestClient, headers: dict, packet: Path):
    return c.post(
        "/api/v0/packets",
        json={"packet_path": str(packet), "sender": {"name": "t", "version": "0"}},
        headers=headers,
    )


def test_app_rejects_a_legend_that_omits_a_visible_id(sync_client, tmp_path):
    c, _settings, headers = sync_client
    packet = _copy_without_shrub(tmp_path)
    r = _post(c, headers, packet)
    assert r.status_code == 202, r.text
    rec = c.get(f"/api/v0/packets/{r.json()['packet_id']}", headers=headers).json()
    assert rec["status"] == "failed" and "legend omits visible id" in rec["error"]


def test_app_applies_the_project_policy_on_ingest(sync_client):
    c, settings, headers = sync_client
    _set_project_policy(settings, {"locked_classes": ["vegetation"]})
    r = _post(c, headers, ROOM_A)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=headers).json()["status"] == "ingested"
    with c.stream("GET", f"/api/v0/packets/{packet_id}/events", headers=headers) as s:
        frames = [json.loads(line[6:]) for line in s.iter_lines() if line.startswith("data: ")]
    done = frames[-1]
    assert done["type"] == "ingest.done"
    assert SHRUB_O in done["data"]["policy"]["demoted_ids"]
    assert SHRUB_O not in done["data"]["policy"]["editable_ids"]
