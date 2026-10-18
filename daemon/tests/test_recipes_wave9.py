"""Runnable recipes, wave 9 (router plan T83): Lensbook a01 to a10 promoted, three folders, save and shadow.

Pure recipes-module tests (the HTTP routes are in test_routes_engines_wave9.py). `PLATE_HOME` is a tmp folder (the
autouse fixture in conftest), so the studio folder is `tmp_path / "recipes"`.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import yaml

from plate_daemon import recipes
from plate_daemon.recipes import ProjectSheet, RecipeError, RecipeExists
from plate_engines.loader import load_manifests
from plate_max import policy
from plate_spec import NEVER_EDITABLE

REPO = Path(__file__).resolve().parents[2]
NEW_IDS = [
    "relight_same_family",
    "relight_cross_family",
    "twilight_hero",
    "monsoon_pass",
    "season_swap",
    "staffage_walkers",
    "interior_occupants",
    "imperfections_close",
    "planting_refresh",
    "night_exterior",
    "festive_dress",
]
LIGHTING_CLASSES = ["sky", "vegetation", "water", "people"]
# id -> (library, group, task, editable_classes)
SPEC = {
    "relight_same_family": ("a01", "lighting", "relight", LIGHTING_CLASSES),
    "relight_cross_family": ("a02", "lighting", "relight", LIGHTING_CLASSES),
    "twilight_hero": ("a03", "lighting", "relight", LIGHTING_CLASSES),
    "monsoon_pass": ("a04", "weather", "relight", LIGHTING_CLASSES),
    "season_swap": ("a05", "season", "vegetation", ["vegetation", "sky", "water"]),
    "staffage_walkers": ("a06", "people", "people", ["people"]),
    "interior_occupants": ("a07", "people", "people", ["people"]),
    "imperfections_close": ("a08", "beauty", "rephotograph_close", []),
    "planting_refresh": ("a09", "vegetation", "vegetation", ["vegetation"]),
    "night_exterior": ("a10", "lighting", "relight", LIGHTING_CLASSES),
    "festive_dress": ("a05", "season", "vegetation", ["vegetation", "people", "sky", "furniture"]),
}


def doc_for(rid: str = "my_recipe", **patch) -> dict:
    doc = {
        "schema": 1,
        "id": rid,
        "title": "My recipe",
        "kind": "still_edit",
        "engine": "nano-banana-2.1",
        "group": "mine",
        "mask": {"editable_classes": ["sky"], "region": "none", "lock_rest": True},
        "prompt": "Relight to [TARGET HOUR].\nKeep the building.",
        "negative_prompt": "new objects",
        "overrides": {"[TARGET HOUR]": "dawn"},
    }
    doc.update(patch)
    return doc


def comparable(r: recipes.RunnableRecipe) -> recipes.RunnableRecipe:
    return dataclasses.replace(r, source="")


# ---------------------------------------------------------------------------- the shipped set


def test_fourteen_runnable_recipes_ship_and_every_one_runs_without_a_composer():
    ids = recipes.list_runnable()
    assert len(ids) >= 14 and set(NEW_IDS) <= set(ids)
    manifests = load_manifests()
    for rid in ids:
        r = recipes.load_runnable(rid)
        assert r.origin == "shipped" and r.id == rid
        assert r.engine in manifests, rid
        assert recipes.unfilled_count(recipes.assemble_runnable(r, ProjectSheet())) == 0, rid


@pytest.mark.parametrize("rid", NEW_IDS)
def test_each_promoted_recipe_matches_its_row_in_the_plan(rid):
    library, group, task, classes = SPEC[rid]
    r = recipes.load_runnable(rid)
    assert (r.library, r.group, r.task) == (library, group, task)
    assert r.mask["editable_classes"] == classes and r.mask["region"] == "none"
    assert r.prefixed is False
    assert r.mask["lock_rest"] is (rid != "imperfections_close")
    assert r.negative_prompt and len(r.negative_prompt.split()) <= 30
    # the library recipe it applies exists, and the runnable names the same job in its own words
    assert recipes.get_recipe(library).id == library
    if rid == "imperfections_close":
        assert (r.kind, r.engine, r.shot_class) == ("still_rephotograph", "gpt-image-2.5", "close")
    else:
        assert (r.kind, r.engine) == ("still_edit", "nano-banana-2.1")


def test_imperfections_close_carries_the_same_router_check_as_sunburst():
    beauty = recipes.load_runnable("imperfections_close")
    sunburst = recipes.load_runnable("sunburst_rephotograph_close")
    assert beauty.router_check == sunburst.router_check
    assert beauty.router_check["require_shot_class"] == "close"


def test_editable_classes_are_ones_the_daemons_legend_uses_and_the_policy_can_edit():
    schema = json.loads((REPO / "spec" / "packet.schema.json").read_text(encoding="utf-8"))
    legend_classes = set(schema["$defs"]["id_class"]["enum"])
    # classes plate_max.policy gives the layer prefixes it treats as editable (VEG_, SKY, PEOPLE, WATER, ENT_, GRASS)
    editable = {
        rule["class"] for rule in policy.CLASS_RULES if rule["pattern"].startswith(policy.EDITABLE_PREFIXES)
    }
    assert editable == {"vegetation", "sky", "people", "water", "furniture"}
    for rid in recipes.list_runnable():
        for cls in recipes.load_runnable(rid).mask["editable_classes"]:
            assert cls in legend_classes and cls not in NEVER_EDITABLE, (rid, cls)
            assert cls in editable, (rid, cls)
    festive = recipes.load_runnable("festive_dress")
    assert "furniture" in festive.mask["editable_classes"]  # ENT_ layers; there is no class called entourage
    assert "entourage" not in {
        c for rid in recipes.list_runnable() for c in recipes.load_runnable(rid).mask["editable_classes"]
    }


def test_shipped_tokens_are_recipe_local_so_a_default_never_hides_a_project_sheet_value():
    for rid in NEW_IDS:
        r = recipes.load_runnable(rid)
        for token in r.overrides:
            inner = token[1:-1].strip()
            assert not any(rule.pattern.search(inner) for rule in recipes.token_rules()), (rid, token)
        # every token in the prompt has a default, and every default is used
        assert set(recipes.unfilled_tokens(r.prompt)) == set(r.overrides), rid


def test_season_swap_and_festive_dress_take_their_one_token():
    swap = recipes.load_runnable("season_swap")
    assert "Shift the season to autumn" in recipes.assemble_runnable(swap, ProjectSheet())
    assert "Shift the season to winter" in recipes.assemble_runnable(
        swap, ProjectSheet(), {"[SEASON]": "winter"}
    )
    festive = recipes.load_runnable("festive_dress")
    assert festive.overrides == {"[OCCASION]": "Diwali"}
    assert "Eid" in recipes.assemble_runnable(festive, ProjectSheet(), {"[OCCASION]": "Eid"})
    twilight = recipes.load_runnable("twilight_hero")
    assert "2700K" in recipes.assemble_runnable(twilight, ProjectSheet())
    same = recipes.load_runnable("relight_same_family")
    assert same.overrides["[TARGET HOUR]"] == "late afternoon"


def test_every_shipped_file_id_is_its_file_stem():
    for path in sorted(recipes.RECIPES_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if doc.get("kind") in recipes.KINDS:
            assert doc["id"] == path.stem and recipes.is_recipe_id(doc["id"]), path.name


# ---------------------------------------------------------------------------- three folders


def test_studio_dir_follows_plate_home(plate_env):
    assert recipes.studio_dir() == plate_env / "recipes"
    assert recipes.project_recipes_dir(plate_env / "p1") == plate_env / "p1" / "recipes"


def test_a_project_folder_recipe_lists_as_project_and_shadows_nothing(tmp_path):
    project = tmp_path / "proj"
    path = recipes.save_runnable(doc_for("only_here"), "project", project)
    assert path == project / "recipes" / "only_here.yaml" and path.is_file()
    shipped = recipes.list_runnable()
    with_project = recipes.list_runnable(project)
    assert "only_here" not in shipped and with_project == [*shipped, "only_here"]
    assert recipes.load_runnable("only_here", project).origin == "project"
    assert recipes.load_runnable("croppair_pushin", project).origin == "shipped"
    with pytest.raises(RecipeError, match="no runnable recipe"):
        recipes.load_runnable("only_here")  # another project's recipe is not visible without its folder
    entries = {r.id: r.origin for r in recipes.runnable_entries(project)}
    assert entries["only_here"] == "project" and entries["night_exterior"] == "shipped"


def test_a_studio_recipe_is_visible_everywhere_and_a_project_one_wins_over_it(tmp_path):
    project = tmp_path / "proj"
    recipes.save_runnable(doc_for("twin", title="studio twin"), "studio")
    assert recipes.load_runnable("twin").origin == "studio"
    assert recipes.load_runnable("twin", project).title == "studio twin"
    recipes.save_runnable(doc_for("twin", title="project twin"), "project", project, overwrite=True)
    got = recipes.load_runnable("twin", project)
    assert (got.origin, got.title) == ("project", "project twin")
    assert recipes.load_runnable("twin").title == "studio twin"
    assert recipes.list_runnable(project).count("twin") == 1  # a shadowed id is listed once


def test_saving_a_shipped_id_without_overwrite_is_refused_and_with_it_shadows(plate_env):
    shipped_file = recipes.RECIPES_DIR / "season_swap.yaml"
    before = shipped_file.read_bytes()
    with pytest.raises(RecipeExists, match="season_swap"):
        recipes.save_runnable(doc_for("season_swap"), "studio")
    assert not (plate_env / "recipes").exists()
    path = recipes.save_runnable(doc_for("season_swap", title="my season swap"), "studio", overwrite=True)
    assert path == plate_env / "recipes" / "season_swap.yaml"
    assert recipes.load_runnable("season_swap").origin == "studio"
    assert recipes.load_runnable("season_swap").title == "my season swap"
    assert shipped_file.read_bytes() == before  # the shipped folder is never written
    assert recipes.list_runnable().count("season_swap") == 1


def test_saving_the_same_studio_id_twice_needs_overwrite():
    recipes.save_runnable(doc_for("again"), "studio")
    with pytest.raises(RecipeExists):
        recipes.save_runnable(doc_for("again"), "studio")
    recipes.save_runnable(doc_for("again", title="second"), "studio", overwrite=True)
    assert recipes.load_runnable("again").title == "second"


def test_a_saved_recipe_reads_back_equal():
    doc = doc_for("roundtrip", shot_class="medium", task="people", params={"n": 2, "size": "native"})
    path = recipes.save_runnable(doc, "studio")
    saved = recipes.load_runnable("roundtrip")
    assert comparable(saved) == comparable(recipes.parse_runnable(doc, "studio"))
    assert (saved.group, saved.task, saved.shot_class, saved.params["n"]) == ("mine", "people", "medium", 2)
    assert recipes.load_runnable(path).id == "roundtrip"  # by path too
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == doc


def test_a_saved_file_is_utf8_lf_and_a_prompt_is_a_block_scalar():
    doc = doc_for("lines", prompt="Line one, with an accent: café.\nLine two.\n\nLine four.")
    path = recipes.save_runnable(doc, "studio")
    raw = path.read_bytes()
    assert b"\r" not in raw and "café".encode() in raw
    assert "prompt: |-\n  Line one" in raw.decode("utf-8")
    assert recipes.load_runnable("lines").prompt == doc["prompt"]


def test_the_origin_key_is_derived_never_written():
    path = recipes.save_runnable(doc_for("derived", origin="shipped"), "studio")
    assert "origin" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    assert recipes.load_runnable("derived").origin == "studio"


def test_a_file_loaded_by_path_takes_its_origin_from_the_folder_it_lives_in(tmp_path):
    project = tmp_path / "proj"
    (project / "recipes").mkdir(parents=True)
    mine = project / "recipes" / "loose.yaml"
    mine.write_text(yaml.safe_dump(doc_for("loose")), encoding="utf-8", newline="\n")
    assert recipes.load_runnable(mine, project).origin == "project"
    assert recipes.load_runnable(recipes.RECIPES_DIR / "croppair_pushin.yaml").origin == "shipped"
    recipes.save_runnable(doc_for("in_studio"), "studio")
    assert recipes.load_runnable(recipes.studio_dir() / "in_studio.yaml").origin == "studio"


def test_a_file_outside_the_recipe_folders_is_not_loaded_by_path(tmp_path):
    """Decision 88: a path must lie in the project's recipes/ folder, the studio's or the shipped one."""
    project = tmp_path / "proj"
    (project / "recipes").mkdir(parents=True)
    loose = tmp_path / "loose.yaml"
    loose.write_text(yaml.safe_dump(doc_for("loose")), encoding="utf-8", newline="\n")
    with pytest.raises(RecipeError, match="must live in"):
        recipes.load_runnable(loose)
    with pytest.raises(RecipeError, match="must live in"):
        recipes.load_runnable(loose, project)
    mine = project / "recipes" / "loose.yaml"
    mine.write_bytes(loose.read_bytes())
    # the project's folder only counts for that project
    with pytest.raises(RecipeError, match="must live in"):
        recipes.load_runnable(mine)
    assert recipes.load_runnable(mine, project).id == "loose"


@pytest.mark.parametrize(
    "patch, match",
    [
        ({"prompt": ""}, "prompt"),
        ({"kind": "banner"}, "kind must be one of"),
        ({"id": "Bad-Id"}, "lower_snake_case"),
        ({"id": "con"}, "reserved"),
        ({"id": "lpt1"}, "reserved"),
        ({"task": "text_to_image"}, "task must be one of"),
        ({"thumbnail": "../../etc/passwd"}, "CAS hash"),
        ({"group": 3}, "group"),
        ({"library": "a99"}, "no library recipe"),
        ({"mask": {"editable_classes": "sky"}}, "editable_classes"),
        ({"schema": 2}, "schema: 1"),
    ],
)
def test_a_bad_doc_is_refused_and_nothing_is_written(plate_env, patch, match):
    with pytest.raises(RecipeError, match=match):
        recipes.save_runnable(doc_for(**patch), "studio")
    assert not (plate_env / "recipes").exists()


def test_save_scope_rules(tmp_path):
    with pytest.raises(RecipeError, match="scope"):
        recipes.save_runnable(doc_for(), "shipped")
    with pytest.raises(RecipeError, match="project folder"):
        recipes.save_runnable(doc_for(), "project")
    with pytest.raises(RecipeError, match="mapping"):
        recipes.save_runnable(["not", "a", "doc"], "studio")  # type: ignore[arg-type]


def test_thumbnail_and_task_round_trip():
    thumb = "ab" * 32
    recipes.save_runnable(doc_for("pretty", thumbnail=thumb, task="vegetation"), "studio")
    r = recipes.load_runnable("pretty")
    assert (r.thumbnail, r.task) == (thumb, "vegetation")


def test_a_file_that_does_not_load_is_left_out_of_the_entries(plate_env):
    studio = recipes.studio_dir()
    studio.mkdir(parents=True)
    (studio / "broken.yaml").write_text(yaml.safe_dump(doc_for("broken", prompt="")), encoding="utf-8")
    (studio / "mismatch.yaml").write_text(yaml.safe_dump(doc_for("other_name")), encoding="utf-8")
    (studio / "notes.yaml").write_text("just: notes\n", encoding="utf-8")
    assert "broken" in recipes.list_runnable()  # it looks like a recipe ...
    assert "broken" not in {
        r.id for r in recipes.runnable_entries()
    }  # ... and fails to load, so it is left out
    assert "other_name" not in recipes.list_runnable() and "mismatch" not in recipes.list_runnable()
    assert "notes" not in recipes.list_runnable()


def test_ids_that_could_escape_the_folder_are_not_recipe_ids():
    for bad in ("../x", "a/b", "x.yaml", "X", "", "con", "9lives", "a-b"):
        assert recipes.is_recipe_id(bad) is False, bad
        with pytest.raises(
            RecipeError
        ):  # an id that is not one is looked up as an id, or read as a path that is absent
            recipes.load_runnable(bad)
    assert recipes.is_recipe_id("night_exterior") is True


def test_unfilled_tokens_are_listed_once_in_order():
    assert recipes.unfilled_tokens("a [B] c [A] d [B]") == ["[B]", "[A]"]
    assert recipes.unfilled_tokens("none") == []
