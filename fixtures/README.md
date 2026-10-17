# fixtures

Everything the gate, pixel, daemon and plugin tests read as input. Nothing here is client data: client plates
under `/Users/aasish/Desktop/test` are referenced by absolute path from the tests that need them and skipped when
absent (`plate_gates.tests.clientdata`), never copied into the repo.

| folder | what | committed |
|---|---|---|
| `packets/` | six Blender-rendered Tier A Shot Packets (still, two cameras, a revision, a recolour variant, a 12-frame dolly) | yes, 17 MB |
| `blender/` | the Blender 5.1 fixture renderer (`scene.py`), the repack into packets (`build_packets.py`), `markers.json` | yes (code), `_work/` is gitignored |
| `synthetic/` | numpy generators: `make_pair(kind)` and `make_depth_scene()` | code only, no files |
| `tests/` | validity, geometry, budget and generator tests for all of the above | yes |

## Using them

```python
import plate_spec as ps
from fixtures.synthetic import make_depth_scene, make_pair  # the repo root is on pytest's pythonpath

ref, test, mask, expected = make_pair("material_shift_de5")
z, normals_cam, ids, K, world_from_camera = make_depth_scene()
ps.validate_packet_dir("fixtures/packets/room_a", check_files=True)  # -> []
```

The packets are real Shot Packets (spec 0.1.1): `manifest.json`, a single-part multilayer `beauty.exr` with V-Ray
layer names, a 16-bit `display.tif` master and `id_legend.json`. See `packets/README.md` for the table and
`blender/README.md` for how they are made and why they look the way they do.

## Regenerating

```
bash fixtures/blender/render.sh              # macOS / Linux; needs Blender 5.1 (BLENDER=... to point at it)
.venv\Scripts\python fixtures\blender\render.py   # Windows: the driver behind render.sh, same arguments
```

About 65 s for all six packets on an Apple-silicon CPU. Idempotent: a variant is re-rendered only when
`scene.py` or `markers.json` changed; the repack always runs and is byte-for-byte reproducible
(`tests/test_blender_scripts.py` proves it against the committed packets).

## Budget

`fixtures/packets` is 17 MB (limit 25 MB). Each packet is at most 3 MB zipped, except `room_a` and
`room_a_ral3020`, which carry three extra RGB layers (the opt-in raw lighting, raw GI and reflection elements the
recolour validation needs) and measure 3.7 MB; `tests/test_packets_valid.py::test_fixture_packets_fit_the_size_budget`
pins both figures. The budget is met with a documented lossy quantisation of the float layers (see
`blender/README.md`), never by shrinking resolution.

## Rules

- Blender is a fixture renderer only (BUILD_PLAN decision 10); there is no Blender sender.
- Cycles cannot stand in for V-Ray behaviour. Anything about V-Ray, Vantage or colour management on the render box
  is a `verify-on-box` item, not something these files can prove.
- Do not edit a committed packet by hand: change `scene.py` or `build_packets.py` and regenerate.
