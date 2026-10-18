"""Custody grades as code (T14): ordering, the stricter-wins intersection, pending counts as D, the LLM route
read from custody_routes.yaml, and the vendor matrix the docs show."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from plate_daemon import custody
from plate_daemon.custody import ALL_GRADES, NDA_GRADES, CustodyViolation, Grade
from plate_daemon.projects import Project
from plate_engines import router
from plate_engines.loader import load_manifests

REPO = Path(__file__).resolve().parents[2]
G = Grade


@pytest.fixture(scope="module")
def manifests():
    return load_manifests()


def _confirmed(route, grade: str = "C1"):
    """A copy of a manifest route as if its written terms were filed (model_copy skips validation)."""
    return route.model_copy(
        update={"custody_grade": grade, "custody_status": "confirmed", "terms_hash": "a" * 64}
    )


# ---------------------------------------------------------------------------- grades


def test_grades_order_by_strictness():
    assert G.C0 < G.C1 < G.C2 < G.D
    assert sorted([G.D, G.C1, G.C0, G.C2]) == [G.C0, G.C1, G.C2, G.D]
    assert max(ALL_GRADES) is G.D and min(ALL_GRADES) is G.C0
    assert G.C1 <= "C1" < "C2" and G.D > "C2" and G.C0 >= "C0"
    assert [g.rank for g in (G.C0, G.C1, G.C2, G.D)] == [0, 1, 2, 3]
    assert "C0" in NDA_GRADES and G.C1 in NDA_GRADES and G.C2 not in NDA_GRADES  # str and enum hash alike


def test_unknown_grade_is_refused():
    with pytest.raises(ValueError, match="unknown custody grade"):
        Grade.parse("C3")
    with pytest.raises(ValueError):
        custody.project_allowed({"nda": False, "allowed_grades": ["C0", "E"]})
    with pytest.raises(TypeError):
        _ = G.C0 < 3


# ---------------------------------------------------------------------------- the intersection


@pytest.mark.parametrize(
    ("project", "packet", "expected"),
    [
        ({"nda": False}, None, {"C0", "C1", "C2", "D"}),
        ({"nda": True}, None, {"C0", "C1"}),
        ({"nda": False}, {"nda": True, "allowed_grades": ["C0", "C1"]}, {"C0", "C1"}),
        (
            {"nda": False, "allowed_grades": ["C0", "C2"]},
            {"nda": False, "allowed_grades": ["C2", "D"]},
            {"C2"},
        ),
        ({"nda": False, "allowed_grades": ["C0"]}, {"nda": True, "allowed_grades": ["C0", "C1"]}, {"C0"}),
        # The packet floor can never widen what the project allows, nor lift NDA's cap.
        ({"nda": True, "allowed_grades": ["C0", "C1"]}, {"nda": False, "allowed_grades": ["C2", "D"]}, set()),
        ({"nda": False}, {"nda": True, "allowed_grades": ["C0", "C1", "C2"]}, {"C0", "C1"}),
        ({"nda": False}, ["C1", "D"], {"C1", "D"}),
    ],
)
def test_project_allowed_is_the_stricter_intersection(project, packet, expected):
    assert custody.project_allowed(project, packet) == {Grade(g) for g in expected}


def test_nda_is_capped_at_c0_c1_whatever_the_lists_say():
    # A hand-edited project.json that lists D on an NDA project still gets C0/C1 only.
    assert custody.project_allowed({"nda": True, "allowed_grades": ["C0", "C1", "C2", "D"]}) == {G.C0, G.C1}


def test_project_with_no_custody_is_local_only():
    assert custody.project_allowed(None) == {G.C0}
    assert custody.project_allowed(None, {"nda": False}) == {G.C0}


def test_accepts_a_project_and_a_project_doc(tmp_path):
    project = Project.create(tmp_path / "p", "tula-wot", {"nda": True})
    assert custody.project_allowed(project) == {G.C0, G.C1}
    assert custody.project_allowed(project.doc) == {G.C0, G.C1}
    example = REPO / "spec" / "examples" / "tierA_still_clubhouse" / "manifest.json"
    packet = json.loads(example.read_text(encoding="utf-8"))
    open_project = Project.create(tmp_path / "q", "own-scene", {"nda": False})
    assert custody.project_allowed(open_project, packet["policy"]["custody"]) == {G.C0, G.C1}


# ---------------------------------------------------------------------------- effective grade


def test_pending_counts_as_d_and_public_uploads_are_d():
    assert custody.effective_grade({"custody_grade": "C1", "custody_status": "pending_written_terms"}) is G.D
    assert custody.effective_grade({"custody_grade": "C1", "custody_status": "confirmed"}) is G.C1
    assert custody.effective_grade({"grade": "C0", "status": "confirmed"}) is G.C0
    assert (
        custody.effective_grade(
            {"custody_grade": "C2", "custody_status": "confirmed", "upload_visibility": "public"}
        )
        is G.D
    )


def test_effective_grade_agrees_with_the_router_on_every_manifest_route(manifests):
    routes = [r for m in manifests.values() for r in m.routes]
    assert len(routes) >= 14
    for r in routes:
        assert custody.effective_grade(r).value == router.effective_grade(r), r.route_id
        promoted = _confirmed(r) if r.upload_visibility == "private" else r
        assert custody.effective_grade(promoted).value == router.effective_grade(promoted), r.route_id


def test_filter_routes_on_nda_keeps_only_local_today(manifests):
    kept = {m.id: custody.filter_routes(m, NDA_GRADES) for m in manifests.values()}
    for engine_id, routes in kept.items():
        for r in routes:
            assert r.custody_grade == "C0" and r.upload_visibility == "local", engine_id
    assert kept["qwen-image-edit-2509"] and kept["wan-2.2-vace"]
    assert kept["gpt-image-2.5"] == [] and kept["nano-banana-2.1"] == [] and kept["seedream-5-pro"] == []
    # Everything is allowed on a project that accepts D.
    for m in manifests.values():
        assert custody.filter_routes(m, ALL_GRADES) == list(m.routes)


def test_a_confirmed_c1_route_becomes_usable_on_nda(manifests):
    gpt = manifests["gpt-image-2.5"]
    confirmed = gpt.model_copy(update={"routes": [_confirmed(r) for r in gpt.routes]})
    assert [r.route_id for r in custody.filter_routes(confirmed, NDA_GRADES)] == [
        r.route_id for r in gpt.routes
    ]
    assert custody.require_route(confirmed.routes[0], NDA_GRADES) is G.C1


def test_require_route_raises_custody_violation_naming_why(manifests):
    atlas = manifests["seedream-5-pro"].routes[0]
    with pytest.raises(CustodyViolation) as exc:
        custody.require_route(atlas, NDA_GRADES, what="seedream-5-pro")
    e = exc.value
    assert e.code == "custody" and e.grade is G.D and e.allowed == NDA_GRADES
    assert e.route_id == atlas.route_id and "seedream-5-pro" in str(e) and str(e).startswith("custody:")
    openai = manifests["gpt-image-2.5"].routes[0]
    with pytest.raises(CustodyViolation, match="pending counts as D"):
        custody.require_route(openai, NDA_GRADES)
    assert custody.route_allowed(openai, ALL_GRADES) and not custody.route_allowed(openai, NDA_GRADES)
    with pytest.raises(CustodyViolation, match="nothing"):
        custody.require_route(manifests["qwen-image-edit-2509"].routes[0], set())


# ---------------------------------------------------------------------------- the LLM route


def test_anthropic_api_is_a_pending_c1_so_nda_runs_without_composer_and_judge():
    llm = custody.llm_route()
    assert (llm.route_id, llm.provider, llm.grade, llm.status) == (
        "anthropic-api",
        "anthropic",
        G.C1,
        "pending_written_terms",
    )
    assert llm.effective is G.D and custody.effective_grade(llm) is G.D
    assert llm.allowed_hosts == ("api.anthropic.com",)
    assert "until the written terms are filed" in llm.note
    assert custody.llm_allowed(NDA_GRADES) is False
    assert custody.llm_allowed(ALL_GRADES) is True  # an own-scene project that accepts D may use it
    assert custody.LLM_UNAVAILABLE == "unavailable: custody"


def _routes_copy(tmp_path: Path, **llm_updates) -> Path:
    doc = yaml.safe_load(custody.ROUTES_YAML.read_text(encoding="utf-8"))
    doc["llm"].update(llm_updates)
    path = tmp_path / "custody_routes.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def test_promoting_the_llm_route_is_an_edit_to_the_yaml(tmp_path):
    path = _routes_copy(tmp_path, status="confirmed", terms_hash="b" * 64)
    llm = custody.llm_route(path)
    assert llm.effective is G.C1
    assert custody.llm_allowed(NDA_GRADES, path) is True
    rows = {r["route_ids"][0]: r for r in custody.matrix(path=path)}
    assert rows["anthropic-api"]["effective_grade"] == "C1" and rows["anthropic-api"]["nda_allowed"] is True


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"status": "confirmed"}, "terms_hash"),
        ({"status": "signed"}, "status must be"),
        ({"grade": "C7"}, "unknown custody grade"),
        ({"terms_hash": "not-a-hash"}, "sha256"),
        ({"upload_visibility": "public"}, "private"),
    ],
)
def test_a_malformed_llm_route_is_refused(tmp_path, updates, message):
    with pytest.raises(ValueError, match=message):
        custody.llm_route(_routes_copy(tmp_path, **updates))


def test_yaml_agrees_with_docs_custody_md():
    text = (REPO / "docs" / "custody.md").read_text(encoding="utf-8")
    api_row = next(ln for ln in text.splitlines() if ln.startswith("| **Anthropic API**"))
    login_row = next(ln for ln in text.splitlines() if ln.startswith("| **Anthropic consumer login**"))
    llm = custody.llm_route()
    if llm.status == "pending_written_terms":
        assert "**D** effective (C1 candidate, pending)" in api_row
    else:  # promoted: the doc row must say so too
        assert f"**{llm.grade.value}**" in api_row and "pending" not in api_row.split("|")[2]
    assert "**Never**" in login_row
    runway_row = next(ln for ln in text.splitlines() if ln.startswith("| **Runway (Dev API)**"))
    assert "**D** (on the evidence" in runway_row and "s4.4" in runway_row
    never = yaml.safe_load(custody.ROUTES_YAML.read_text(encoding="utf-8"))["never"]
    assert [n["route_id"] for n in never] == ["anthropic-consumer-login"]


# ---------------------------------------------------------------------------- the matrix


def test_matrix_shows_every_vendor_at_its_effective_grade(manifests):
    rows = custody.matrix(manifests)
    by_vendor: dict[str, list[dict]] = {}
    for r in rows:
        by_vendor.setdefault(r["vendor"], []).append(r)

    def only(vendor: str) -> dict:
        assert len(by_vendor[vendor]) == 1, by_vendor[vendor]
        return by_vendor[vendor][0]

    assert only("Atlas (AtlasCloud)")["effective_grade"] == "D"
    assert only("Atlas (AtlasCloud)")["upload_visibility"] == "public"
    for vendor in ("Runway (web app)", "Higgsfield", "fal"):
        assert only(vendor)["effective_grade"] == "D" and not only(vendor)["nda_allowed"]
    runway = only("Runway (Dev API)")
    assert runway["effective_grade"] == "D" and runway["custody_status"] == "confirmed"
    assert runway["upload_visibility"] == "private" and runway["nda_allowed"] is False
    assert set(runway["engines"]) == {
        "gen-4.5",
        "kling-o3-4k",
        "kling-v3-pro",
        "seedance-2.5-i2v",
        "seedance-2.5-r2v",
        "veo-3.1",
    }
    assert only("Runway (web app)")["engines"] == [] and "graded apart" in only("Runway (web app)")["note"]
    for vendor in ("OpenAI", "Google Gemini API", "Google Vertex AI", "Anthropic API"):
        row = only(vendor)
        assert (row["declared_grade"], row["custody_status"], row["effective_grade"]) == (
            "C1",
            "pending_written_terms",
            "D",
        ), vendor
        assert not row["nda_allowed"]
    assert (
        "NDA projects run without composer and judge until the written terms are filed"
        in only("Anthropic API")["note"]
    )
    assert "C1 candidate" in only("OpenAI")["note"]
    login = only("Anthropic consumer login")
    assert login["never"] is True and login["nda_allowed"] is False and login["custody_status"] == "never"
    local = only("Local (render box)")
    assert local["effective_grade"] == "C0" and local["nda_allowed"] is True
    assert {"qwen-image-edit-2509", "wan-2.2-vace", "real-esrgan"} <= set(local["engines"])
    assert [r["vendor"] for r in rows if r["nda_allowed"]] == ["Local (render box)"]
    engines_listed = {e for r in rows for e in r["engines"]}
    assert set(manifests) <= engines_listed


def test_matrix_markdown_is_a_table():
    md = custody.matrix_markdown()
    lines = md.strip().splitlines()
    assert lines[0].startswith("| vendor |") and re.fullmatch(r"(\|---)+\|", lines[1])
    assert any(ln.startswith("| Anthropic consumer login |") and "| never |" in ln for ln in lines)
    assert all(ln.count("|") == lines[0].count("|") for ln in lines)
