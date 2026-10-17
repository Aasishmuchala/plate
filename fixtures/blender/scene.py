"""PLATE fixture renderer. Runs INSIDE Blender 5.1 (bpy), never in the project venv:

    Blender -b --factory-startup -P fixtures/blender/scene.py -- <out_dir> [--variant base|ral3020|v2] [--quick]
                                                                  [--only shot[,shot]]

Builds the 5 x 4 x 3 m conformance room (markers from markers.json, glass pane, lettered sign, two RAL
panels, foliage proxy, floor, walls, emissive sky backdrop, people dummy, pool), renders the shots of
the chosen variant with Cycles on the CPU and writes raw Blender outputs under <out_dir>/<variant>/<shot>/.
build_packets.py (run in the venv) repacks those into V-Ray-layer-named Shot Packets.

This is a FIXTURE RENDERER, not a sender (decision 10): it exists so every gate on a Mac exercises the file
layout the Max plugin writes. Nothing here is client data. Per shot it writes:

    ml.exr       Blender multilayer EXR, 32-bit, every pass (Combined, Depth, Normal(world), Diffuse*, Glossy*,
                 CryptoObject/Material 00..02), pixel filter 1.5 px, depth of field on for CAM_a
    zu.exr       Depth only, 1 sample, pixel filter 0.01 px and no depth of field: the UNFILTERED Z the
                 manifest declares (V-Ray's VRayZDepth is unfiltered; Cycles filters its Depth pass)
    display.tif  16-bit TIFF through the Standard view transform, saved from the same render as ml.exr
    info.json    camera, resolution, frames, file names

Shot variants: base renders room_a, room_b, dolly_hero and dolly_12f (12 clay frames); ral3020 re-renders
CAM_a with the ARCH_panel_7016 albedo set to RAL 3020 (#CC0605); v2 re-renders CAM_a with the ARCH_wall_E
material recoloured (a client revision, used by the supersedes/carry-over tests).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Vector

HERE = Path(__file__).resolve().parent
ROOM_X, ROOM_Y, ROOM_Z = 5.0, 4.0, 3.0  # x -2.5..2.5, y -2..2, z 0..3 (metres, Z up)
SAMPLES = 32
SENSOR_WIDTH_MM = 36.0
RAL_7016 = "#383E42"
RAL_3020 = "#CC0605"

# camera name -> placement; DOF only on CAM_a (f/2.8 at 2.5 m) so the CoC weighting has something to weigh
CAMERAS = {
    "CAM_a": dict(
        loc=(-1.30, -1.85, 1.50), target=(0.55, 2.0, 1.24), lens=35.0, dof=True, fstop=2.8, focus=2.5
    ),
    "CAM_b": dict(
        loc=(1.90, -1.75, 1.50), target=(-0.40, 2.0, 1.20), lens=24.0, dof=False, fstop=8.0, focus=4.0
    ),
    "CAM_dolly": dict(
        loc=(0.10, -1.90, 1.45), target=(0.10, 2.0, 1.30), lens=28.0, dof=False, fstop=8.0, focus=4.0
    ),
}
DOLLY_FRAMES = 12
DOLLY_TRAVEL_M = 0.6  # frame 0 -> frame 11, along the camera's forward axis

SHOTS = {
    "base": [
        dict(shot="room_a", cam="CAM_a", res=(640, 360), frames=[0]),
        dict(shot="room_b", cam="CAM_b", res=(640, 360), frames=[0]),
        dict(shot="dolly_hero", cam="CAM_dolly", res=(640, 360), frames=[0]),
        dict(shot="dolly_12f", cam="CAM_dolly", res=(320, 180), frames=list(range(DOLLY_FRAMES)), clay=True),
    ],
    "ral3020": [dict(shot="room_a_ral3020", cam="CAM_a", res=(640, 360), frames=[0])],
    "v2": [dict(shot="room_a_v2", cam="CAM_a", res=(640, 360), frames=[0])],
}


# --------------------------------------------------------------------------- small helpers


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_linear(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(srgb_to_linear(int(h[i : i + 2], 16) / 255.0) for i in (0, 2, 4))  # type: ignore[return-value]


def r6(x) -> float:
    return round(float(x), 6)


class Records:
    """What the packet digests hash: one dict per object and per material, plus global state."""

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.materials: dict[str, dict] = {}
        self.globals: dict = {}


REC = Records()


# --------------------------------------------------------------------------- materials


def _clear_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _nodes(mat):
    if not mat.use_nodes:
        mat.use_nodes = True
    return mat.node_tree, mat.node_tree.nodes, mat.node_tree.links


def _set(socket_owner, names, value) -> None:
    for n in names if isinstance(names, (list, tuple)) else [names]:
        if n in socket_owner.inputs:
            socket_owner.inputs[n].default_value = value
            return
    raise KeyError(names)


def new_principled(name, base_hex, rough=0.5, metallic=0.0, spec=0.5, params=None):
    """Returns the material; its Principled node is mat.node_tree.nodes['Principled BSDF']."""
    mat = bpy.data.materials.new(name)
    _nt, nodes, _links = _nodes(mat)
    bsdf = nodes["Principled BSDF"]
    rgb = hex_linear(base_hex)
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metallic
    _set(bsdf, ["Specular IOR Level"], spec)
    REC.materials[name] = dict(
        kind="principled",
        base_srgb=base_hex.lower(),
        roughness=r6(rough),
        metallic=r6(metallic),
        specular=r6(spec),
        **(params or {}),
    )
    return mat


def mat_noise_tint(mat, scale=60.0, strength=0.10, detail=8.0):
    """Subtle 3D noise on the albedo: fine detail for the texture and edge gates, orientation independent."""
    _nt, nodes, links = _nodes(mat)
    bsdf = nodes["Principled BSDF"]
    coord = nodes.new("ShaderNodeTexCoord")
    noise = nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = scale
    noise.inputs["Detail"].default_value = detail
    links.new(coord.outputs["Object"], noise.inputs["Vector"])
    base = bsdf.inputs["Base Color"].default_value
    mix = nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "MULTIPLY"
    mix.inputs["Fac"].default_value = strength * 2.0
    mix.inputs["Color1"].default_value = base
    links.new(noise.outputs["Fac"], mix.inputs["Color2"])
    links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    REC.materials[mat.name]["noise"] = dict(scale=scale, strength=strength, detail=detail)


def mat_floor():
    mat = new_principled(
        "HARD_floor", "#59606A", rough=0.55, spec=0.35, params=dict(pattern="brick 0.20x0.10 m")
    )
    _nt, nodes, links = _nodes(mat)
    bsdf = nodes["Principled BSDF"]
    coord = nodes.new("ShaderNodeTexCoord")
    brick = nodes.new("ShaderNodeTexBrick")
    brick.inputs["Scale"].default_value = 1.0
    brick.inputs["Brick Width"].default_value = 0.20
    brick.inputs["Row Height"].default_value = 0.10
    brick.inputs["Mortar Size"].default_value = 0.008
    brick.inputs["Mortar Smooth"].default_value = 0.2
    brick.inputs["Bias"].default_value = 0.0
    brick.inputs["Color1"].default_value = (*hex_linear("#3F454D"), 1.0)
    brick.inputs["Color2"].default_value = (*hex_linear("#6B727C"), 1.0)
    brick.inputs["Mortar"].default_value = (*hex_linear("#B7B1A4"), 1.0)
    links.new(coord.outputs["Object"], brick.inputs["Vector"])
    links.new(brick.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def mat_glass():
    mat = bpy.data.materials.new("GLZ_pane")
    _nt, nodes, _links = _nodes(mat)
    bsdf = nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.95, 0.98, 0.97, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.0
    _set(bsdf, "IOR", 1.45)
    _set(bsdf, "Transmission Weight", 1.0)
    REC.materials["GLZ_pane"] = dict(
        kind="glass", ior=1.45, transmission=1.0, roughness=0.0, tint_srgb="#f2faf8"
    )
    return mat


def mat_sky():
    mat = bpy.data.materials.new("SKY_dome")
    nt, nodes, links = _nodes(mat)
    for n in list(nodes):
        nodes.remove(n)
    out = nodes.new("ShaderNodeOutputMaterial")
    emit = nodes.new("ShaderNodeEmission")
    geo = nodes.new("ShaderNodeNewGeometry")
    sep = nodes.new("ShaderNodeSeparateXYZ")
    ramp = nodes.new("ShaderNodeValToRGB")
    noise = nodes.new("ShaderNodeTexNoise")
    cloud = nodes.new("ShaderNodeValToRGB")
    mix = nodes.new("ShaderNodeMixRGB")
    maprange = nodes.new("ShaderNodeMapRange")
    links.new(geo.outputs["Position"], sep.inputs["Vector"])
    maprange.inputs["From Min"].default_value = -2.0
    maprange.inputs["From Max"].default_value = 4.5
    links.new(sep.outputs["Z"], maprange.inputs["Value"])
    ramp.color_ramp.elements[0].color = (*hex_linear("#D7E4F2"), 1.0)
    ramp.color_ramp.elements[1].color = (*hex_linear("#4C86D6"), 1.0)
    links.new(maprange.outputs["Result"], ramp.inputs["Fac"])
    noise.inputs["Scale"].default_value = 0.22
    noise.inputs["Detail"].default_value = 6.0
    links.new(geo.outputs["Position"], noise.inputs["Vector"])
    cloud.color_ramp.elements[0].position = 0.50
    cloud.color_ramp.elements[1].position = 0.68
    links.new(noise.outputs["Fac"], cloud.inputs["Fac"])
    mix.inputs["Color2"].default_value = (1.0, 1.0, 1.0, 1.0)
    links.new(cloud.outputs["Color"], mix.inputs["Fac"])
    links.new(ramp.outputs["Color"], mix.inputs["Color1"])
    links.new(mix.outputs["Color"], emit.inputs["Color"])
    emit.inputs["Strength"].default_value = 1.6
    links.new(emit.outputs["Emission"], out.inputs["Surface"])
    REC.materials["SKY_dome"] = dict(kind="emission", strength=1.6, ramp="D7E4F2-4C86D6", clouds=True)
    return mat


def mat_clay():
    mat = bpy.data.materials.new("PLATE_clay_matte_0.7")
    _nt, nodes, _links = _nodes(mat)
    bsdf = nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.7, 0.7, 0.7, 1.0)
    bsdf.inputs["Roughness"].default_value = 1.0
    _set(bsdf, "Specular IOR Level", 0.0)
    return mat


# --------------------------------------------------------------------------- geometry


def _link(obj, collection=None):
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def mesh_object(name, verts, faces, material, smooth=False):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(v) for v in verts], [], [tuple(f) for f in faces])
    mesh.update()
    if smooth:
        mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    obj = bpy.data.objects.new(name, mesh)
    _link(obj)
    if material is not None:
        obj.data.materials.append(material)
    return obj


def quad(p0, p1, p2, p3):
    return [p0, p1, p2, p3], [(0, 1, 2, 3)]


def bm_to_object(name, bm, material, smooth=True):
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    if smooth:
        mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    obj = bpy.data.objects.new(name, mesh)
    _link(obj)
    obj.data.materials.append(material)
    return obj


def uv_sphere(bm, center, radius, u=16, v=8):
    mat = Matrix.Translation(center)
    try:
        bmesh.ops.create_uvsphere(bm, u_segments=u, v_segments=v, radius=radius, matrix=mat)
    except TypeError:
        bmesh.ops.create_uvsphere(bm, u_segments=u, v_segments=v, diameter=radius, matrix=mat)


def ico_sphere(bm, center, radius, subdiv=2):
    mat = Matrix.Translation(center)
    try:
        bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=radius, matrix=mat)
    except TypeError:
        bmesh.ops.create_icosphere(bm, subdivisions=subdiv, diameter=radius, matrix=mat)


def box(bm, center, size, rot_z=0.0):
    mat = (
        Matrix.Translation(center)
        @ Matrix.Rotation(rot_z, 4, "Z")
        @ Matrix.Diagonal(Vector((size[0], size[1], size[2], 1.0)))
    )
    bmesh.ops.create_cube(bm, size=1.0, matrix=mat)


def lathe(profile, segments=24):
    """Revolve a (radius, z) profile around the Z axis: capsule, vase. Returns verts, faces."""
    verts, faces = [], []
    for r, z in profile:
        for s in range(segments):
            a = 2 * math.pi * s / segments
            verts.append((r * math.cos(a), r * math.sin(a), z))
    for ring_i in range(len(profile) - 1):
        for s in range(segments):
            a = ring_i * segments + s
            b = ring_i * segments + (s + 1) % segments
            faces.append((a, b, b + segments, a + segments))
    return verts, faces


def build_room(variant: str):
    X, Y, Z = ROOM_X / 2, ROOM_Y / 2, ROOM_Z
    # ---- materials
    wall_n = new_principled("ARCH_wall_N", "#D9D3C7", rough=0.8, spec=0.2)
    mat_noise_tint(wall_n, scale=55.0, strength=0.06)
    wall_s = new_principled("ARCH_wall_S", "#A3ABB5", rough=0.8, spec=0.2)
    wall_e_hex = "#8FA58B" if variant != "v2" else "#C9A34B"  # v2: the client changes the east wall finish
    wall_e = new_principled("ARCH_wall_E", wall_e_hex, rough=0.75, spec=0.2)
    mat_noise_tint(wall_e, scale=70.0, strength=0.07)
    wall_w = new_principled("ARCH_wall_W", "#B8785C", rough=0.8, spec=0.2)
    ceil_m = new_principled("ARCH_ceiling", "#F1EFEA", rough=0.9, spec=0.1)
    floor_m = mat_floor()
    glass_m = mat_glass()
    sky_m = mat_sky()
    sign_m = new_principled("SIGN_a", "#14171A", rough=0.45, spec=0.4)
    p7016_hex = RAL_7016 if variant != "ral3020" else RAL_3020
    p7016_m = new_principled("ARCH_panel_7016", p7016_hex, rough=0.38, spec=0.6)
    p3020_m = new_principled("ARCH_panel_3020", RAL_3020, rough=0.38, spec=0.6)
    marker_m = new_principled("ARCH_marker", "#FFAA00", rough=0.6, spec=0.3)
    leaf_m = new_principled("VEG_leaf", "#3E7A2B", rough=0.6, spec=0.3)
    mat_noise_tint(leaf_m, scale=18.0, strength=0.16)
    people_m = new_principled("PEOPLE_dummy", "#BBA593", rough=0.7, spec=0.3)
    water_m = new_principled("WATER_pool", "#0D3A46", rough=0.04, spec=0.8)

    # ---- walls, floor, ceiling (inward-facing single planes; N wall carries the window opening)
    def plane(name, p0, p1, p2, p3, mat):
        v, f = quad(p0, p1, p2, p3)
        return mesh_object(name, v, f, mat)

    plane("HARD_floor", (-X, -Y, 0), (X, -Y, 0), (X, Y, 0), (-X, Y, 0), floor_m)
    plane("ARCH_ceiling", (-X, Y, Z), (X, Y, Z), (X, -Y, Z), (-X, -Y, Z), ceil_m)
    plane("ARCH_wall_S", (X, -Y, 0), (-X, -Y, 0), (-X, -Y, Z), (X, -Y, Z), wall_s)
    plane("ARCH_wall_E", (X, Y, 0), (X, -Y, 0), (X, -Y, Z), (X, Y, Z), wall_e)
    plane("ARCH_wall_W", (-X, -Y, 0), (-X, Y, 0), (-X, Y, Z), (-X, -Y, Z), wall_w)
    # north wall with a window x -1..1, z 0.9..2.3
    wx0, wx1, wz0, wz1 = -1.0, 1.0, 0.9, 2.3
    nv = [
        (-X, Y, 0),
        (X, Y, 0),
        (X, Y, Z),
        (-X, Y, Z),
        (wx0, Y, wz0),
        (wx1, Y, wz0),
        (wx1, Y, wz1),
        (wx0, Y, wz1),
    ]
    nf = [(1, 0, 4, 5), (6, 7, 3, 2), (0, 3, 7, 4), (1, 5, 6, 2)]  # below, above, left, right of the opening
    # winding checked below: normals must face -y (into the room); flip faces whose normal has +y
    nf_fixed = []
    for f in nf:
        pts = [Vector(nv[i]) for i in f]
        n = (pts[1] - pts[0]).cross(pts[2] - pts[1])
        nf_fixed.append(f if n.y < 0 else tuple(reversed(f)))
    mesh_object("ARCH_wall_N", nv, nf_fixed, wall_n)

    # ---- glass pane: a 2 cm slab filling the lower part of the window opening (the top 0.45 m is an open
    # transom, so the sky backdrop is directly visible and carries its own ID), invisible to shadow rays so
    # the sun and sky light the room without caustic noise (camera, glossy and transmission rays see it)
    pane_top = 1.85
    bm = bmesh.new()
    box(bm, (0.0, Y - 0.02, 0.5 * (wz0 + pane_top)), (wx1 - wx0, 0.02, pane_top - wz0))
    pane = bm_to_object("GLZ_pane", bm, glass_m, smooth=False)
    pane.visible_shadow = False

    # ---- sky backdrop outside the window, finite so the world colour shows above it (uncovered pixels)
    sky_v = [(-40, 14, -4), (40, 14, -4), (40, 14, 4.0), (-40, 14, 4.0)]
    sky = mesh_object("SKY_dome", sky_v, [(0, 1, 2, 3)], sky_m)  # normal faces -y toward the room
    sky.visible_shadow = False
    sky.visible_diffuse = True

    # ---- sign: "PLATE 01" as real geometry on the north wall, right of the window
    curve = bpy.data.curves.new("SIGN_plate01", type="FONT")
    curve.body = "PLATE 01"
    curve.size = 0.22
    curve.extrude = 0.012
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    sign = bpy.data.objects.new("SIGN_plate01", curve)
    _link(sign)
    sign.location = (1.62, Y - 0.02, 1.62)
    sign.rotation_euler = (math.pi / 2, 0.0, 0.0)
    bpy.context.view_layer.objects.active = sign
    sign.select_set(True)
    bpy.ops.object.convert(target="MESH")
    sign = bpy.data.objects["SIGN_plate01"]
    sign.data.materials.clear()
    sign.data.materials.append(sign_m)
    sign.select_set(False)

    # ---- RAL panels: freestanding screens (thin boxes), yawed so they shade differently
    bm = bmesh.new()
    box(bm, (-0.73, 1.45, 0.80), (0.75, 0.05, 1.6), rot_z=math.radians(-32))
    bm_to_object("ARCH_panel_7016", bm, p7016_m, smooth=False)
    bm = bmesh.new()
    box(bm, (0.32, 0.14, 0.70), (0.7, 0.05, 1.4), rot_z=math.radians(-12))
    bm_to_object("ARCH_panel_3020", bm, p3020_m, smooth=False)

    # ---- foliage proxy: ico-sphere cluster
    rnd = random.Random(7)
    bm = bmesh.new()
    for _ in range(14):
        c = Vector(
            (
                -1.50 + rnd.uniform(-0.30, 0.30),
                0.55 + rnd.uniform(-0.30, 0.30),
                0.55 + rnd.uniform(-0.25, 0.45),
            )
        )
        ico_sphere(bm, c, rnd.uniform(0.11, 0.2), subdiv=2)
    shrub = bm_to_object("VEG_shrub_A", bm, leaf_m, smooth=True)
    _ = shrub

    # ---- people dummy: capsule 1.7 m tall
    prof = (
        [(0.0, 0.0)]
        + [
            (0.17 * math.sin(a), 0.17 - 0.17 * math.cos(a))
            for a in [i * math.pi / 2 / 5 for i in range(1, 6)]
        ]
        + [(0.17, 1.53)]
        + [
            (0.17 * math.cos(a), 1.53 + 0.17 * math.sin(a))
            for a in [i * math.pi / 2 / 5 for i in range(1, 6)]
        ]
    )
    pv, pf = lathe(prof, 24)
    people = mesh_object("PEOPLE_dummy", pv, pf, people_m, smooth=True)
    people.location = (0.02, 1.28, 0.0)

    # ---- pool: reflective plane just above the floor
    plane(
        "WATER_pool",
        (-0.90, 0.95, 0.012),
        (0.90, 0.95, 0.012),
        (0.90, 1.85, 0.012),
        (-0.90, 1.85, 0.012),
        water_m,
    )

    # ---- conformance markers
    mk = json.loads((HERE / "markers.json").read_text(encoding="utf-8"))
    for m in mk["markers"]:
        bm = bmesh.new()
        uv_sphere(bm, Vector(m["position"]), mk["radius_m"], u=24, v=12)
        bm_to_object(m["name"], bm, marker_m, smooth=True)

    # ---- lights: key sun through the window, a camera-invisible ceiling fill, a sky-blue world
    sun_data = bpy.data.lights.new("SUN_key", "SUN")
    sun_data.energy = 4.0
    sun_data.angle = math.radians(0.6)
    sun = bpy.data.objects.new("SUN_key", sun_data)
    _link(sun)
    d = Vector((0.32, -0.85, -0.42)).normalized()
    sun.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    fill_data = bpy.data.lights.new("LGT_fill", "AREA")
    fill_data.energy = 90.0
    fill_data.shape = "RECTANGLE"
    fill_data.size, fill_data.size_y = 1.6, 1.0
    fill = bpy.data.objects.new("LGT_fill", fill_data)
    _link(fill)
    fill.location = (-0.3, -0.2, ROOM_Z - 0.03)
    fill.visible_camera = False
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs["Color"].default_value = (0.42, 0.58, 0.95, 1.0)
    bg.inputs["Strength"].default_value = 0.9
    bpy.context.scene.world = world
    REC.globals["lights"] = dict(
        sun=dict(energy=4.0, angle_deg=0.6, direction=[r6(c) for c in d]),
        fill=dict(energy=90.0, size=[1.6, 1.0], location=[r6(c) for c in fill.location]),
        world=dict(color=[0.42, 0.58, 0.95], strength=0.9),
    )


# --------------------------------------------------------------------------- cameras and render settings


def look_at_matrix(loc, target) -> Matrix:
    loc, target = Vector(loc), Vector(target)
    rot = (target - loc).to_track_quat("-Z", "Y").to_matrix().to_4x4()
    return Matrix.Translation(loc) @ rot


def make_camera(name: str, spec: dict) -> bpy.types.Object:
    cam = bpy.data.cameras.new(name)
    cam.lens = spec["lens"]
    cam.sensor_width = SENSOR_WIDTH_MM
    cam.sensor_fit = "HORIZONTAL"
    cam.clip_start, cam.clip_end = 0.05, 200.0
    cam.dof.use_dof = bool(spec["dof"])
    cam.dof.aperture_fstop = spec["fstop"]
    cam.dof.focus_distance = spec["focus"]
    obj = bpy.data.objects.new(name, cam)
    _link(obj)
    obj.matrix_world = look_at_matrix(spec["loc"], spec["target"])
    return obj


def setup_render(
    scene: bpy.types.Scene, quick: bool, samples: int | None = None, denoise: bool = False
) -> None:
    scene.render.engine = "CYCLES"
    cy = scene.cycles
    cy.device = "CPU"
    cy.samples = samples or (8 if quick else SAMPLES)
    cy.use_adaptive_sampling = False
    cy.use_denoising = denoise
    if denoise:
        cy.denoiser = "OPENIMAGEDENOISE"
    cy.seed = 0
    cy.use_animated_seed = False
    cy.max_bounces = 6
    cy.diffuse_bounces = 3
    cy.glossy_bounces = 3
    cy.transmission_bounces = 4
    cy.caustics_reflective = False
    cy.caustics_refractive = False
    cy.sample_clamp_indirect = 6.0
    cy.filter_width = 1.5
    r = scene.render
    r.film_transparent = False
    r.use_motion_blur = False
    r.use_compositing = False
    r.use_sequencer = False
    r.use_stamp = False
    r.dither_intensity = 0.0
    r.use_file_extension = False
    r.resolution_percentage = 100
    r.pixel_aspect_x = r.pixel_aspect_y = 1.0
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.display_settings.display_device = "sRGB"
    scene.sequencer_colorspace_settings.name = "sRGB"
    vl = scene.view_layers[0]
    vl.use_pass_combined = True
    vl.use_pass_z = True
    vl.use_pass_normal = True
    vl.use_pass_diffuse_color = True
    vl.use_pass_diffuse_direct = True
    vl.use_pass_diffuse_indirect = True
    vl.use_pass_glossy_color = True
    vl.use_pass_glossy_direct = True
    vl.use_pass_glossy_indirect = True
    vl.use_pass_cryptomatte_object = True
    vl.use_pass_cryptomatte_material = True
    vl.pass_cryptomatte_depth = 6


def set_multilayer(scene) -> None:
    s = scene.render.image_settings
    s.media_type = "MULTI_LAYER_IMAGE"
    s.file_format = "OPEN_EXR_MULTILAYER"
    s.color_depth = "32"
    s.exr_codec = "ZIP"


def render_to(scene, path: Path) -> None:
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def save_render_as(scene, path: Path, fmt: str, depth: str) -> None:
    s = scene.render.image_settings
    s.media_type = "IMAGE"
    s.file_format = fmt
    s.color_depth = depth
    s.color_mode = "RGB"
    if fmt == "TIFF":
        s.tiff_codec = "DEFLATE"
    elif fmt == "PNG":
        s.compression = 90
    img = bpy.data.images["Render Result"]
    img.save_render(str(path), scene=scene)
    set_multilayer(scene)


# --------------------------------------------------------------------------- shots


def matrix_rows(obj) -> list[list[float]]:
    bpy.context.view_layer.update()
    return [[r6(v) for v in row] for row in obj.matrix_world]


def camera_info(obj, res, spec) -> dict:
    cam = obj.data
    return dict(
        name=obj.name,
        lens_mm=r6(cam.lens),
        sensor_width_mm=r6(cam.sensor_width),
        sensor_fit=cam.sensor_fit,
        resolution=list(res),
        f_number=r6(cam.dof.aperture_fstop),
        focus_distance_m=r6(cam.dof.focus_distance),
        dof_enabled=bool(spec["dof"]),
    )


def dolly_matrix(frame: int) -> Matrix:
    spec = CAMERAS["CAM_dolly"]
    base = look_at_matrix(spec["loc"], spec["target"])
    fwd = -base.col[2].to_3d().normalized()  # camera looks down local -Z
    step = DOLLY_TRAVEL_M * frame / (DOLLY_FRAMES - 1)
    return Matrix.Translation(fwd * step) @ base


def render_unfiltered_z(scene, cam_obj, dof_was: bool, path: Path) -> None:
    """Depth only, one sample, a 0.01 px pixel filter and no depth of field: a point-sampled Z."""
    cy, vl = scene.cycles, scene.view_layers[0]
    keep = (cy.samples, cy.filter_width, cam_obj.data.dof.use_dof, cy.use_denoising)
    keep_passes = {k: getattr(vl, k) for k in vl.bl_rna.properties.keys() if k.startswith("use_pass_")}
    for k in keep_passes:
        setattr(vl, k, k == "use_pass_z")
    vl.use_pass_combined = True
    cy.samples, cy.filter_width, cy.use_denoising = 1, 0.01, False
    cam_obj.data.dof.use_dof = False
    render_to(scene, path)
    cy.samples, cy.filter_width, cy.use_denoising = keep[0], keep[1], keep[3]
    cam_obj.data.dof.use_dof = keep[2]
    for k, v in keep_passes.items():
        setattr(vl, k, v)


def render_shot(scene, cam_objs, clay_mat, shot: dict, out_dir: Path, quick: bool) -> None:
    sdir = out_dir / shot["shot"]
    sdir.mkdir(parents=True, exist_ok=True)
    spec = CAMERAS[shot["cam"]]
    cam_obj = cam_objs[shot["cam"]]
    scene.camera = cam_obj
    w, h = shot["res"]
    scene.render.resolution_x, scene.render.resolution_y = w, h
    info = dict(
        shot=shot["shot"],
        cam=camera_info(cam_obj, shot["res"], spec),
        samples=scene.cycles.samples,
        blender=bpy.app.version_string,
        frames=[],
    )
    vl = scene.view_layers[0]
    set_multilayer(scene)
    # frame 0 of the packet: the full beauty + data passes (the hero still)
    if shot["cam"] == "CAM_dolly":
        cam_obj.matrix_world = dolly_matrix(0)
    render_to(scene, sdir / "ml.exr")
    save_render_as(scene, sdir / "display.tif", "TIFF", "16")
    render_unfiltered_z(scene, cam_obj, spec["dof"], sdir / "zu.exr")
    info["hero"] = dict(ml="ml.exr", zu="zu.exr", display="display.tif", matrix_world=matrix_rows(cam_obj))
    # motion packets: clay frames (material override), id/z from the same renders, camera path
    if shot.get("clay"):
        seq = sdir / "seq"
        seq.mkdir(exist_ok=True)
        vl.material_override = clay_mat
        # the clay frames are the video-engine conditioning image, not a data pass: OIDN-denoise them (the 16-bit
        # PNGs compress 3x smaller). Cryptomatte and Z are never denoised, so id/z are unaffected.
        keep_dn = (scene.cycles.use_denoising, scene.cycles.denoiser)
        scene.cycles.use_denoising, scene.cycles.denoiser = True, "OPENIMAGEDENOISE"
        for f in shot["frames"]:
            cam_obj.matrix_world = dolly_matrix(f)
            render_to(scene, seq / f"ml_{f:04d}.exr")
            save_render_as(scene, seq / f"clay_{f:04d}.png", "PNG", "16")
            render_unfiltered_z(scene, cam_obj, spec["dof"], seq / f"zu_{f:04d}.exr")
            info["frames"].append(dict(frame=f, matrix_world=matrix_rows(cam_obj)))
        scene.cycles.use_denoising, scene.cycles.denoiser = keep_dn
        vl.material_override = None
        info["clay_material"] = clay_mat.name
        cam_obj.matrix_world = dolly_matrix(0)
    (sdir / "info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"[plate-fixture] rendered {shot['shot']}")


def scene_records(variant: str, out_dir: Path) -> None:
    bpy.context.view_layer.update()
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        me = obj.data
        xs = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        REC.objects[obj.name] = dict(
            name=obj.name,
            matrix_world=[[r6(v) for v in row] for row in obj.matrix_world],
            vertices=len(me.vertices),
            polygons=len(me.polygons),
            bbox_min=[r6(min(p[i] for p in xs)) for i in range(3)],
            bbox_max=[r6(max(p[i] for p in xs)) for i in range(3)],
            material=obj.data.materials[0].name if obj.data.materials else None,
        )
    REC.globals["render"] = dict(
        engine="cycles", samples=SAMPLES, filter_width=1.5, view_transform="Standard"
    )
    doc = dict(objects=REC.objects, materials=REC.materials, globals=REC.globals)
    (out_dir / "scene_records.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


def parse_args(argv):
    out = None
    variant, quick, only, samples, denoise = "base", False, None, None, False
    it = iter(argv)
    for a in it:
        if a == "--variant":
            variant = next(it)
        elif a == "--samples":
            samples = int(next(it))
        elif a == "--denoise":
            denoise = True
        elif a == "--quick":
            quick = True
        elif a == "--only":
            only = set(next(it).split(","))
        elif out is None:
            out = Path(a)
    if out is None or variant not in SHOTS:
        raise SystemExit(
            "usage: scene.py -- <out_dir> [--variant base|ral3020|v2] [--quick] [--only shot,...]"
        )
    return out, variant, quick, only, samples, denoise


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out, variant, quick, only, samples, denoise = parse_args(argv)
    out_dir = out / variant
    out_dir.mkdir(parents=True, exist_ok=True)
    _clear_scene()
    scene = bpy.context.scene
    setup_render(scene, quick, samples, denoise)
    build_room(variant)
    clay = mat_clay()
    cam_objs = {name: make_camera(name, spec) for name, spec in CAMERAS.items()}
    scene_records(variant, out_dir)
    for shot in SHOTS[variant]:
        if only and shot["shot"] not in only:
            continue
        render_shot(scene, cam_objs, clay, shot, out_dir, quick)
    stamp = hashlib.sha256(Path(__file__).read_bytes() + (HERE / "markers.json").read_bytes()).hexdigest()
    (out_dir / "stamp.txt").write_text(stamp + "\n", encoding="utf-8", newline="\n")


main()
