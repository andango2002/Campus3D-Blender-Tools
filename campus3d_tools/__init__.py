
bl_info = {
    "name": "Campus3D Tools",
    "author": "Akinaru Iino",
    "version": (0, 4, 7),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Campus3D",
    "description": "Campus3D Workflow [B]: import GSI2Campus3D packages, finish scenes and prepare GLB for CampusMint",
    "category": "3D View",
}

import bpy
import bmesh
import math
import os
import json
from mathutils import Vector
from bpy.props import BoolProperty, FloatProperty, PointerProperty, StringProperty, EnumProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper

CATEGORY_MAP = [
    ("Terrain", ("terrain", "dem")),
    ("Roads", ("rdedg", "road")),
    ("Building_Normal_Small", ("building_normal_small",)),
    ("Building_Normal_Large", ("building_normal_large",)),
    ("Building_Solid_Small", ("building_solid_small",)),
    ("Building_Solid_Large", ("building_solid_large",)),
]

# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------


def category_from_text(text):
    low = (text or "").lower()
    tests = [
        ("Building_Normal_Small", ("building_normal_small", "normal_small")),
        ("Building_Normal_Large", ("building_normal_large", "normal_large")),
        ("Building_Solid_Small", ("building_solid_small", "solid_small")),
        ("Building_Solid_Large", ("building_solid_large", "solid_large")),
        ("Roads", ("rdedg", "road")),
        ("Terrain", ("terrain", "dem")),
    ]
    for cat, keys in tests:
        if any(k in low for k in keys):
            return cat
    return None

def category_for_name(name):
    return category_from_text(name) or "Other"

def parse_obj_object_names(filepath):
    """Read QGIS OBJ 'o ' records in source order."""
    names = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("o "):
                    name = line[2:].strip()
                    if name:
                        names.append(name)
    except Exception:
        pass
    return names

def bind_source_obj_names(imported_objects, filepath):
    """Bind QGIS source OBJ names to Blender-imported meshes by source order.

    QGIS 3D Scene Export writes one 'o <layer_name...>' per exported object.
    Blender may reuse/alter displayed names, so we store the original name
    explicitly in a custom property and rename the object where possible.
    """
    source_names = parse_obj_object_names(filepath)
    meshes = [o for o in imported_objects if getattr(o, "type", None) == 'MESH']
    bound = 0

    if not source_names or not meshes:
        return bound, len(source_names), len(meshes)

    # Blender's OBJ importer creates mesh objects in OBJ object order.
    # Bind as many as available. QGIS source object names are normally unique.
    for obj, source_name in zip(meshes, source_names):
        try:
            obj["campus3d_source_obj_name"] = source_name
            obj.name = source_name
            if getattr(obj, "data", None) is not None:
                obj.data.name = source_name + "_mesh"
            bound += 1
        except Exception:
            pass

    return bound, len(source_names), len(meshes)

def category_for_object(obj):
    """Classify using the original QGIS OBJ name first.

    Do NOT use current Collection names as evidence: doing that causes a
    self-reinforcing bug where objects already placed in Normal_Small can never
    escape that collection on reclassification.
    """
    candidates = []

    # Strongest signal: original `o` name parsed directly from the QGIS OBJ.
    try:
        src = obj.get("campus3d_source_obj_name", "")
        if src:
            candidates.append(src)
    except Exception:
        pass

    candidates.append(getattr(obj, "name", ""))

    data = getattr(obj, "data", None)
    if data is not None:
        candidates.append(getattr(data, "name", ""))
        mats = getattr(data, "materials", None)
        if mats:
            for mat in mats:
                if mat:
                    candidates.append(getattr(mat, "name", ""))

    # Other custom properties can preserve source metadata.
    try:
        for k in obj.keys():
            if k == "campus3d_source_obj_name":
                continue
            candidates.append(str(k))
            candidates.append(str(obj[k]))
    except Exception:
        pass

    found = []
    for c in candidates:
        cat = category_from_text(c)
        if cat:
            found.append(cat)

    for cat in (
        "Building_Normal_Small",
        "Building_Normal_Large",
        "Building_Solid_Small",
        "Building_Solid_Large",
    ):
        if cat in found:
            return cat
    if "Roads" in found:
        return "Roads"
    if "Terrain" in found:
        return "Terrain"
    return "Other"

def ensure_collection(name, parent=None):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        if parent is None:
            bpy.context.scene.collection.children.link(coll)
        else:
            parent.children.link(coll)
    return coll

def move_object_to_collection(obj, coll):
    if obj.name not in coll.objects:
        coll.objects.link(obj)
    for old in list(obj.users_collection):
        if old != coll:
            old.objects.unlink(obj)

def bbox_world(obj):
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs = [p.x for p in pts]; ys = [p.y for p in pts]; zs = [p.z for p in pts]
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)

def terrain_objects(objects):
    return [o for o in objects if o.type == 'MESH' and category_for_object(o) == "Terrain"]

def terrain_extent(objects):
    ts = terrain_objects(objects)
    if not ts:
        return None
    boxes = [bbox_world(o) for o in ts]
    return (
        min(b[0] for b in boxes), max(b[1] for b in boxes),
        min(b[2] for b in boxes), max(b[3] for b in boxes),
        min(b[4] for b in boxes), max(b[5] for b in boxes),
    )

def terrain_size(objects):
    ext = terrain_extent(objects)
    if not ext:
        return None
    return ext[1]-ext[0], ext[3]-ext[2], ext[5]-ext[4]

def set_origin_geometry(objects):
    view_layer = bpy.context.view_layer
    bpy.ops.object.select_all(action='DESELECT')
    n = 0
    for obj in objects:
        if obj.type != 'MESH' or len(obj.data.vertices) == 0:
            continue
        obj.select_set(True)
        view_layer.objects.active = obj
        try:
            bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')
            n += 1
        except Exception:
            pass
        obj.select_set(False)
    return n

def recenter_objects(objects):
    ext = terrain_extent(objects)
    if not ext:
        return Vector((0,0,0))
    xmin, xmax, ymin, ymax, zmin, zmax = ext
    offset = Vector((-(xmin+xmax)/2.0, -(ymin+ymax)/2.0, -zmin))
    for obj in objects:
        obj.location += offset
    return offset

def apply_uniform_scale(objects, factor):
    if abs(factor - 1.0) < 1e-12:
        return
    for obj in objects:
        obj.location *= factor
        obj.scale *= factor

    view_layer = bpy.context.view_layer
    bpy.ops.object.select_all(action='DESELECT')
    for obj in objects:
        if obj.type not in {'MESH','CURVE'}:
            continue
        obj.select_set(True)
        view_layer.objects.active = obj
        try:
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        except Exception:
            pass
        obj.select_set(False)

def calibration_factor(cur_x, cur_y, real_x, real_y, denom):
    if min(cur_x, cur_y, real_x, real_y, denom) <= 0:
        return None
    tx = real_x / denom
    ty = real_y / denom

    fx = tx / cur_x
    fy = ty / cur_y
    f1 = math.sqrt(fx * fy)
    m1 = abs(math.log(fx / fy))

    fsx = tx / cur_y
    fsy = ty / cur_x
    f2 = math.sqrt(fsx * fsy)
    m2 = abs(math.log(fsx / fsy))

    return (f2, True, m2) if m2 < m1 else (f1, False, m1)

def frame_objects(context, objects):
    if not objects:
        return False
    bpy.ops.object.select_all(action='DESELECT')
    for o in objects:
        o.select_set(True)
    context.view_layer.objects.active = objects[0]
    try:
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region:
                    with context.temp_override(area=area, region=region):
                        bpy.ops.view3d.view_selected(use_all_regions=False)
                    return True
    except Exception:
        pass
    return False

# ------------------------------------------------------------------------
# Geometry cleanup
# ------------------------------------------------------------------------

def cleanup_mesh_to_terrain_xy(obj, ext, margin=0.0):
    if obj.type != 'MESH':
        return 0,0
    xmin, xmax, ymin, ymax, _, _ = ext
    xmin -= margin; xmax += margin; ymin -= margin; ymax += margin

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()

    bad = []
    for f in bm.faces:
        wc = obj.matrix_world @ f.calc_center_median()
        if wc.x < xmin or wc.x > xmax or wc.y < ymin or wc.y > ymax:
            bad.append(f)

    fr = len(bad)
    if bad:
        bmesh.ops.delete(bm, geom=bad, context='FACES')

    loose_edges = [e for e in bm.edges if len(e.link_faces) == 0]
    lr = len(loose_edges)
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context='EDGES')

    loose_verts = [v for v in bm.verts if len(v.link_faces) == 0]
    lr += len(loose_verts)
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context='VERTS')

    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return fr, lr

def remove_empty_mesh_objects(objects):
    n = 0
    for obj in list(objects):
        if obj.type == 'MESH' and (len(obj.data.vertices)==0 or len(obj.data.polygons)==0):
            bpy.data.objects.remove(obj, do_unlink=True)
            try:
                objects.remove(obj)
            except ValueError:
                pass
            n += 1
    return n

# ------------------------------------------------------------------------
# UV / mapping
# ------------------------------------------------------------------------

def adjusted_uv(u, v, s):
    # flip
    if s.map_flip_x:
        u = 1.0 - u
    if s.map_flip_y:
        v = 1.0 - v

    # centered rotation
    x = u - 0.5
    y = v - 0.5
    a = math.radians(s.map_rotation_deg)
    ca, sa = math.cos(a), math.sin(a)
    xr = x*ca - y*sa
    yr = x*sa + y*ca

    # zoom >100 means zoom in
    zx = max(s.map_zoom_x_pct / 100.0, 1e-6)
    zy = max(s.map_zoom_y_pct / 100.0, 1e-6)
    xr /= zx
    yr /= zy

    # move in percent of texture
    u = xr + 0.5 + s.map_shift_x_pct / 100.0
    v = yr + 0.5 + s.map_shift_y_pct / 100.0
    return u, v

def generate_global_uv(objects, s):
    ts = terrain_objects(objects)
    ext = terrain_extent(objects)
    if not ts or not ext:
        return 0

    xmin, xmax, ymin, ymax, _, _ = ext
    dx = xmax - xmin
    dy = ymax - ymin
    if dx <= 0 or dy <= 0:
        return 0

    loops = 0
    for obj in ts:
        mesh = obj.data
        uv = mesh.uv_layers.get("Campus3D_UV") or mesh.uv_layers.new(name="Campus3D_UV")
        mesh.uv_layers.active = uv

        for poly in mesh.polygons:
            for li in poly.loop_indices:
                vi = mesh.loops[li].vertex_index
                wc = obj.matrix_world @ mesh.vertices[vi].co
                u = (wc.x - xmin) / dx
                v = (wc.y - ymin) / dy
                u, v = adjusted_uv(u, v, s)
                uv.data[li].uv = (u,v)
                loops += 1
        mesh.update()
    return loops

def ensure_map_material(image, opacity=1.0):
    mat = bpy.data.materials.get("Campus3D_Map")
    if mat is None:
        mat = bpy.data.materials.new("Campus3D_Map")
    mat.use_nodes = True

    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    tex = nt.nodes.new("ShaderNodeTexImage")
    uv = nt.nodes.new("ShaderNodeUVMap")

    uv.uv_map = "Campus3D_UV"
    tex.image = image
    tex.interpolation = 'Linear'
    tex.extension = 'EXTEND'

    nt.links.new(uv.outputs["UV"], tex.inputs["Vector"])
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 0.65
    if "Alpha" in bsdf.inputs:
        bsdf.inputs["Alpha"].default_value = opacity

    # Blender 4.x transparency API differs across versions; use it when available.
    try:
        mat.surface_render_method = 'DITHERED'
    except Exception:
        pass

    return mat

def assign_material(objects, mat):
    n = 0
    for obj in terrain_objects(objects):
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        for p in obj.data.polygons:
            p.material_index = 0
        n += 1
    return n

def make_guide_material():
    mat = bpy.data.materials.get("Campus3D_UV_Guide")
    if mat is None:
        mat = bpy.data.materials.new("Campus3D_UV_Guide")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    uv = nt.nodes.new("ShaderNodeUVMap")
    uv.uv_map = "Campus3D_UV"
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(uv.outputs["UV"], sep.inputs["Vector"])

    def grid_channel(sock):
        mul = nt.nodes.new("ShaderNodeMath"); mul.operation='MULTIPLY'; mul.inputs[1].default_value=10
        frac = nt.nodes.new("ShaderNodeMath"); frac.operation='FRACT'
        lt = nt.nodes.new("ShaderNodeMath"); lt.operation='LESS_THAN'; lt.inputs[1].default_value=0.035
        nt.links.new(sock, mul.inputs[0]); nt.links.new(mul.outputs[0], frac.inputs[0]); nt.links.new(frac.outputs[0], lt.inputs[0])
        return lt.outputs[0]

    gu = grid_channel(sep.outputs["X"])
    gv = grid_channel(sep.outputs["Y"])
    mx = nt.nodes.new("ShaderNodeMath"); mx.operation='MAXIMUM'
    nt.links.new(gu, mx.inputs[0]); nt.links.new(gv, mx.inputs[1])

    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.inputs["Color1"].default_value=(0.08,0.08,0.08,1)
    mix.inputs["Color2"].default_value=(1.0,0.2,0.02,1)
    nt.links.new(mx.outputs[0], mix.inputs["Fac"])
    nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value=0.7
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


def load_or_reload_image(filepath):
    """Load a file-backed image and force Blender to refresh it from disk.

    Campus3D packages intentionally reuse stable filenames such as
    Terrain_Texture.png. Blender normally caches an existing image datablock,
    so re-importing an updated package can keep showing the old pixels until
    Blender is restarted. This helper matches by normalized absolute filepath,
    reloads existing datablocks from disk, and only creates a new datablock
    when none exists.
    """
    target = os.path.normcase(os.path.abspath(bpy.path.abspath(filepath)))

    for img in list(bpy.data.images):
        try:
            if getattr(img, "source", None) != 'FILE':
                continue
            current = os.path.normcase(os.path.abspath(bpy.path.abspath(img.filepath)))
            if current != target:
                continue

            # Keep the same datablock so existing material-node references remain valid.
            img.filepath = filepath
            try:
                img.filepath_raw = filepath
            except Exception:
                pass
            try:
                img.reload()
                img.update()
            except Exception:
                # If reload fails, remove the stale datablock and fall through to a clean load.
                try:
                    bpy.data.images.remove(img)
                except Exception:
                    pass
                break
            return img
        except Exception:
            continue

    img = bpy.data.images.load(filepath, check_existing=False)
    try:
        img.reload()
        img.update()
    except Exception:
        pass
    return img


def strip_qgis_terrain_materials(objects, purge_images=False):
    """Remove materials imported from QGIS/OBJ from all terrain meshes.
    Optionally remove image datablocks which become unused.
    Returns (terrain_count, material_slots_removed, images_removed).
    """
    ts = terrain_objects(objects)
    removed_slots = 0
    for obj in ts:
        mesh = obj.data
        removed_slots += len(mesh.materials)
        mesh.materials.clear()

    images_removed = 0
    if purge_images:
        for img in list(bpy.data.images):
            # Keep generated/render-result images and any image still used.
            try:
                if img.users == 0 and not img.packed_file and img.source == 'FILE':
                    bpy.data.images.remove(img)
                    images_removed += 1
            except Exception:
                pass

    return len(ts), removed_slots, images_removed



_live_update_scheduled = False

def update_map_material_opacity(opacity):
    mat = bpy.data.materials.get("Campus3D_Map")
    if not mat or not mat.use_nodes or not mat.node_tree:
        return
    for node in mat.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED' and "Alpha" in node.inputs:
            node.inputs["Alpha"].default_value = opacity
            break

def _do_live_map_update():
    global _live_update_scheduled
    try:
        scene = bpy.context.scene
        if scene and hasattr(scene, "campus3d_settings"):
            s = scene.campus3d_settings
            if s.map_live_update:
                generate_global_uv(list(scene.objects), s)
                update_map_material_opacity(s.map_opacity)
                # Force visible 3D/UV refresh.
                wm = bpy.context.window_manager
                for win in wm.windows:
                    for area in win.screen.areas:
                        if area.type in {'VIEW_3D', 'IMAGE_EDITOR', 'NODE_EDITOR'}:
                            area.tag_redraw()
    except Exception:
        pass
    _live_update_scheduled = False
    return None

def schedule_live_map_update(self, context):
    global _live_update_scheduled
    try:
        if not self.map_live_update:
            return
    except Exception:
        return
    if _live_update_scheduled:
        return
    _live_update_scheduled = True
    try:
        bpy.app.timers.register(_do_live_map_update, first_interval=0.05)
    except Exception:
        _live_update_scheduled = False



def _collection_tree_objects(coll):
    found=[]
    seen=set()
    def walk(c):
        for o in c.objects:
            if o.as_pointer() not in seen:
                seen.add(o.as_pointer()); found.append(o)
        for ch in c.children: walk(ch)
    walk(coll)
    return found

def clear_campus3d_scene():
    root=bpy.data.collections.get("Campus3D")
    if root is None:
        return 0
    objs=_collection_tree_objects(root)
    for o in objs:
        try: bpy.data.objects.remove(o,do_unlink=True)
        except Exception: pass
    try: bpy.data.collections.remove(root)
    except Exception: pass
    # Remove empty child collections left orphaned by Blender data ownership.
    for name in ["Terrain","Roads","Building_Normal_Small","Building_Normal_Large","Building_Solid_Small","Building_Solid_Large","Other"]:
        c=bpy.data.collections.get(name)
        if c is not None and len(c.objects)==0 and len(c.children)==0:
            try: bpy.data.collections.remove(c)
            except Exception: pass
    return len(objs)

def import_obj_file(filepath):
    before=set(bpy.data.objects)
    kwargs={"filepath":filepath,"use_split_objects":True,"use_split_groups":False,"validate_meshes":True,
            "forward_axis":'Y',"up_axis":'Z'}
    try:
        bpy.ops.wm.obj_import(**kwargs)
    except TypeError:
        kwargs.pop("forward_axis",None); kwargs.pop("up_axis",None)
        bpy.ops.wm.obj_import(**kwargs)
    return [o for o in bpy.data.objects if o not in before]


# ------------------------------------------------------------------------
# Water
# ------------------------------------------------------------------------

def campus3d_scale_info(scene):
    """Return package horizontal denominator, vertical multiplier, and real Z origin."""
    try:
        denom = float(scene.get("campus3d_scale_denominator", 0) or 0)
    except Exception:
        denom = 0.0
    if denom <= 0:
        try:
            denom = float(scene.campus3d_settings.target_scale)
        except Exception:
            denom = 1000.0

    try:
        vscale = float(scene.get("campus3d_vertical_scale", 1.0) or 1.0)
    except Exception:
        vscale = 1.0

    origin_z = 0.0
    try:
        raw = scene.get("campus3d_origin_json", "")
        if raw:
            origin = json.loads(raw)
            origin_z = float(origin.get("z", 0.0) or 0.0)
    except Exception:
        origin_z = 0.0

    return denom, vscale, origin_z

def real_elevation_to_blender_z(scene, elevation_m):
    """Convert a real-world elevation to package Blender Z."""
    denom, vscale, origin_z = campus3d_scale_info(scene)
    return (float(elevation_m) - origin_z) / denom * vscale

def real_horizontal_to_blender(scene, metres):
    denom, _, _ = campus3d_scale_info(scene)
    return float(metres) / denom

def ensure_water_material():
    mat = bpy.data.materials.get("Campus3D_Water")
    if mat is None:
        mat = bpy.data.materials.new("Campus3D_Water")
    mat.use_nodes = True

    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")

    # A restrained pale blue-green water. Designed to remain usable in both
    # Material Preview and rendered views without extra setup.
    if "Base Color" in bsdf.inputs:
        bsdf.inputs["Base Color"].default_value = (0.055, 0.24, 0.28, 1.0)
    if "Metallic" in bsdf.inputs:
        bsdf.inputs["Metallic"].default_value = 0.0
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 0.16
    if "IOR" in bsdf.inputs:
        bsdf.inputs["IOR"].default_value = 1.333
    if "Transmission Weight" in bsdf.inputs:
        bsdf.inputs["Transmission Weight"].default_value = 0.35
    elif "Transmission" in bsdf.inputs:
        bsdf.inputs["Transmission"].default_value = 0.35
    if "Alpha" in bsdf.inputs:
        bsdf.inputs["Alpha"].default_value = 0.78

    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    try:
        mat.surface_render_method = 'DITHERED'
    except Exception:
        pass
    try:
        mat.use_transparency_overlap = False
    except Exception:
        pass

    return mat

def water_object():
    obj = bpy.data.objects.get("Water")
    if obj is not None and obj.type == 'MESH':
        return obj
    return None

def create_or_update_water(context):
    scene = context.scene
    s = scene.campus3d_settings
    objs = list(scene.objects)
    ext = terrain_extent(objs)
    if not ext:
        raise RuntimeError("Terrainを検出できません")

    xmin, xmax, ymin, ymax, _, _ = ext
    margin = real_horizontal_to_blender(scene, s.water_margin_m)
    xmin -= margin
    xmax += margin
    ymin -= margin
    ymax += margin

    z = real_elevation_to_blender_z(scene, s.water_elevation_m)

    obj = water_object()
    if obj is None:
        mesh = bpy.data.meshes.new("Water_mesh")
        obj = bpy.data.objects.new("Water", mesh)
    else:
        mesh = obj.data
        mesh.clear_geometry()

    # Rectangle in world coordinates. The object's transforms remain identity,
    # so its origin stays predictable and updating never accumulates transforms.
    verts = [
        (xmin, ymin, z),
        (xmax, ymin, z),
        (xmax, ymax, z),
        (xmin, ymax, z),
    ]
    faces = [(0, 1, 2, 3)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)
    obj["campus3d_real_elevation_m"] = float(s.water_elevation_m)
    obj["campus3d_margin_m"] = float(s.water_margin_m)

    root = ensure_collection("Campus3D")
    coll = ensure_collection("Water", root)
    move_object_to_collection(obj, coll)

    if s.water_auto_material:
        mat = ensure_water_material()
        mesh.materials.clear()
        mesh.materials.append(mat)

    return obj, z, margin

def delete_water():
    n = 0
    obj = bpy.data.objects.get("Water")
    if obj is not None:
        bpy.data.objects.remove(obj, do_unlink=True)
        n += 1
    return n


# ------------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------------

class Campus3DSettings(PropertyGroup):
    auto_orient: BoolProperty(name="QGIS軸をBlenderへ補正", default=True)

    real_width_m: FloatProperty(name="QGIS DEM 実幅 X (m)", default=0.0, min=0.0, precision=3)
    real_height_m: FloatProperty(name="QGIS DEM 実幅 Y (m)", default=0.0, min=0.0, precision=3)
    target_scale: EnumProperty(
        name="目標縮尺",
        items=[
            ('1000',"1:1000（演習標準）",""),
            ('500',"1:500",""),
            ('100',"1:100",""),
            ('1',"1:1（実寸）",""),
        ],
        default='1000'
    )
    auto_calibrate: BoolProperty(name="実寸幅から縮尺を自動校正", default=True)

    geometry_clip: BoolProperty(name="Terrain外の飛び地を掃除", default=True)
    outside_margin: FloatProperty(name="掃除マージン", default=0.002, min=0.0, soft_max=0.1)
    set_origins: BoolProperty(name="原点を各ジオメトリへ", default=True)
    recenter_scene: BoolProperty(name="Terrain中心をワールド原点へ", default=True)
    organize: BoolProperty(name="Collection整理", default=True)
    auto_frame: BoolProperty(name="読込後にTerrainを表示", default=True)

    map_image_path: StringProperty(name="地図画像", subtype='FILE_PATH')
    map_shift_x_pct: FloatProperty(name="左右移動 (%)", default=0.0, soft_min=-100, soft_max=100, update=schedule_live_map_update)
    map_shift_y_pct: FloatProperty(name="上下移動 (%)", default=0.0, soft_min=-100, soft_max=100, update=schedule_live_map_update)
    map_zoom_x_pct: FloatProperty(name="横倍率 (%)", default=100.0, min=1.0, soft_max=500, update=schedule_live_map_update)
    map_zoom_y_pct: FloatProperty(name="縦倍率 (%)", default=100.0, min=1.0, soft_max=500, update=schedule_live_map_update)
    map_rotation_deg: FloatProperty(name="回転 (°)", default=0.0, min=-180.0, max=180.0, update=schedule_live_map_update)
    map_flip_x: BoolProperty(name="左右反転", default=False, update=schedule_live_map_update)
    map_flip_y: BoolProperty(name="上下反転", default=False, update=schedule_live_map_update)
    map_opacity: FloatProperty(name="不透明度", default=1.0, min=0.05, max=1.0, subtype='FACTOR', update=schedule_live_map_update)
    map_live_update: BoolProperty(name="リアルタイム反映", default=True)
    remove_qgis_materials_on_import: BoolProperty(name="旧方式OBJの既存Terrainマテリアルを削除", default=False)
    purge_unused_qgis_images: BoolProperty(name="未使用の旧画像データも削除", default=False)
    replace_existing_package: BoolProperty(name="既存Campus3Dを置き換える", default=True)
    apply_package_texture: BoolProperty(name="パッケージのTerrain画像を自動貼付", default=True)
    water_elevation_m: FloatProperty(
        name="海面標高 (m)",
        description="実世界の標高。Campus3Dパッケージではmanifestの標高原点と縮尺からBlender Zへ自動変換します",
        default=0.0,
        min=-100.0,
        max=100.0,
        precision=2
    )
    water_margin_m: FloatProperty(
        name="広がりマージン (m)",
        description="Terrain外周から水面をさらに広げる実世界距離",
        default=200.0,
        min=0.0,
        soft_max=2000.0,
        precision=1
    )
    water_auto_material: BoolProperty(
        name="海面マテリアルを自動作成",
        default=True
    )

# ------------------------------------------------------------------------
# Operators
# ------------------------------------------------------------------------

class CAMPUS3D_OT_import_package(Operator, ImportHelper):
    bl_idname="campus3d.import_package"
    bl_label="Campus3Dパッケージを一括読込"
    bl_description="GSI2Campus3Dが作ったcampus3d_manifest.jsonからTerrain/道路/建物4分類を別々に読み込みます"
    filename_ext=".json"
    filter_glob:StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self,context):
        s=context.scene.campus3d_settings
        try:
            with open(self.filepath,'r',encoding='utf-8') as fh:
                manifest=json.load(fh)
        except Exception as e:
            self.report({'ERROR'},f"manifestを読めません: {e}")
            return {'CANCELLED'}
        if manifest.get('format')!='Campus3D Blender Package':
            self.report({'ERROR'},"Campus3D Blender Packageのmanifestではありません")
            return {'CANCELLED'}
        base=os.path.dirname(self.filepath)
        files=manifest.get('files',{})
        required=['Terrain','Building_Normal_Small','Building_Normal_Large','Building_Solid_Small','Building_Solid_Large']
        missing=[k for k in required if k not in files or not os.path.exists(os.path.join(base,files[k]))]
        if missing:
            self.report({'ERROR'},"必要OBJが見つかりません: "+", ".join(missing))
            return {'CANCELLED'}

        if s.replace_existing_package:
            clear_campus3d_scene()

        root=ensure_collection('Campus3D')
        cats={name:ensure_collection(name,root) for name,_ in CATEGORY_MAP}
        cats['Other']=ensure_collection('Other',root)
        order=['Terrain','Roads','Building_Normal_Small','Building_Normal_Large','Building_Solid_Small','Building_Solid_Large']
        counts={}
        all_imported=[]
        for cat in order:
            rel=files.get(cat)
            if not rel: continue
            fp=os.path.join(base,rel)
            if not os.path.exists(fp): continue
            imported=import_obj_file(fp)
            counts[cat]=len(imported)
            for i,o in enumerate(imported):
                o['campus3d_package_category']=cat
                if len(imported)==1:
                    o.name=cat
                else:
                    o.name=f"{cat}_{i+1:03d}"
                move_object_to_collection(o,cats[cat])
            all_imported.extend(imported)

        # Manifest is authoritative: no size guessing or layer-name inference.
        real=manifest.get('real_size_m',{})
        denom=int(manifest.get('scale_denominator',1000))
        s.real_width_m=float(real.get('x',0.0) or 0.0)
        s.real_height_m=float(real.get('y',0.0) or 0.0)
        s.target_scale=str(denom) if str(denom) in {'1000','500','100','1'} else '1000'
        s.auto_calibrate=False
        context.scene['campus3d_manifest_path']=self.filepath
        context.scene['campus3d_crs']=manifest.get('crs','')
        context.scene['campus3d_scale_denominator']=denom
        context.scene['campus3d_vertical_scale']=float(manifest.get('vertical_scale',1.0) or 1.0)
        context.scene['campus3d_origin_json']=json.dumps(manifest.get('origin',{}),ensure_ascii=False)
        context.scene['campus3d_building_vertical_mode']=manifest.get('building_vertical_mode','legacy_all_z_scaled')

        # Package Terrain texture is aligned to the full DEM extent.
        # Reset prior partial-map adjustments before generating package UVs.
        s.map_shift_x_pct=0.0; s.map_shift_y_pct=0.0
        s.map_zoom_x_pct=100.0; s.map_zoom_y_pct=100.0
        s.map_rotation_deg=0.0; s.map_flip_x=False; s.map_flip_y=False

        generate_global_uv(all_imported,s)

        tex_rel=files.get('Terrain_Texture')
        tex_applied=False
        if s.apply_package_texture and tex_rel:
            tex_path=os.path.join(base,tex_rel)
            if os.path.exists(tex_path):
                try:
                    img=load_or_reload_image(tex_path)
                    mat=ensure_map_material(img,1.0)
                    assign_material(all_imported,mat)
                    s.map_image_path=tex_path
                    s.map_opacity=1.0
                    tex_applied=True
                except Exception:
                    tex_applied=False

        if s.set_origins:
            set_origin_geometry(all_imported)
        if s.auto_frame:
            frame_objects(context,terrain_objects(all_imported))

        summary=' / '.join(f"{k}:{v}" for k,v in counts.items())
        vz=float(manifest.get('vertical_scale',1.0) or 1.0)
        building_mode=manifest.get('building_vertical_mode','legacy_all_z_scaled')
        if vz != 1.0 and building_mode != 'terrain_exaggeration_only':
            self.report(
                {'WARNING'},
                "旧形式Campus3Dパッケージです。Z倍率が1以外の場合、建物高さも誇張されている可能性があります。"
                "GSI2Campus3D v0.4.18以降でパッケージを再出力してください。"
            )
        self.report({'INFO'},f"Campus3Dパッケージ読込完了 1:{denom} / Z×{vz:.2f} / Terrain画像:{'ON' if tex_applied else 'なし/OFF'} / {summary}")
        return {'FINISHED'}

class CAMPUS3D_OT_import(Operator, ImportHelper):
    bl_idname="campus3d.import_obj"
    bl_label="Campus3D OBJを読み込む"
    filename_ext=".obj"
    filter_glob:StringProperty(default="*.obj", options={'HIDDEN'})

    def execute(self, context):
        s=context.scene.campus3d_settings
        context.scene.unit_settings.system='METRIC'
        context.scene.unit_settings.length_unit='METERS'
        context.scene.unit_settings.scale_length=1.0

        before=set(bpy.data.objects)
        kwargs={"filepath":self.filepath,"use_split_objects":True,"use_split_groups":False,"validate_meshes":True}
        if s.auto_orient:
            kwargs["forward_axis"]='Y'
            kwargs["up_axis"]='Z'
        try:
            bpy.ops.wm.obj_import(**kwargs)
        except TypeError:
            kwargs.pop("forward_axis",None); kwargs.pop("up_axis",None)
            bpy.ops.wm.obj_import(**kwargs)

        imported=[o for o in bpy.data.objects if o not in before]
        if not imported:
            self.report({'ERROR'},"新規オブジェクトを検出できません")
            return {'CANCELLED'}

        bound, source_count, mesh_count = bind_source_obj_names(imported, self.filepath)
        context.scene["campus3d_last_obj_path"] = self.filepath
        context.scene["campus3d_obj_names_bound"] = int(bound)
        context.scene["campus3d_obj_source_name_count"] = int(source_count)
        context.scene["campus3d_imported_mesh_count"] = int(mesh_count)

        if s.remove_qgis_materials_on_import:
            strip_qgis_terrain_materials(imported, s.purge_unused_qgis_images)

        root=ensure_collection("Campus3D")
        class_counts = {}
        if s.organize:
            cats={cat:ensure_collection(cat,root) for cat,_ in CATEGORY_MAP}
            cats["Other"]=ensure_collection("Other",root)
            for o in imported:
                cat = category_for_object(o)
                move_object_to_collection(o,cats[cat])
                class_counts[cat] = class_counts.get(cat, 0) + 1
        context.scene["campus3d_class_counts"] = str(class_counts)

        pre=terrain_size(imported)
        if not pre:
            self.report({'ERROR'},"Terrainを検出できません")
            return {'CANCELLED'}

        ext=terrain_extent(imported)
        if s.geometry_clip and ext:
            for o in list(imported):
                if o.type=='MESH' and category_for_object(o)!="Terrain":
                    cleanup_mesh_to_terrain_xy(o,ext,s.outside_margin)
            remove_empty_mesh_objects(imported)

        if s.set_origins:
            set_origin_geometry(imported)
        if s.recenter_scene:
            recenter_objects(imported)

        if s.auto_calibrate and s.real_width_m>0 and s.real_height_m>0:
            r=calibration_factor(pre[0],pre[1],s.real_width_m,s.real_height_m,float(s.target_scale))
            if r:
                factor,swapped,mismatch=r
                apply_uniform_scale(imported,factor)
                context.scene["campus3d_calibration_factor"]=float(factor)
                context.scene["campus3d_axis_swapped"]=bool(swapped)

        generate_global_uv(imported,s)
        if s.auto_frame:
            frame_objects(context,terrain_objects(imported))

        post=terrain_size(imported) or (0,0,0)
        self.report({'INFO'},f"読込完了 / OBJ名 {bound}/{source_count} 結合 / Terrain {post[0]:.3f}×{post[1]:.3f}m / UV準備済み")
        return {'FINISHED'}


class CAMPUS3D_OT_apply_map(Operator):
    bl_idname="campus3d.apply_map"
    bl_label="地図を貼る / 更新"
    def execute(self,context):
        s=context.scene.campus3d_settings
        path=bpy.path.abspath(s.map_image_path).strip()
        if not path or not os.path.exists(path):
            self.report({'ERROR'},"地図画像を選択してください")
            return {'CANCELLED'}

        objs=list(context.scene.objects)
        if not terrain_objects(objs):
            self.report({'ERROR'},"Terrainを検出できません")
            return {'CANCELLED'}

        generate_global_uv(objs,s)
        # Hard reset inherited QGIS/OBJ terrain materials so the new map always wins.
        strip_qgis_terrain_materials(objs, False)
        try:
            img=load_or_reload_image(path)
        except Exception as e:
            self.report({'ERROR'},f"画像を読めません: {e}")
            return {'CANCELLED'}

        mat=ensure_map_material(img,s.map_opacity)
        n=assign_material(objs,mat)
        self.report({'INFO'},f"{n} Terrain object(s) に地図を貼りました")
        return {'FINISHED'}

class CAMPUS3D_OT_guide(Operator):
    bl_idname="campus3d.show_guide"
    bl_label="位置合わせガイドを表示"
    def execute(self,context):
        s=context.scene.campus3d_settings
        objs=list(context.scene.objects)
        if generate_global_uv(objs,s)==0:
            self.report({'ERROR'},"Terrainを検出できません")
            return {'CANCELLED'}
        strip_qgis_terrain_materials(objs, False)
        assign_material(objs,make_guide_material())
        self.report({'INFO'},"10×10ガイドを表示しました")
        return {'FINISHED'}

class CAMPUS3D_OT_reset_map(Operator):
    bl_idname="campus3d.reset_map"
    bl_label="位置合わせをリセット"
    def execute(self,context):
        s=context.scene.campus3d_settings
        s.map_shift_x_pct=0.0; s.map_shift_y_pct=0.0
        s.map_zoom_x_pct=100.0; s.map_zoom_y_pct=100.0
        s.map_rotation_deg=0.0; s.map_flip_x=False; s.map_flip_y=False
        generate_global_uv(list(context.scene.objects),s)
        self.report({'INFO'},"位置合わせを初期状態へ戻しました")
        return {'FINISHED'}


class CAMPUS3D_OT_clear_qgis_texture(Operator):
    bl_idname="campus3d.clear_qgis_texture"
    bl_label="QGIS衛星画像を完全に外す"
    def execute(self,context):
        s=context.scene.campus3d_settings
        objs=list(context.scene.objects)
        tc, slots, imgs = strip_qgis_terrain_materials(objs, s.purge_unused_qgis_images)
        if tc == 0:
            self.report({'ERROR'},"Terrainを検出できません")
            return {'CANCELLED'}
        self.report({'INFO'},f"{tc} Terrain / {slots} material slot(s) を削除、画像 {imgs} 件削除")
        return {'FINISHED'}


class CAMPUS3D_OT_reclassify(Operator):
    bl_idname="campus3d.reclassify"
    bl_label="建物Collectionを再分類"
    bl_description="OBJ名だけでなくMesh名・Material名なども使って建物4分類をやり直します"

    def execute(self,context):
        root=ensure_collection("Campus3D")
        cats={cat:ensure_collection(cat,root) for cat,_ in CATEGORY_MAP}
        cats["Other"]=ensure_collection("Other",root)

        scene_objs=[o for o in list(context.scene.objects) if o.type in {'MESH','CURVE'}]

        # Recovery path for scenes imported by older plugin versions:
        # re-bind the original QGIS OBJ `o` names if the OBJ path is still known.
        has_source = any(bool(o.get("campus3d_source_obj_name", "")) for o in scene_objs if o.type=='MESH')
        if not has_source:
            last_path = context.scene.get("campus3d_last_obj_path", "")
            if last_path and os.path.exists(last_path):
                bind_source_obj_names(scene_objs, last_path)

        counts={k:0 for k in cats.keys()}
        unknown=[]

        for o in scene_objs:
            cat=category_for_object(o)
            if cat not in cats:
                cat="Other"
            move_object_to_collection(o,cats[cat])
            counts[cat]+=1
            if cat=="Other":
                unknown.append(o.name)

        context.scene["campus3d_reclass_counts"] = str(counts)
        msg = " / ".join([
            f"N-S:{counts.get('Building_Normal_Small',0)}",
            f"N-L:{counts.get('Building_Normal_Large',0)}",
            f"S-S:{counts.get('Building_Solid_Small',0)}",
            f"S-L:{counts.get('Building_Solid_Large',0)}",
            f"Other:{counts.get('Other',0)}",
        ])
        self.report({'INFO'}, "再分類: " + msg)
        return {'FINISHED'}


class CAMPUS3D_OT_water_add(Operator):
    bl_idname="campus3d.water_add"
    bl_label="海面を追加 / 更新"
    bl_description="Terrain全体を覆う水面を、指定した実標高とマージンで作成または更新します"

    def execute(self,context):
        try:
            obj, z, margin = create_or_update_water(context)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        denom, vscale, origin_z = campus3d_scale_info(context.scene)
        self.report(
            {'INFO'},
            f"海面を更新: 標高 {context.scene.campus3d_settings.water_elevation_m:.2f} m "
            f"→ Blender Z {z:.4f} m / 1:{int(denom)} / Z×{vscale:.2f}"
        )
        return {'FINISHED'}

class CAMPUS3D_OT_water_delete(Operator):
    bl_idname="campus3d.water_delete"
    bl_label="海面を削除"
    bl_description="Campus3DのWaterオブジェクトを削除します"

    def execute(self,context):
        n = delete_water()
        if n:
            self.report({'INFO'}, "海面を削除しました")
        else:
            self.report({'INFO'}, "削除する海面はありません")
        return {'FINISHED'}

class CAMPUS3D_OT_focus(Operator):
    bl_idname="campus3d.focus"
    bl_label="Terrain全体を表示"
    def execute(self,context):
        ts=terrain_objects(list(context.scene.objects))
        if not ts:
            self.report({'ERROR'},"Terrainを検出できません")
            return {'CANCELLED'}
        frame_objects(context,ts)
        return {'FINISHED'}

# ------------------------------------------------------------------------
# UI
# ------------------------------------------------------------------------

class CAMPUS3D_PT_panel(Panel):
    bl_label="Campus3D Workflow [B]"
    bl_idname="CAMPUS3D_PT_panel"
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category="Campus3D"

    def draw(self,context):
        layout=self.layout
        s=context.scene.campus3d_settings

        brand=layout.box()
        row=brand.row(align=True)
        row.scale_y=1.25
        row.label(text="Campus3D Workflow",icon='WORLD')
        row.label(text="[B]")
        title=brand.row()
        title.scale_y=1.15
        title.label(text="Campus3D Tools")
        brand.label(text="3D finishing & GLB export for CampusMint")
        brand.separator()
        brand.label(text="Est. 2026 | Powered by IINO Lab.")
        brand.label(text="Add-on v0.4.7")
        flow=brand.row()
        flow.scale_y=0.95
        flow.label(text="[A] QGIS  →  [B] Blender  →  GLB")
        brand.label(text="← [A] GSI2Campus3D の続きです")

        b=layout.box()
        b.label(text="[B-1] 推奨：GSI2Campus3Dパッケージ")
        b.prop(s,"replace_existing_package")
        b.prop(s,"apply_package_texture")
        b.operator("campus3d.import_package",icon='IMPORT')
        b.label(text="campus3d_manifest.json を選択")
        b.label(text="分類・縮尺・原点・Z倍率はmanifestから自動取得")
        b.label(text="※v0.3.2以降のQGISパッケージはTerrain画像を同梱可能")

        b=layout.box()
        b.label(text="[B-旧] 単一OBJの手動読込")
        b.prop(s,"real_width_m")
        b.prop(s,"real_height_m")
        b.prop(s,"target_scale")
        b.prop(s,"auto_calibrate")

        b=layout.box()
        b.label(text="旧方式OBJオプション")
        b.prop(s,"auto_orient")
        b.prop(s,"geometry_clip")
        if s.geometry_clip: b.prop(s,"outside_margin")
        b.prop(s,"set_origins")
        b.prop(s,"recenter_scene")
        b.prop(s,"organize")
        b.prop(s,"remove_qgis_materials_on_import")
        if s.remove_qgis_materials_on_import:
            b.prop(s,"purge_unused_qgis_images")
        b.prop(s,"auto_frame")
        b.operator("campus3d.import_obj",icon='IMPORT')

        b=layout.box()
        b.label(text="[B-2] 地図をTerrainに貼る")
        b.prop(s,"map_image_path",text="地図画像")
        b.operator("campus3d.clear_qgis_texture",icon='TRASH')
        b.operator("campus3d.apply_map",icon='IMAGE_DATA')
        b.operator("campus3d.show_guide",icon='UV')

        b.separator()
        b.label(text="部分地図・位置ずれの調整")
        b.prop(s,"map_live_update")
        b.prop(s,"map_shift_x_pct")
        b.prop(s,"map_shift_y_pct")
        b.prop(s,"map_zoom_x_pct")
        b.prop(s,"map_zoom_y_pct")
        b.prop(s,"map_rotation_deg")
        row=b.row(align=True)
        row.prop(s,"map_flip_x")
        row.prop(s,"map_flip_y")
        b.prop(s,"map_opacity")
        if not s.map_live_update:
            b.operator("campus3d.apply_map",text="調整を反映",icon='FILE_REFRESH')
        else:
            b.label(text="数値変更を約0.05秒後に自動反映")
        b.operator("campus3d.reset_map",icon='LOOP_BACK')

        b=layout.box()
        b.label(text="[B-3] 海面")
        b.prop(s,"water_elevation_m")
        b.prop(s,"water_margin_m")
        b.prop(s,"water_auto_material")
        row=b.row(align=True)
        row.operator("campus3d.water_add",icon='MESH_PLANE')
        row.operator("campus3d.water_delete",icon='TRASH')
        try:
            denom=float(context.scene.get("campus3d_scale_denominator",0) or 0)
            vz=float(context.scene.get("campus3d_vertical_scale",1.0) or 1.0)
            if denom>0:
                b.label(text=f"manifest基準: 1:{int(denom)} / Z×{vz:.2f}")
        except Exception:
            pass

        b=layout.box()
        b.label(text="[B-4] 表示補助")
        b.operator("campus3d.focus",icon='VIEWZOOM')
        b.label(text="UV Editing / Shadingを開かなくてもOK")

        info=layout.box()
        info.label(text="Campus3D Workflow [B] | v0.4.6")
        info.label(text="・パッケージTerrain画像は自動貼付ONが標準")
        info.label(text="・旧方式OBJだけ既存マテリアル削除を選択可")
        info.label(text="・6ファイルをファイル単位で確実に4分類")
        info.label(text="・地図位置調整はリアルタイム反映対応")
        info.label(text="・海面を実標高0m基準でワンクリック生成")
        info.label(text="・[A] QGIS → [B] Blender → GLB → CampusMint")
        info.label(text="Powered by IINO Lab.")
        info.label(text="同範囲画像 → そのまま貼付")
        info.label(text="部分画像 → 移動/倍率/回転で手動調整")

classes=(
    Campus3DSettings,
    CAMPUS3D_OT_import_package,
    CAMPUS3D_OT_import,
    CAMPUS3D_OT_apply_map,
    CAMPUS3D_OT_guide,
    CAMPUS3D_OT_reset_map,
    CAMPUS3D_OT_clear_qgis_texture,
    CAMPUS3D_OT_reclassify,
    CAMPUS3D_OT_water_add,
    CAMPUS3D_OT_water_delete,
    CAMPUS3D_OT_focus,
    CAMPUS3D_PT_panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.campus3d_settings=PointerProperty(type=Campus3DSettings)

def unregister():
    del bpy.types.Scene.campus3d_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__=="__main__":
    register()
