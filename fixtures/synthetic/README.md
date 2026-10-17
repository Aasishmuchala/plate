# fixtures/synthetic

Numpy generators, no committed files. Import from the repo root (pytest puts it on `pythonpath`):

```python
from fixtures.synthetic import make_pair, make_depth_scene, shift_image, laplacian_variance, KINDS
```

`make_pair(kind, size=(256, 256), seed=0)` returns `(ref, test, mask, expected)`; `size` is `(width, height)`.
`KINDS`: `identity`, `shift3`, `window_added`, `material_shift_de5`, `texture_crunch`, `feather_leak`. The module
docstring in `__init__.py` is the contract for each kind (what changed, what `mask` means, which ground truth is in
`expected`). Everything is deterministic in `(kind, size, seed)`.

`make_depth_scene(size=(256, 256))` returns `(z, normals_cam, ids, K, world_from_camera)`: a pinhole camera in the
plate-cam-v0 convention looking at a wall (4.0 m planar depth) and a box (front face at 2.5 m, left side visible).
The returned object is a tuple subclass that also carries `.image` (a 3x3 supersampled render of the same scene),
`.edges` (the analytic edge map), `.labels`, `.wall_id`, `.box_id`, `.wall_mask`, `.box_rect`, `.camera` (accepted by
`plate_spec.project_point`) and `.size`.

`_color.py` holds the sRGB <-> Lab and CIEDE2000 used to build and measure the `material_shift_de5` pair. It is
deliberately independent of `plate_gates.color` (a fixture must not import the code it tests) and is checked
against the published Sharma, Wu and Dalal vectors in `tests/test_synthetic.py`.
