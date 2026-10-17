# On-box spike answers

The answers to the checklist in `plugins/max/ONBOX.md`, filled in on the render box (3ds Max 2026, V-Ray 7, Vantage 3.3). Colour answers go in [colour.md](colour.md). Until a row has an answer, the constant it names in the plugin is a hypothesis, marked as unverified in the code (`grep -rn "\-verify" plugins/max/plate_max` lists them).

| step | what is asked | answer | versions and date | who |
|---|---|---|---|---|
| 1 | property names: every candidate tuple in `digest.py` and `bridge/*.py` | not run | | |
| 2 | VRayZDepth: planar or radial, raw or normalised | not run | | |
| 3 | VRayNormals: space and sign | not run | | |
| 4 | lens shift, tilt and the principal point | not run | | |
| 5 | element overhead (caps 15% time, 10% RSS) | not run | | |
| 6 | the two files: EXR channels and the VFB TIFF16 | not run | | |
| 7 | Vantage: elements, live link, export | not run | | |
