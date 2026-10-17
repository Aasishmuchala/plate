"""Perceptual-hash index and drop-to-resolve (T26)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from plate_daemon import lineage as L
from plate_daemon import phash
from plate_daemon.cas import CAS, hash_file
from plate_daemon.db import open_index
from plate_daemon.index import Index
from plate_daemon.phash import PhashError, PhashIndex, resolve_drop

PROJECT = "proj-a"


def scene(seed: int, size: tuple[int, int] = (640, 360)) -> Image.Image:
    """A structured test picture: a colour gradient with random boxes and ellipses and a little sensor noise."""
    rng = np.random.default_rng(seed)
    w, h = size
    y = np.linspace(0, 1, h)[:, None, None]
    x = np.linspace(0, 1, w)[None, :, None]
    base = np.concatenate(
        [
            y * rng.random() + x * rng.random(),
            (1 - y) * rng.random() + x * 0.3,
            y * 0.4 + (1 - x) * rng.random(),
        ],
        axis=2,
    )
    img = Image.fromarray((np.clip(base, 0, 1) * 200 + 20).astype(np.uint8))
    draw = ImageDraw.Draw(img)
    for _ in range(9):
        x0, y0 = int(rng.integers(0, w - 80)), int(rng.integers(0, h - 60))
        box = [x0, y0, x0 + int(rng.integers(40, 260)), y0 + int(rng.integers(30, 180))]
        fill = tuple(int(v) for v in rng.integers(0, 255, 3))
        (draw.rectangle if rng.random() < 0.5 else draw.ellipse)(box, fill=fill)
    noisy = np.asarray(img).astype(np.float32) + rng.normal(0, 4, (h, w, 3))
    return Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8))


def stripped_jpeg(src: Image.Image, path: Path, *, scale: float = 0.6, quality: int = 70) -> Path:
    """What a screenshot or a re-saved preview looks like: smaller, lossy, no metadata."""
    small = src.resize((int(src.width * scale), int(src.height * scale)), Image.Resampling.BICUBIC)
    small.save(path, quality=quality)
    return path


@pytest.fixture
def pictures(tmp_path):
    imgs = [scene(s) for s in range(6)]
    paths = []
    for i, im in enumerate(imgs):
        p = tmp_path / f"pic{i}.png"
        im.save(p)
        paths.append(p)
    return imgs, paths


KEY = [f"{i:064x}" for i in range(1, 9)]


# ---------------------------------------------------------------------------- hashing


def test_compute_is_64_hex_characters_and_stable(pictures):
    _, paths = pictures
    h = phash.compute(paths[0])
    assert len(h) == 64 and set(h) <= set("0123456789abcdef")
    assert phash.compute(paths[0]) == h
    assert phash.compute(str(paths[0])) == h
    assert phash.distance(h, h) == 0
    assert phash.distance(h, phash.compute(paths[1])) > 60  # different pictures are far apart


def test_distance_validates_its_inputs():
    a, b = "0" * 64, "f" * 63 + "e"
    assert phash.distance(a, b) == 255
    for bad in ("", "0" * 63, "G" * 64, "F" * 64, None, 5):
        with pytest.raises(PhashError):
            phash.distance(a, bad)


def test_a_stripped_jpeg_derivative_is_within_twelve_bits_of_its_original(pictures, tmp_path):
    imgs, paths = pictures
    hashes = [phash.compute(p) for p in paths]
    for i, im in enumerate(imgs):
        for scale, quality in ((0.6, 70), (0.4, 50), (1.0, 85)):
            jpg = stripped_jpeg(im, tmp_path / f"d{i}.jpg", scale=scale, quality=quality)
            d = phash.distance(phash.compute(jpg), hashes[i])
            assert d <= phash.DEFAULT_MAX_DISTANCE, (i, scale, quality, d)
            others = [phash.distance(phash.compute(jpg), hashes[j]) for j in range(len(imgs)) if j != i]
            assert min(others) > 2 * phash.DEFAULT_MAX_DISTANCE


def test_thumbnail_is_never_enlarged_and_big_images_shrink(tmp_path):
    big = scene(1, (1600, 900))
    small = big.resize((200, 112), Image.Resampling.BICUBIC)
    p_big, p_small = tmp_path / "big.png", tmp_path / "small.png"
    big.save(p_big)
    small.save(p_small)
    assert phash._thumb(p_big).width == phash.THUMB_WIDTH
    assert phash._thumb(p_small).width == 200
    assert phash.distance(phash.compute(p_big), phash.compute(p_small)) <= phash.DEFAULT_MAX_DISTANCE


def test_16bit_and_float_sources_hash_like_their_8bit_pngs(pictures, tmp_path):
    import OpenEXR
    import tifffile

    imgs, paths = pictures
    srgb8 = np.asarray(imgs[2])
    ref = phash.compute(paths[2])

    tif = tmp_path / "deep.tif"
    tifffile.imwrite(tif, srgb8.astype(np.uint16) * 257, photometric="rgb", compression="zlib", metadata=None)
    assert phash.distance(phash.compute(tif), ref) <= 4

    srgb = srgb8.astype(np.float32) / 255.0
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4).astype(np.float32)
    exr = tmp_path / "beauty.exr"
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage},
        {"RGB": linear},
    ).write(str(exr))
    assert phash.distance(phash.compute(exr), ref) <= 4

    gray = tmp_path / "gray16.png"
    Image.fromarray(np.asarray(imgs[2].convert("L")).astype(np.uint16) * 257).save(gray)
    assert (
        phash.distance(phash.compute(gray), phash.compute(_save(imgs[2].convert("L"), tmp_path / "g8.png")))
        <= 4
    )


def _save(img: Image.Image, path: Path) -> Path:
    img.save(path)
    return path


def test_things_that_are_not_images_raise_phash_error(tmp_path):
    txt = tmp_path / "notes.png"
    txt.write_text("not an image\n", encoding="utf-8")
    with pytest.raises(PhashError, match="cannot read"):
        phash.compute(txt)
    with pytest.raises(PhashError):
        phash.compute(tmp_path / "missing.jpg")
    with pytest.raises(PhashError):
        phash.compute(tmp_path / "clip.mp4")
    only_z = tmp_path / "z.exr"
    import OpenEXR

    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage},
        {"Z": np.ones((4, 4), np.float32)},
    ).write(str(only_z))
    with pytest.raises(PhashError, match="no R, G, B or Y"):
        phash.compute(only_z)


# ---------------------------------------------------------------------------- the index (memory)


def flip(h: str, bits: int) -> str:
    """`h` with its lowest `bits` bits flipped."""
    return f"{int(h, 16) ^ ((1 << bits) - 1):064x}"


def test_memory_index_orders_by_distance_then_content_hash():
    base = "a5" * 32
    idx = PhashIndex()
    idx.add(base, KEY[0])
    idx.add(flip(base, 3), KEY[1])
    idx.add(flip(base, 3), KEY[2])  # same distance: content hash decides
    idx.add(flip(base, 12), KEY[3])
    idx.add(flip(base, 13), KEY[4])  # just outside the default bound
    assert idx.in_memory
    assert idx.nearest(base) == [(KEY[0], 0), (KEY[1], 3), (KEY[2], 3), (KEY[3], 12)]
    assert idx.nearest(base, max_distance=3) == [(KEY[0], 0), (KEY[1], 3), (KEY[2], 3)]
    assert idx.nearest(base, max_distance=0) == [(KEY[0], 0)]
    assert idx.nearest(flip(base, 200)) == []
    idx.add(flip(base, 5), KEY[0])  # re-adding replaces
    assert idx.get(KEY[0]) == flip(base, 5) and idx.nearest(base, 4)[0] == (KEY[1], 3)
    assert idx.has_content(KEY[0]) and not idx.has_content(KEY[7]) and idx.get(KEY[7]) is None
    assert idx.run_for(KEY[0]) is None


def test_index_rejects_malformed_arguments():
    idx = PhashIndex()
    with pytest.raises(PhashError):
        idx.add("xyz", KEY[0])
    with pytest.raises(Exception, match="blake3"):
        idx.add("0" * 64, "short")
    with pytest.raises(PhashError):
        idx.nearest("0" * 10)
    with pytest.raises(TypeError, match="project_id"):
        PhashIndex(Index(Path(".")))
    with pytest.raises(TypeError):
        PhashIndex(object())


# ---------------------------------------------------------------------------- the index (SQLite)


@pytest.fixture
def index(plate_env):
    return Index(plate_env)


def test_sqlite_index_stores_the_hash_in_the_assets_table(index):
    base = "5a" * 32
    index.record_asset(PROJECT, KEY[0], "png", 10)
    index.record_asset(PROJECT, KEY[1], "png", 10)
    ph = PhashIndex(index, PROJECT)
    assert not ph.in_memory
    ph.add(base, KEY[0])
    ph.add(flip(base, 4), KEY[1])
    assert ph.get(KEY[0]) == base and ph.get(KEY[2]) is None
    assert ph.nearest(base) == [(KEY[0], 0), (KEY[1], 4)]
    assert ph.nearest(flip(base, 9), 5) == [(KEY[1], 5)]
    assert ph.has_content(KEY[1]) and not ph.has_content(KEY[2])
    with index.project(PROJECT) as conn:  # the same column the CAS hook fills in
        assert conn.execute("SELECT phash FROM assets WHERE hash = ?", (KEY[1],)).fetchone()[0] == flip(
            base, 4
        )
    with pytest.raises(KeyError, match="record_asset"):
        ph.add(base, KEY[5])
    with index.project(PROJECT) as conn:
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 2  # no phantom row


def test_a_bare_connection_works_and_survives_a_rebuild_from_the_folder(index, tmp_path):
    conn = open_index(tmp_path / "bare.sqlite")
    try:
        conn.execute(
            "INSERT INTO assets (hash, ext, bytes, added_utc) VALUES (?, 'png', 1, '2026-10-17T00:00:00.000000Z')",
            (KEY[0],),
        )
        ph = PhashIndex(conn)
        ph.add("0f" * 32, KEY[0])
        assert ph.nearest("0f" * 32) == [(KEY[0], 0)]
        conn.execute("UPDATE assets SET phash = 'not-a-hash' WHERE hash = ?", (KEY[0],))
        assert ph.nearest("0f" * 32) == []  # a row some other writer left is skipped, not a crash
    finally:
        conn.close()
    assert isinstance(sqlite3.connect(":memory:"), sqlite3.Connection)


def test_run_for_follows_the_run_that_produced_the_content(index):
    index.record_asset(PROJECT, KEY[0], "png", 10)
    with index.project(PROJECT) as conn:
        conn.execute(
            "INSERT INTO packets (packet_id, project_id, shot_id, kind, tier, packet_version, path, manifest_blake3, "
            "status, received_utc) VALUES ('pkt1', ?, 's1', 'still', 'A', '0.1.0', 'p', ?, 'ingested', '2026-10-17T00:00:00.000000Z')",
            (PROJECT, KEY[7]),
        )
        for run_id, created in (
            ("run_old", "2026-10-17T01:00:00.000000Z"),
            ("run_new", "2026-10-17T02:00:00.000000Z"),
        ):
            conn.execute(
                "INSERT INTO runs (id, project_id, packet_id, state, priority, inputs, params, route, attempt, "
                "result_key, created_utc, updated_utc) VALUES (?, ?, 'pkt1', 'DRAFT', 'interactive', '{}', '{}', '{}', 1, ?, ?, ?)",
                (run_id, PROJECT, KEY[0], created, created),
            )
    ph = PhashIndex(index, PROJECT)
    assert ph.run_for(KEY[0]) == "run_new"
    assert ph.run_for(KEY[1]) is None


# ---------------------------------------------------------------------------- drop to resolve


def _store(project_dir: Path, index: Index, src: Path, ph: PhashIndex) -> str:
    """Put a file in the project's CAS the way the daemon does, and index its perceptual hash."""
    cas = CAS(project_dir / "assets", on_put=lambda k, e, n: index.record_asset(PROJECT, k, e, n))
    key = cas.put_file(src)
    ph.add_file(src, key)
    return key


def test_resolve_drop_tries_lineage_then_content_hash_then_phash(index, pictures, tmp_path):
    imgs, paths = pictures
    ph = PhashIndex(index, PROJECT)
    project_dir = tmp_path / "project"

    # The run's output: a PNG that carries its lineage. The CAS stores what was delivered.
    delivered = tmp_path / "delivered.png"
    imgs[0].save(delivered)
    lin = L.Lineage(PROJECT, "node_3", "run_77", "pkt_1", "nano-banana-2.1", "2.1.3")
    L.embed(delivered, lin)
    key = _store(project_dir, index, delivered, ph)
    with index.project(PROJECT) as conn:
        conn.execute(
            "INSERT INTO packets (packet_id, project_id, shot_id, kind, tier, packet_version, path, manifest_blake3, "
            "status, received_utc) VALUES ('pkt_1', ?, 's1', 'still', 'A', '0.1.0', 'p', ?, 'ingested', '2026-10-17T00:00:00.000000Z')",
            (PROJECT, "9" * 64),
        )
        conn.execute(
            "INSERT INTO runs (id, project_id, packet_id, state, priority, inputs, params, route, attempt, result_key, "
            "created_utc, updated_utc) VALUES ('run_77', ?, 'pkt_1', 'DRAFT', 'interactive', '{}', '{}', '{}', 1, ?, "
            "'2026-10-17T01:00:00.000000Z', '2026-10-17T01:00:00.000000Z')",
            (PROJECT, key),
        )

    # 1. the file itself: lineage wins (even though its bytes are also a stored asset)
    assert resolve_drop(delivered, ph) == {
        "by": "lineage",
        "run_id": "run_77",
        "project_id": PROJECT,
        "node_id": "node_3",
        "content_hash": key,
    }

    # 2. the same picture with its lineage gone but the bytes of a stored asset: exact content hash
    imgs[1].save(paths[1])
    key1 = _store(project_dir, index, paths[1], ph)
    assert resolve_drop(paths[1], ph) == {"by": "content_hash", "content_hash": key1}
    copy = tmp_path / "renamed copy.png"
    copy.write_bytes(delivered.read_bytes())
    assert resolve_drop(copy, ph)["by"] == "lineage"

    # 3. a stripped JPEG of a stored picture: perceptual hash, with the run that made it
    jpg = stripped_jpeg(imgs[0], tmp_path / "screenshot.jpg")
    got = resolve_drop(jpg, ph)
    assert got["by"] == "phash" and got["content_hash"] == key and got["run_id"] == "run_77"
    assert 0 < got["distance"] <= 12
    jpg1 = stripped_jpeg(imgs[1], tmp_path / "screenshot1.jpg", scale=0.4, quality=50)
    got1 = resolve_drop(jpg1, ph)
    assert got1["by"] == "phash" and got1["content_hash"] == key1 and "run_id" not in got1

    # 4. nothing stored resembles it, or it is not an image
    stranger = tmp_path / "stranger.png"
    scene(99).save(stranger)
    assert resolve_drop(stranger, ph) is None
    notes = tmp_path / "notes.txt"
    notes.write_text("hello\n", encoding="utf-8")
    assert resolve_drop(notes, ph) is None
    assert resolve_drop(jpg, ph, max_distance=0) is None  # the bound is the caller's to tighten


def test_resolve_drop_in_memory_and_with_a_hostile_lineage(pictures, tmp_path):
    imgs, paths = pictures
    ph = PhashIndex()
    h0, k0 = phash.compute(paths[0]), hash_file(paths[0])[0]
    ph.add(h0, k0)
    assert resolve_drop(paths[0], ph) == {"by": "content_hash", "content_hash": k0}
    got = resolve_drop(stripped_jpeg(imgs[0], tmp_path / "s.jpg"), ph)
    assert (got["by"], got["content_hash"]) == ("phash", k0)

    # A file claiming another project's run is reported as its own claim; the caller checks project_id
    foreign = tmp_path / "foreign.png"
    imgs[3].save(foreign)
    L.embed(foreign, L.Lineage("other-project", "n9", "run_x", "pkt_x", "gpt-image-2.5"))
    got = resolve_drop(foreign, ph)
    assert (got["by"], got["project_id"], got["run_id"]) == ("lineage", "other-project", "run_x")
