"""bridge.render (packet frame), bridge.vantage (vrscene, live link) and bridge.safety on the fake.

The fake runtime records render()/vfbControl()/saveMaxFile()/vrayExportVRScene() calls. V-Ray itself
is simulated here: when the renderer's raw-image output is on, the wrapped render() writes a file
with the OpenEXR magic number to output_rawFileName, and the VFB save writes a real 16-bit TIFF.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from plate_max import rt
from plate_max.bridge import elements, render, safety, vantage

DEFAULT_LAYERS = [
    "RGBA",
    "VRayZDepth",
    "VRayNormals",
    "CryptoObject",
    "CryptoMaterial",
    "VRayWireColor",
    "VRayDiffuseFilter",
]


def tiff(bits: int = 16, order: str = "<", samples: int = 3) -> bytes:
    """A minimal 2x1 RGB TIFF with BitsPerSample = bits per channel (stdlib struct only)."""
    head = (b"II*\x00" if order == "<" else b"MM\x00*") + struct.pack(order + "I", 8)
    entries = [
        (256, 3, 1, struct.pack(order + "HH", 2, 0)),
        (257, 3, 1, struct.pack(order + "HH", 1, 0)),
    ]
    n_tags = 4
    ifd_size = 2 + 12 * n_tags + 4
    bits_offset = 8 + ifd_size
    if samples * 2 <= 4:
        bits_value = struct.pack(order + "H" * samples, *([bits] * samples)) + b"\x00" * (4 - 2 * samples)
        extra = b""
    else:
        bits_value = struct.pack(order + "I", bits_offset)
        extra = struct.pack(order + "H" * samples, *([bits] * samples))
    entries.append((258, 3, samples, bits_value))
    entries.append((277, 3, 1, struct.pack(order + "HH", samples, 0)))
    ifd = struct.pack(order + "H", n_tags) + b"".join(
        struct.pack(order + "HHI", tag, kind, count) + value for tag, kind, count, value in entries
    )
    ifd += struct.pack(order + "I", 0)
    pixels = b"\x00" * (2 * samples * bits // 8)
    return head + ifd + extra + pixels


def make_vray(scene, tif_bits: int = 16):
    """Give the fake renderer the V-Ray output properties and make render()/VFB behave like V-Ray."""
    vr = scene.renderers.current
    vr.output_saveRawFile = False
    vr.output_rawFileName = ""
    vr.output_genpreview = True
    vr.output_on = True
    vr.output_splitgbuffer = True
    vr.output_rawExrBitsPerChannel = 16
    vr.output_rawExrCompression = 4
    plain_render, plain_vfb = scene.render, scene.vfbControl

    def vray_render(*args, **kw):
        bitmap = plain_render(*args, **kw)
        if vr.output_saveRawFile and vr.output_rawFileName:
            path = Path(vr.output_rawFileName)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(render.EXR_MAGIC + b"\x02\x00\x00\x00 fake multilayer exr\n")
        return bitmap

    def vray_vfb(*args):
        plain_vfb(*args)
        if len(args) >= 2 and str(args[0]).lower() == "saveimage":
            Path(args[1]).write_bytes(tiff(tif_bits))

    scene.render, scene.vfbControl = vray_render, vray_vfb
    return vr


# --------------------------------------------------------------------------- render_packet_frame


def test_render_packet_frame_writes_the_exr_and_the_tiff16(scene, tmp_path):
    vr = make_vray(scene)
    elements.ensure_default_set()
    out_exr, out_tif = tmp_path / "pkt" / "beauty.exr", tmp_path / "pkt" / "display.tif"
    result = render.render_packet_frame("CAM_main", out_exr, out_tif, 3840, 2160, 0)
    assert result.ok, result.warnings
    assert result.exr == str(out_exr) and result.tif == str(out_tif) and result.tif_bits == 16
    assert render.is_exr(out_exr) and render.tiff_bits_per_sample(out_tif) == 16
    call = scene.render_calls[-1]
    assert call["vfb"] is True and call["quiet"] is True and call["frame"] == 0
    assert call["camera"] is scene.cameras["CAM_main"]
    assert "outputfile" not in call  # Max's writer would clobber V-Ray's multilayer EXR
    assert result.element_layers == DEFAULT_LAYERS
    assert result.peak_rss_gb == 12.0 and result.time_s >= 0
    assert result.settings["output_saveRawFile"] is True
    assert result.settings["output_rawFileName"] == str(out_exr)
    assert result.settings["output_rawExrBitsPerChannel"] == 32
    assert result.settings["output_rawExrCompression"] == render.EXR_COMPRESSION_ZIP
    assert ("setsaveformat", "tif", 16) in [(str(c[0]), *c[1:]) for c in scene.vfb_calls]
    assert ("saveimage", str(out_tif)) in [(str(c[0]), *c[1:]) for c in scene.vfb_calls]
    # every renderer setting is back the way the artist had it
    assert (vr.output_saveRawFile, vr.output_rawFileName, vr.output_genpreview) == (False, "", True)
    assert (vr.output_splitgbuffer, vr.output_rawExrBitsPerChannel, vr.output_rawExrCompression) == (
        True,
        16,
        4,
    )
    assert result.as_dict()["exr"] == str(out_exr)


def test_the_render_size_is_set_for_the_frame_and_restored(scene, tmp_path):
    make_vray(scene)
    result = render.render_packet_frame(
        scene.cameras["CAM_main"], tmp_path / "b.exr", tmp_path / "d.tif", 1920, 1080, 5
    )
    assert result.ok
    call = scene.render_calls[-1]
    assert (call["outputwidth"], call["outputheight"], call["frame"]) == (1920, 1080, 5)
    assert (scene.renderWidth, scene.renderHeight) == (3840, 2160)


def test_an_8_bit_tiff_is_not_a_display_master(scene, tmp_path):
    make_vray(scene, tif_bits=8)
    result = render.render_packet_frame("CAM_main", tmp_path / "b.exr", tmp_path / "d.tif", 64, 36, 0)
    assert result.exr is not None and result.tif is None and result.tif_bits == 8
    assert any("8-bit TIFF" in w for w in result.warnings)


def test_a_vfb_that_saves_a_png_is_caught(scene, tmp_path):
    make_vray(scene)
    scene.vfbControl = type(scene).vfbControl.__get__(scene)  # the plain fake: writes a PNG
    result = render.render_packet_frame("CAM_main", tmp_path / "b.exr", tmp_path / "d.tif", 64, 36, 0)
    assert result.tif is None and any("not a TIFF" in w for w in result.warnings)


def test_a_renderer_without_raw_output_writes_no_exr(scene, tmp_path):
    result = render.render_packet_frame("CAM_main", tmp_path / "b.exr", tmp_path / "d.tif", 64, 36, 0)
    assert result.exr is None and not result.ok
    joined = " | ".join(result.warnings)
    assert "output_saveRawFile" in joined and "wrote no EXR" in joined


def test_a_stale_exr_never_passes_for_a_new_render(scene, tmp_path):
    out_exr = tmp_path / "b.exr"
    out_exr.write_bytes(render.EXR_MAGIC + b"old render\n")
    result = render.render_packet_frame("CAM_main", out_exr, tmp_path / "d.tif", 64, 36, 0)
    assert result.exr is None and not out_exr.exists()


def test_a_failed_render_raises_and_restores_the_settings(scene, tmp_path):
    vr = make_vray(scene)
    scene.render = lambda **kw: None
    with pytest.raises(render.RenderFailed, match="undefined"):
        render.render_packet_frame("CAM_main", tmp_path / "b.exr", tmp_path / "d.tif", 64, 36, 0)
    assert vr.output_saveRawFile is False and scene.renderWidth == 3840

    def boom(**kw):
        raise RuntimeError("V-Ray: out of memory")

    scene.render = boom
    with pytest.raises(render.RenderFailed, match="out of memory"):
        render.render_packet_frame("CAM_main", tmp_path / "b.exr", tmp_path / "d.tif", 64, 36, 0)


def test_bad_inputs(scene, tmp_path):
    with pytest.raises(render.RenderFailed, match="not found"):
        render.render_packet_frame("CAM_nope", tmp_path / "b.exr", tmp_path / "d.tif", 64, 36, 0)
    with pytest.raises(ValueError, match="':'"):
        render.render_packet_frame("CAM_main", tmp_path / "b 12:00.exr", tmp_path / "d.tif", 64, 36, 0)


def test_rss_is_none_without_sysinfo(scene, tmp_path):
    make_vray(scene)
    scene.sysInfo = None
    result = render.render_packet_frame("CAM_main", tmp_path / "b.exr", tmp_path / "d.tif", 64, 36, 0)
    assert result.ok and result.peak_rss_gb is None


@pytest.mark.parametrize("order", ["<", ">"])
@pytest.mark.parametrize("samples", [1, 2, 3, 4])
def test_tiff_bits_per_sample_reads_both_byte_orders_and_the_array_form(tmp_path, order, samples):
    path = tmp_path / "t.tif"
    path.write_bytes(tiff(16, order, samples))
    assert render.tiff_bits_per_sample(path) == 16
    path.write_bytes(tiff(8, order, samples))
    assert render.tiff_bits_per_sample(path) == 8


def test_tiff_and_exr_sniffers_refuse_other_files(tmp_path):
    from fake_pymxs import tiny_png

    png = tmp_path / "x.png"
    png.write_bytes(tiny_png())
    assert render.tiff_bits_per_sample(png) is None and not render.is_exr(png)
    assert render.tiff_bits_per_sample(tmp_path / "missing.tif") is None
    no_bits = tmp_path / "nobits.tif"
    no_bits.write_bytes(b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 0) + struct.pack("<I", 0))
    assert render.tiff_bits_per_sample(no_bits) == 1  # TIFF 6.0 default: bilevel


def test_element_layers_skip_disabled_elements(scene):
    elements.ensure_default_set()
    scene.element_manager.elements[1].enabled = False
    assert render.element_layers() == [layer for layer in DEFAULT_LAYERS if layer != "VRayNormals"]


# --------------------------------------------------------------------------- vantage


class FakeViewport:
    def __init__(self, camera=None):
        self.camera = camera
        self.history = []

    def setCamera(self, node):  # noqa: N802
        self.camera = node
        self.history.append(getattr(node, "name", node))

    def getCamera(self):  # noqa: N802
        return self.camera


def _second_camera(scene):
    from fake_pymxs import FakeCamera

    return scene.add_camera(FakeCamera("CAM_side"))


def test_export_vrscene_passes_the_frames_and_restores_the_viewport(scene, tmp_path):
    side = _second_camera(scene)
    scene.viewport = FakeViewport(side)
    out = vantage.export_vrscene(tmp_path / "vrscene" / "clubhouse-c03.vrscene", "CAM_main", 0, 24)
    assert out == str(tmp_path / "vrscene" / "clubhouse-c03.vrscene") and Path(out).is_file()
    call = scene.vrscene_exports[-1]
    assert (call["startFrame"], call["endFrame"], call["exportCompressed"]) == (0, 24, True)
    assert scene.viewport.history == ["CAM_main", "CAM_side"] and scene.viewport.camera is side


def test_a_still_export_uses_the_current_frame_for_both_ends(scene, tmp_path):
    scene.viewport = FakeViewport()
    scene.sliderTime = 17
    vantage.export_vrscene(tmp_path / "s.vrscene", scene.cameras["CAM_main"])
    call = scene.vrscene_exports[-1]
    assert (call["startFrame"], call["endFrame"]) == (17, 17)


def test_the_fallback_export_still_passes_the_frames(scene, tmp_path):
    scene.viewport = FakeViewport()
    plain = scene.vrayExportVRScene

    def picky(path, **kw):
        if "exportCompressed" in kw:
            raise RuntimeError("unknown keyword exportCompressed")
        return plain(path, **kw)

    scene.vrayExportVRScene = picky
    assert vantage.export_vrscene(tmp_path / "s.vrscene", "CAM_main", 3, 9)
    assert scene.vrscene_exports[-1] == {"path": str(tmp_path / "s.vrscene"), "startFrame": 3, "endFrame": 9}


def test_export_refusals(scene, tmp_path):
    scene.viewport = FakeViewport()
    assert vantage.export_vrscene(tmp_path / "s.vrscene", "CAM_nope") is None
    with pytest.raises(ValueError, match="backwards"):
        vantage.export_vrscene(tmp_path / "s.vrscene", "CAM_main", 10, 2)
    with pytest.raises(ValueError, match="':'"):
        vantage.export_vrscene(tmp_path / "a:b.vrscene", "CAM_main")
    scene.vrayExportVRScene = None  # no V-Ray exporter on this build
    assert vantage.export_vrscene(tmp_path / "s.vrscene", "CAM_main") is None
    del scene.viewport
    scene.vrayExportVRScene = type(scene).vrayExportVRScene.__get__(scene)
    assert vantage.export_vrscene(tmp_path / "s.vrscene", "CAM_main") is None  # no viewport to aim


def test_start_live_link_tries_globals_then_the_action_table(scene):
    ok, how = vantage.start_live_link()
    assert ok and how == f"maxscript global {vantage.LIVE_LINK_GLOBALS[0]}"
    assert scene.executed == [vantage.LIVE_LINK_GLOBALS[0]]

    def undefined(script):
        raise RuntimeError("Call needs function or class, got: undefined")

    scene.execute = undefined
    fired = []

    class Action:
        def __init__(self, text):
            self.text = text

        def getDescriptionText(self):  # noqa: N802
            return self.text

        def execute(self):
            fired.append(self.text)

    class Table:
        def __init__(self, texts):
            self.actions = [Action(t) for t in texts]
            self.numActions = len(texts)

        def getAction(self, i):  # noqa: N802 (1-based, as in Max)
            return self.actions[i - 1]

    class ActionMan:
        tables = [Table(["Render Setup"]), Table(["Open VFB", "Initiate a Live-Link to Chaos Vantage"])]
        numActionTables = 2

        def getActionTable(self, i):  # noqa: N802
            return self.tables[i - 1]

    scene.actionMan = ActionMan()
    ok, how = vantage.start_live_link()
    assert (
        ok
        and how == "actionMan: Initiate a Live-Link to Chaos Vantage"
        and fired == ["Initiate a Live-Link to Chaos Vantage"]
    )
    scene.actionMan = None
    ok, how = vantage.start_live_link()
    assert not ok and "no live-link entry point" in how


def test_start_live_link_off_max():
    rt.set_runtime(None)
    ok, how = vantage.start_live_link()
    assert not ok and "pymxs unavailable" in how


# --------------------------------------------------------------------------- safety


@pytest.fixture
def saved_scene(scene, tmp_path):
    max_file = tmp_path / "scenes" / "WOT_clubhouse_v12.max"
    max_file.parent.mkdir()
    max_file.write_bytes(b"MAX")
    scene.maxFilePath, scene.maxFileName = str(max_file.parent) + "/", max_file.name
    return scene, max_file


def test_backup_saves_a_sibling_copy_without_touching_the_open_file(saved_scene):
    scene, max_file = saved_scene
    path = safety.backup()
    assert path.parent == max_file.parent and path.is_file()
    assert path.name.startswith("WOT_clubhouse_v12.plate-backup.") and path.suffix == ".max"
    assert ":" not in path.name
    call = scene.save_calls[-1]
    assert (call["useNewFile"], call["quiet"], call["clearNeedSaveFlag"]) == (False, True, False)
    assert scene.maxFileName == "WOT_clubhouse_v12.max"  # still the client's file
    second = safety.backup()
    assert second != path and second.is_file()  # two in one second never overwrite each other


def test_an_untitled_scene_backs_up_to_the_temp_folder(scene, tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    path = safety.backup()
    assert path.parent == tmp_path and path.name.startswith("untitled.plate-backup.")


def test_a_failed_backup_raises(saved_scene):
    scene, _ = saved_scene
    scene.saveMaxFile = lambda path, **kw: False
    with pytest.raises(safety.BackupFailed, match="nothing was changed"):
        safety.backup()


class FakeHold:
    def __init__(self, accept_fails=False):
        self.calls = []
        self.accept_fails = accept_fails

    def Begin(self):  # noqa: N802
        self.calls.append("Begin")

    def Accept(self, label):  # noqa: N802
        self.calls.append(f"Accept:{label}")
        if self.accept_fails:
            raise RuntimeError("accept failed")

    def Cancel(self):  # noqa: N802
        self.calls.append("Cancel")


def test_one_undo_accepts_once_and_cancels_exactly_its_hold(scene):
    scene.theHold = FakeHold()
    with safety.one_undo("PLATE send"):
        pass
    assert scene.theHold.calls == ["Begin", "Accept:PLATE send"]
    scene.theHold = FakeHold()
    with pytest.raises(KeyError), safety.one_undo():
        raise KeyError("boom")
    assert scene.theHold.calls == ["Begin", "Cancel"]
    scene.theHold = FakeHold(accept_fails=True)
    with pytest.raises(RuntimeError, match="accept failed"), safety.one_undo("x"):
        pass
    assert scene.theHold.calls == ["Begin", "Accept:x", "Cancel"]


def test_one_undo_refuses_without_the_hold(scene):
    with pytest.raises(RuntimeError, match="theHold"), safety.one_undo():
        pass


def test_journal_writes_as_it_goes_under_app_data(scene, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))
    journal = safety.Journal("D:/jobs/WOT.max", "D:/jobs/WOT.plate-backup.x.max", op="send")
    assert journal.path.parent == tmp_path / "home" / "logs" / "journal"
    assert ":" not in journal.path.name
    journal.record("render", "CAM_main", "ok", "1874 s")
    on_disk = json.loads(journal.path.read_text(encoding="utf-8"))
    assert on_disk["steps"] == [{"step": "render", "target": "CAM_main", "status": "ok", "detail": "1874 s"}]
    journal.finish("done")
    on_disk = json.loads(journal.path.read_text(encoding="utf-8"))
    assert on_disk["finished"] == "done" and on_disk["backup"].endswith(".max")
    other = safety.Journal(None, None)
    assert other.path != journal.path


def test_journal_write_failures_never_stop_the_operation(scene, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_HOME", str(tmp_path / "home"))

    def refuse(*a, **kw):
        raise PermissionError("read-only share")

    monkeypatch.setattr(safety, "atomic_write_json", refuse)
    journal = safety.Journal(None, None)
    journal.record("x")
    journal.finish("done")
    assert journal.data["finished"] == "done"
