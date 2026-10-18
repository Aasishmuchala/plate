"""Wave 12 stress fixes (T141): the scene's project binding is never guessed.

A <scene>.plate.json that exists but cannot be used raises SidecarUnreadable instead of reading as
"unbound" (which fell through to config.json or the stem and sent the packet into another project);
nothing is ever written over it; a file stem that cannot name a project raises ProjectUnresolved
instead of becoming "untitled" or "a"; a sidecar path the file system refuses is a message, never a
bare OSError. The Controller half needs no Qt (the dock half lives in test_ui_dock.py).
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from plate_max import recipes
from plate_max.recipes import CameraRecipe, SidecarUnreadable, SidecarUnwritable
from plate_max.ui import controller as ctl

UNREADABLE = {
    "trailing comma": (b'{"project_id": "clubhouse",}', "not valid JSON"),
    "utf-16 bom": ('{"project_id": "clubhouse"}'.encode("utf-16"), "UTF-16"),
    "empty": (b"", "the file is empty"),
    "blank": (b"  \n", "the file is empty"),
    "conflict markers": (
        b'<<<<<<< HEAD\n{"project_id": "a"}\n=======\n{"project_id": "b"}\n>>>>>>> topic\n',
        "merge conflict markers",
    ),
    "project_id is a number": (b'{"project_id": 7}', "project_id is not a string"),
    "project_id is null": (b'{"project_id": null}', "project_id is not a string"),
    "project_id is no id": ('{"project_id": "!!!"}'.encode(), "is not a project id"),
    "a list": (b'["clubhouse"]', "not an object"),
    "latin-1 text": ('{"note": "caf\xe9"}'.encode("latin-1"), "not UTF-8"),
}


def _save_scene(runtime, folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    runtime.maxFilePath, runtime.maxFileName = str(folder), name
    return folder / name


def _write_config(**keys) -> None:
    path = Path(os.environ["PLATE_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys), encoding="utf-8", newline="\n")


def _sidecar(max_file: Path, raw: bytes) -> Path:
    path = recipes.sidecar_path(max_file)
    path.write_bytes(raw)
    return path


def _boom(*_a, **_k):
    raise AssertionError("an action ran for a scene whose project binding is unusable")


# ---- an unreadable sidecar never re-binds silently, and is never overwritten
@pytest.mark.parametrize("raw_reason", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_an_unreadable_sidecar_raises_and_is_not_skipped_for_config_or_stem(scene, tmp_path, raw_reason):
    raw, reason = raw_reason
    max_file = _save_scene(scene, tmp_path / "jobs", "clubhouse.max")
    side = _sidecar(max_file, raw)
    with pytest.raises(SidecarUnreadable) as caught:
        ctl.scene_project_id(max_file)
    assert reason in str(caught.value) and caught.value.path == side and isinstance(caught.value, ValueError)
    assert (
        str(caught.value)
        == f"clubhouse.plate.json is unreadable ({caught.value.reason}): fix or delete it before sending"
    )
    with pytest.raises(SidecarUnreadable):  # config.json's project_id does not mask it
        ctl.resolve_project_id(max_file, {"project_id": "tula_wot"})
    _write_config(project_id="tula_wot")
    with pytest.raises(SidecarUnreadable):
        ctl.LiveController().project_id()


@pytest.mark.parametrize("raw_reason", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_set_project_and_save_recipes_leave_an_unreadable_sidecar_byte_identical(scene, tmp_path, raw_reason):
    raw, reason = raw_reason
    max_file = _save_scene(scene, tmp_path / "jobs", "clubhouse.max")
    side = _sidecar(max_file, raw)
    live = ctl.LiveController()
    with pytest.raises(SidecarUnreadable):
        live.set_project("tula-wot")
    with pytest.raises(SidecarUnreadable):
        ctl.save_scene_project_id("tula-wot", max_file)
    if reason != "is not a project id":  # recipes only judge the file's shape, not the id's spelling
        with pytest.raises(SidecarUnreadable):
            recipes.save_recipes({"CAM_a": CameraRecipe(camera="CAM_a")}, max_file)
        with pytest.raises(SidecarUnreadable):
            recipes.set_recipe(CameraRecipe(camera="CAM_a", preset="night-relight"), max_file)
    assert side.read_bytes() == raw
    assert sorted(p.name for p in side.parent.iterdir()) == [side.name]  # no temp file left behind
    with pytest.raises(SidecarUnreadable):  # the failed set_project did not bind the scene in memory
        live.project_id()


def test_load_recipes_stays_lenient_about_a_broken_sidecar(scene, tmp_path):
    max_file = _save_scene(scene, tmp_path / "jobs", "clubhouse.max")
    _sidecar(max_file, b'{"project_id": 7, "cameras": {"CAM_a": {"preset": "night-relight"}}}')
    assert recipes.load_recipes(max_file)["CAM_a"].preset == "night-relight"  # only the binding is strict
    _sidecar(max_file, b'{"cameras": {"CAM_a": {"preset": "x"},}}')
    assert recipes.load_recipes(max_file) == {}


def test_a_sidecar_without_a_binding_or_with_a_utf8_bom_is_still_readable(scene, tmp_path):
    max_file = _save_scene(scene, tmp_path / "jobs", "Club House.max")
    assert ctl.scene_project_id(max_file) is None  # absent: unbound, not unreadable
    side = _sidecar(max_file, json.dumps({"format_version": 1, "cameras": {}}).encode())
    assert ctl.scene_project_id(max_file) is None
    assert ctl.resolve_project_id(max_file) == "club-house"  # no binding in it: the stem
    side.write_bytes(b"\xef\xbb\xbf" + json.dumps({"project_id": "Tula WoT"}).encode())
    assert ctl.scene_project_id(max_file) == "tula-wot"
    side.write_bytes(json.dumps({"project_id": "   "}).encode())
    assert ctl.scene_project_id(max_file) is None  # a blank id is no binding
    recipes.save_recipes({"CAM_a": CameraRecipe(camera="CAM_a")}, max_file)  # and it can be written
    assert json.loads(side.read_text(encoding="utf-8"))["cameras"]["CAM_a"]["camera"] == "CAM_a"


def test_a_sidecar_that_is_a_directory_is_unreadable_not_unbound(scene, tmp_path):
    max_file = _save_scene(scene, tmp_path / "jobs", "clubhouse.max")
    recipes.sidecar_path(max_file).mkdir()
    with pytest.raises(SidecarUnreadable, match="cannot be read"):
        ctl.scene_project_id(max_file)


# ---- the Controller refuses, with the message, and says why in unavailable()
def test_send_packet_and_the_other_senders_refuse_with_the_sidecar_message(scene, tmp_path, monkeypatch):
    max_file = _save_scene(scene, tmp_path / "jobs", "clubhouse.max")
    _sidecar(max_file, b'{"project_id": "clubhouse",}')
    monkeypatch.setattr(ctl.LiveController, "_action", lambda self, cap: types.SimpleNamespace(run=_boom))
    live = ctl.LiveController()
    message = live.binding_problem()
    assert message.startswith("clubhouse.plate.json is unreadable (") and message.endswith(
        "): fix or delete it before sending"
    )
    caps = live.capabilities()
    assert not set(ctl.PROJECT_CAPABILITIES) & caps
    assert {ctl.CAP_RECEIVE, ctl.CAP_SCOUT} <= caps  # they put no packet into a project
    why = live.unavailable()
    assert all(why[cap] == message for cap in ctl.PROJECT_CAPABILITIES if cap != ctl.CAP_LIGHT)
    assert ctl.CAP_LIGHT in why  # MaxGaffer's own reason wins when it is missing too
    progress = lambda *a: None  # noqa: E731
    for results in (
        live.send_packet(["CAM_main"], progress),
        live.batch_cameras(["CAM_main"], progress),
        live.send_motion("CAM_main", progress, hero_packet_id="pkt_hero"),
        live.light("CAM_main", progress, reference_path="ref.png"),
    ):
        assert [(r["camera"], r["ok"], r["errors"]) for r in results] == [("CAM_main", False, [message])]
    two = live.send_packet(["CAM_main", "CAM_lobby"], progress)
    assert [r["camera"] for r in two] == ["CAM_main", "CAM_lobby"] and all(
        r["errors"] == [message] for r in two
    )
    quoted = live.send_and_quote(["CAM_main"], progress)
    assert quoted[0]["ok"] is False and quoted[0]["quote_line"] == f"not quoted: {message}"
    assert quoted[0]["quote_error"] is True
    assert live.project_custody() is None and live.hero_candidates("CAM_main") == []
    live.note_packet("CAM_main", "pkt_mine")
    assert live.hero_candidates("CAM_main") == ["pkt_mine"]


def test_fixing_the_sidecar_enables_the_senders_again_without_a_restart(scene, tmp_path):
    max_file = _save_scene(scene, tmp_path / "jobs", "clubhouse.max")
    side = _sidecar(max_file, b"")
    live = ctl.LiveController()
    assert live.binding_problem() and ctl.CAP_SEND_PACKET not in live.capabilities()
    side.write_text(json.dumps({"project_id": "tula-wot"}), encoding="utf-8", newline="\n")
    assert live.binding_problem() is None and live.project_id() == "tula-wot"
    assert ctl.CAP_SEND_PACKET in live.capabilities() and ctl.CAP_SEND_PACKET not in live.unavailable()
    side.unlink()  # deleting it is the other fix: the stem names the project again
    assert ctl.LiveController().project_id() == "clubhouse"


# ---- a stem that names no project
@pytest.mark.parametrize(
    "name",
    ["客户A_别墅.max", "क्लबहाउस.max", "نادي_الفيلا.max", "  .max", "😀😀😀😀.max", "!!!!.max"],
    ids=["cjk", "devanagari", "arabic", "blank", "emoji", "punctuation"],
)
def test_a_stem_that_cannot_name_a_project_is_unresolved(scene, tmp_path, name):
    max_file = _save_scene(scene, tmp_path / "jobs", name)
    assert ctl.stem_project_id(max_file) is None
    with pytest.raises(ctl.ProjectUnresolved) as caught:
        ctl.resolve_project_id(max_file, {})
    stem = Path(name).stem
    assert str(caught.value) == f"pick a project in the Project combo: '{stem}' cannot name one"
    assert isinstance(caught.value, ValueError)
    live = ctl.LiveController()
    assert live.binding_problem() == str(caught.value)
    assert not set(ctl.PROJECT_CAPABILITIES) & live.capabilities()
    [refusal] = live.send_packet(["CAM_main"], lambda *a: None)
    assert refusal["ok"] is False and refusal["errors"] == [str(caught.value)]


def test_picking_a_project_resolves_an_unresolved_stem_and_persists(scene, tmp_path):
    max_file = _save_scene(scene, tmp_path / "jobs", "客户A_别墅.max")
    live = ctl.LiveController()
    assert live.binding_problem() and live.project_custody() is None
    saved = live.set_project("Tula WoT")
    assert saved == recipes.sidecar_path(max_file)
    assert live.binding_problem() is None and live.project_id() == "tula-wot"
    assert set(ctl.PROJECT_CAPABILITIES) & live.capabilities()
    assert ctl.LiveController().project_id() == "tula-wot"  # the next session reads it back


def test_a_config_project_id_resolves_an_unsluggable_stem(scene, tmp_path):
    _save_scene(scene, tmp_path / "jobs", "客户A_别墅.max")
    _write_config(project_id="clubhouse")
    assert ctl.LiveController().project_id() == "clubhouse"


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("Club House.max", "club-house"),
        ("Club House_v2.max", "club-house-v2"),
        ("ab.max", "ab"),  # short, but the whole stem: nothing was lost
        ("客户A.max", "a"),  # a stem of 3 or fewer characters keeps what it has
        ("客户A_别.max", None),  # 4 characters slugging to "a"
        ("Aa_é_é.max", None),  # "aa" out of 6
        ("abc.max", "abc"),
        ("a_b_c_客户.max", "a-b-c"),
    ],
)
def test_stem_project_id_rules(name, want):
    assert ctl.stem_project_id(Path("jobs") / name) == want


def test_an_unsaved_scene_keeps_untitled(scene):
    assert ctl.stem_project_id(None) == "untitled" and ctl.stem_project_id("") == "untitled"
    assert ctl.resolve_project_id("", {}) == "untitled"
    live = ctl.LiveController()
    assert live.project_id() == "untitled" and live.binding_problem() is None
    assert ctl.CAP_SEND_PACKET in live.capabilities()
    assert live.set_project("adhoc") is None and live.project_id() == "adhoc"  # kept in memory only


# ---- a sidecar path the file system refuses
def test_a_249_character_stem_gives_a_clear_message_not_an_oserror(scene, tmp_path):
    name = "a" * 249 + ".max"
    max_file = _save_scene(scene, tmp_path / "jobs", name)
    live = ctl.LiveController()
    assert live.project_id() == "a" * 64 and live.binding_problem() is None  # reading it is not an error
    assert recipes.load_recipes(max_file) == {}
    with pytest.raises(SidecarUnwritable) as caught:
        live.set_project("clubhouse")
    message = str(caught.value)
    assert not isinstance(caught.value, OSError) and isinstance(caught.value, SidecarUnreadable)
    assert "cannot be written" in message and "shorten the scene's file name" in message
    assert message.startswith("a" * 40 + "...") and len(message) < 300  # the name is elided, not pasted whole
    with pytest.raises(SidecarUnwritable):
        recipes.save_recipes({"CAM_a": CameraRecipe(camera="CAM_a")}, max_file)
    assert live.project_id() == "a" * 64  # the refused pick did not bind
    assert not [p for p in (tmp_path / "jobs").iterdir()]  # and no temp file was left in the folder


def test_a_folder_that_refuses_the_write_is_a_message_too(scene, tmp_path, monkeypatch):
    max_file = _save_scene(scene, tmp_path / "jobs", "clubhouse.max")

    def refuse(path, data):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(recipes, "atomic_write_json", refuse)
    with pytest.raises(
        SidecarUnwritable, match=r"clubhouse\.plate\.json cannot be written \(Permission denied\)"
    ):
        ctl.save_scene_project_id("tula-wot", max_file)
