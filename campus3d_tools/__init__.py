
bl_info = {
    "name": "Campus3D Tools",
    "author": "Akinaru Iino",
    "version": (0, 4, 18),
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
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty, StringProperty, EnumProperty
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
        raise RuntimeError("Terrain could not be detected")

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
    auto_orient: BoolProperty(name="Correct QGIS axes for Blender", default=True)

    real_width_m: FloatProperty(name="QGIS DEM real width X (m)", default=0.0, min=0.0, precision=3)
    real_height_m: FloatProperty(name="QGIS DEM real height Y (m)", default=0.0, min=0.0, precision=3)
    target_scale: EnumProperty(
        name="Target scale",
        items=[
            ('1000',"1:1000 (recommended for exercises)",""),
            ('500',"1:500",""),
            ('100',"1:100",""),
            ('1',"1:1 (real size)",""),
        ],
        default='1000'
    )
    auto_calibrate: BoolProperty(name="Auto-calibrate scale from real dimensions", default=True)

    geometry_clip: BoolProperty(name="Remove geometry outside Terrain", default=True)
    outside_margin: FloatProperty(name="Cleanup margin", default=0.002, min=0.0, soft_max=0.1)
    set_origins: BoolProperty(name="Set origin to each geometry", default=True)
    recenter_scene: BoolProperty(name="Move Terrain center to world origin", default=True)
    organize: BoolProperty(name="Organize collections", default=True)
    auto_frame: BoolProperty(name="Frame Terrain after import", default=True)

    map_image_path: StringProperty(name="Map image", subtype='FILE_PATH')
    map_shift_x_pct: FloatProperty(name="Horizontal shift (%)", default=0.0, soft_min=-100, soft_max=100, update=schedule_live_map_update)
    map_shift_y_pct: FloatProperty(name="Vertical shift (%)", default=0.0, soft_min=-100, soft_max=100, update=schedule_live_map_update)
    map_zoom_x_pct: FloatProperty(name="Horizontal scale (%)", default=100.0, min=1.0, soft_max=500, update=schedule_live_map_update)
    map_zoom_y_pct: FloatProperty(name="Vertical scale (%)", default=100.0, min=1.0, soft_max=500, update=schedule_live_map_update)
    map_rotation_deg: FloatProperty(name="Rotation (°)", default=0.0, min=-180.0, max=180.0, update=schedule_live_map_update)
    map_flip_x: BoolProperty(name="Flip horizontally", default=False, update=schedule_live_map_update)
    map_flip_y: BoolProperty(name="Flip vertically", default=False, update=schedule_live_map_update)
    map_opacity: FloatProperty(name="Opacity", default=1.0, min=0.05, max=1.0, subtype='FACTOR', update=schedule_live_map_update)
    map_live_update: BoolProperty(name="Live update", default=True)
    remove_qgis_materials_on_import: BoolProperty(name="Remove existing Terrain materials for legacy OBJ", default=False)
    purge_unused_qgis_images: BoolProperty(name="Remove unused legacy image data", default=False)
    replace_existing_package: BoolProperty(name="Replace existing Campus3D objects", default=True)
    apply_package_texture: BoolProperty(name="Apply package Terrain image automatically", default=True)
    remove_factory_cube: BoolProperty(
        name="Remove untouched startup Cube", default=True,
        description="Remove only the original Blender startup Cube on successful package import; never remove an edited or saved-project Cube"
    )
    water_elevation_m: FloatProperty(
        name="Water elevation (m)",
        description="Real-world elevation. For Campus3D packages, it is converted automatically to Blender Z using the manifest elevation origin and scale.",
        default=0.0,
        min=-100.0,
        max=100.0,
        precision=2
    )
    water_margin_m: FloatProperty(
        name="Water margin (m)",
        description="Real-world distance to extend the water plane beyond the Terrain boundary.",
        default=200.0,
        min=0.0,
        soft_max=2000.0,
        precision=1
    )
    water_auto_material: BoolProperty(
        name="Create water material automatically",
        default=True
    )

# ------------------------------------------------------------------------
# Operators
# ------------------------------------------------------------------------


def store_manifest_in_scene(scene, manifest, manifest_path=None):
    """Store Campus3D manifest metadata inside the .blend scene.

    The full manifest is preserved as JSON text for future compatibility,
    while frequently used geolocation/settings are also copied to explicit
    scene custom properties for easy access by future tools such as
    Campus3D Thermal.
    """
    try:
        scene["campus3d_manifest_json"] = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":")
        )
    except Exception:
        # Fallback to ASCII-safe JSON if a string cannot be serialized normally.
        scene["campus3d_manifest_json"] = json.dumps(
            manifest, ensure_ascii=True, separators=(",", ":")
        )

    if manifest_path:
        try:
            scene["campus3d_manifest_source"] = str(manifest_path)
        except Exception:
            pass

    geo = manifest.get("geolocation", {}) if isinstance(manifest, dict) else {}
    if isinstance(geo, dict):
        mesh_codes = geo.get("mesh_codes") or []
        second_mesh_codes = geo.get("second_mesh_codes") or []
        third_mesh_codes = geo.get("third_mesh_codes") or []

        # Blender ID properties do not reliably support arbitrary nested lists
        # across versions, so store list values as JSON strings.
        scene["campus3d_mesh_codes_json"] = json.dumps(mesh_codes, ensure_ascii=False)
        scene["campus3d_second_mesh_codes_json"] = json.dumps(second_mesh_codes, ensure_ascii=False)
        scene["campus3d_third_mesh_codes_json"] = json.dumps(third_mesh_codes, ensure_ascii=False)

        extent = geo.get("extent_wgs84") or {}
        if isinstance(extent, dict):
            for key in ("west", "south", "east", "north"):
                val = extent.get(key)
                if val is not None:
                    try:
                        scene[f"campus3d_extent_{key}"] = float(val)
                    except Exception:
                        pass

        center = geo.get("center_wgs84") or {}
        if isinstance(center, dict):
            lat = center.get("latitude")
            lon = center.get("longitude")
            if lat is not None:
                try:
                    scene["campus3d_center_latitude"] = float(lat)
                except Exception:
                    pass
            if lon is not None:
                try:
                    scene["campus3d_center_longitude"] = float(lon)
                except Exception:
                    pass

        for src_key, dst_key in (
            ("source_crs", "campus3d_source_crs"),
            ("projected_crs", "campus3d_projected_crs"),
        ):
            val = geo.get(src_key)
            if val:
                scene[dst_key] = str(val)

    # Explicit visual vs physical vertical scale metadata.
    vertical = manifest.get("vertical_scaling", {}) if isinstance(manifest, dict) else {}
    if isinstance(vertical, dict) and vertical:
        vvis = vertical.get("visual_vertical_scale")
        vphys = vertical.get("physical_vertical_scale", 1.0)
        if vvis is not None:
            try:
                scene["campus3d_visual_vertical_scale"] = float(vvis)
            except Exception:
                pass
        try:
            scene["campus3d_physical_vertical_scale"] = float(vphys)
        except Exception:
            scene["campus3d_physical_vertical_scale"] = 1.0
        scene["campus3d_terrain_exaggerated"] = bool(vertical.get("terrain_exaggerated", False))
        scene["campus3d_buildings_real_scale"] = bool(vertical.get("buildings_real_scale", True))
        mode = vertical.get("geometry_mode")
        if mode:
            scene["campus3d_geometry_mode"] = str(mode)
    else:
        # Backward-compatible fallback for older manifests.
        try:
            legacy = (manifest.get("model_size") or {}).get("z_multiplier")
            if legacy is not None:
                scene["campus3d_visual_vertical_scale"] = float(legacy)
                scene["campus3d_physical_vertical_scale"] = 1.0
                scene["campus3d_terrain_exaggerated"] = abs(float(legacy) - 1.0) > 1e-9
                scene["campus3d_buildings_real_scale"] = True
                scene["campus3d_geometry_mode"] = "terrain_exaggerated_buildings_real_scale"
        except Exception:
            pass

    # Copy several stable top-level settings for convenient inspection.
    for src_key, dst_key in (
        ("scale_denominator", "campus3d_scale_denominator"),
        ("vertical_scale", "campus3d_vertical_scale"),
        ("epsg", "campus3d_epsg"),
    ):
        if isinstance(manifest, dict) and src_key in manifest:
            val = manifest.get(src_key)
            try:
                if isinstance(val, (int, float)):
                    scene[dst_key] = val
                elif val is not None:
                    scene[dst_key] = str(val)
            except Exception:
                pass

    scene["campus3d_manifest_embedded"] = True


def get_campus3d_vertical_scaling(scene):
    """Return Campus3D vertical-scale metadata for physical analysis."""
    try:
        visual = float(scene.get("campus3d_visual_vertical_scale", 1.0))
    except Exception:
        visual = 1.0
    try:
        physical = float(scene.get("campus3d_physical_vertical_scale", 1.0))
    except Exception:
        physical = 1.0
    mode = str(scene.get(
        "campus3d_geometry_mode",
        "terrain_exaggerated_buildings_real_scale"
    ))
    return {
        "visual_vertical_scale": visual,
        "physical_vertical_scale": physical,
        "geometry_mode": mode,
        "terrain_z_to_physical_factor": (physical / visual) if abs(visual) > 1e-12 else 1.0,
    }


def load_embedded_manifest(scene):
    """Return embedded Campus3D manifest dict, or None when unavailable."""
    raw = scene.get("campus3d_manifest_json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None



# ------------------------------------------------------------------------
# v0.4.13: conservative identification of Blender's untouched startup cube.
# Do NOT decide based only on object name: a user's edited Cube must survive.
# Called before importing, and removal only after the full import succeeds.
# ------------------------------------------------------------------------

def _close_numbers(actual, expected, tol=0.005):
    try:
        return len(actual) == len(expected) and all(
            abs(float(a)-float(b)) <= tol for a,b in zip(actual, expected)
        )
    except (TypeError, ValueError):
        return False


def _inspect_startup_cube(scene):
    """Return (factory Cube, reason). A dirty *file* is not proof Cube changed.

    `bpy.data.is_dirty` becomes true for benign operations (for example opening
    an importer). Instead examine the actual scene, default object transforms,
    factory mesh, and material. Prefer false negatives to deleting user edits.
    The reason is surfaced after import when Cube is deliberately retained.
    """
    def skip(why):
        return None, why
    try:
        if bpy.data.filepath:
            return skip('The .blend file has already been saved')
        if len(bpy.data.scenes) != 1 or scene.name != 'Scene':
            return skip('The project is not a single default Scene')
        if {o.name for o in scene.objects} != {'Cube', 'Camera', 'Light'}:
            return skip('The scene contains objects other than the default Cube, Camera, and Light')
        if len(scene.collection.children) != 1 or scene.collection.children[0].name != 'Collection':
            return skip('The default collection structure was changed')
        cube = scene.objects.get('Cube')
        cam = scene.objects.get('Camera')
        light = scene.objects.get('Light')
        if cube is None or cube.type != 'MESH' or cam is None or cam.type != 'CAMERA' or light is None or light.type != 'LIGHT':
            return skip('Default startup object types do not match')
        if cube.data is None or cube.data.name != 'Cube' or cube.data.users != 1:
            return skip('Cube mesh was modified or is shared')
        if cube.parent is not None or cube.hide_viewport or cube.hide_render or cube.hide_get():
            return skip('Cube parenting or visibility was changed')
        # Camera/Light as well as Cube must retain Blender's factory positions.
        expected_positions = (
            (cube, (0, 0, 0)),
            (cam, (7.35889, -6.92579, 4.95831)),
            (light, (4.07625, 1.00545, 5.90386)),
        )
        for obj, position in expected_positions:
            if not _close_numbers(obj.location, position, 0.01):
                return skip(obj.name + ' position differs from startup defaults')
            if not _close_numbers(obj.scale, (1, 1, 1)):
                return skip(obj.name + ' scale differs from startup defaults')
            if obj.animation_data or len(obj.constraints) or obj.library or obj.override_library:
                return skip(obj.name + ' has constraints, animation, or library links')
        if not _close_numbers(cube.rotation_euler, (0, 0, 0)) or cube.rotation_mode != 'XYZ':
            return skip('Cube rotation differs from startup defaults')
        if not _close_numbers(cam.rotation_euler, (1.10932, 0, 0.814929), 0.02):
            return skip('Camera rotation differs from startup defaults')
        if not _close_numbers(light.rotation_euler, (0.650328, 0.055217, 1.866391), 0.02):
            return skip('Light rotation differs from startup defaults')
        if len(cube.modifiers) or len(cube.vertex_groups) or cube.data.shape_keys is not None:
            return skip('Cube has modifiers, vertex groups, or shape keys')
        mesh = cube.data
        if len(mesh.vertices) != 8 or len(mesh.edges) != 12 or len(mesh.polygons) != 6:
            return skip('Cube mesh vertex, edge, or face count was changed')
        if any(len(p.vertices) != 4 for p in mesh.polygons):
            return skip('Cube face topology was changed')
        if any(not _close_numbers(tuple(abs(float(c)) for c in v.co), (1, 1, 1)) for v in mesh.vertices):
            return skip('Cube vertex positions were changed')
        if len(cube.keys()) != 0:
            return skip('Cube has custom properties')
        if len(mesh.materials) != 1:
            return skip('Cube material slot count was changed')
        material = mesh.materials[0]
        if material is None or material.name != 'Material' or not material.use_nodes:
            return skip('Cube material differs from the startup default')
        nt = material.node_tree
        if nt is None or len(nt.nodes) != 2 or len(nt.links) != 1:
            return skip('Cube material nodes were changed')
        shader = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if shader is None:
            return skip('Cube shader was changed')
        color = shader.inputs.get('Base Color')
        metal = shader.inputs.get('Metallic')
        rough = shader.inputs.get('Roughness')
        if color is None or metal is None or rough is None:
            return skip('Cube shader settings differ from startup defaults')
        if not _close_numbers(color.default_value, (0.8, 0.8, 0.8, 1.0)):
            return skip('Cube base color differs from startup defaults')
        if abs(float(metal.default_value)) > 0.0001 or abs(float(rough.default_value) - 0.5) > 0.0001:
            return skip('Cube material parameters differ from startup defaults')
        return cube, 'Confirmed untouched startup Cube'
    except Exception as exc:
        return skip('Startup Cube safety check failed: ' + str(exc)[:90])


def _untouched_startup_cube(scene):
    """Backward compatible helper for the importer and existing extensions."""
    return _inspect_startup_cube(scene)[0]


def _redraw_campus3d_panels(wm):
    try:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D','STATUSBAR'}:
                    area.tag_redraw()
    except Exception:
        pass

# v0.4.14: a Blender *application* timer is used instead of a modal handler.
# ImportHelper opens a transient file browser. Adding a modal handler from its
# execute() is not reliable because that browser closes immediately afterwards.
# The app timer is independent of which editor receives keyboard/mouse events.
_CAMPUS3D_IMPORT_JOB = None


def _campus3d_import_context(job):
    """Find a live Blender 3D view after ImportHelper's file browser closes."""
    wm = bpy.context.window_manager
    for window in wm.windows:
        if window.scene != job.scene:
            continue
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region:
                    return window, area, region
    return None


def _campus3d_import_timer():
    """One import stage per timer call, always on Blender's main thread."""
    global _CAMPUS3D_IMPORT_JOB
    job = _CAMPUS3D_IMPORT_JOB
    if job is None:
        return None
    try:
        ctx = _campus3d_import_context(job)
        if ctx is None:
            job.waiting_for_view += 1
            if job.waiting_for_view > 40:  # 10 seconds, not an endless 0% state
                raise RuntimeError('Could not return to the 3D view after the file browser closed.')
            return 0.25
        win, area, region = ctx
        with bpy.context.temp_override(window=win, area=area, region=region,
                                      scene=job.scene, view_layer=win.view_layer):
            return job.tick(bpy.context)
    except Exception as exc:
        job.abort('Import failed: ' + str(exc))
        return None


class _Campus3DImportJob:
    """Pure Python state that survives returning from the file-browser operator.

    Never retain a Blender Operator RNA instance after execute() returns.
    """
    def __init__(self, context, path, manifest, files, base, tasks):
        self.scene = context.scene
        self.wm = context.window_manager
        self.filepath = path
        self._manifest = manifest
        self._files = files
        self._base = base
        self._tasks = tasks
        self._cube_to_remove = None
        self._cube_skip_reason = ''
        self._cube_outcome = ''
        if self.scene.campus3d_settings.remove_factory_cube:
            self._cube_to_remove, self._cube_skip_reason = _inspect_startup_cube(self.scene)
            if self._cube_to_remove is None:
                print('[Campus3D] Startup Cube retained: ' + self._cube_skip_reason)
        self._counts = {}
        self._all_imported = []
        self._step = 0
        self._phase = 'prepare'
        self.waiting_for_view = 0
        self.cancel_requested = False
        self._progress_started = False
        self._finished = False

    def report(self, types, message):
        # An ImportHelper operator can no longer report after it has returned.
        print('[Campus3D ' + ','.join(sorted(types)) + '] ' + str(message))

    def progress(self, percent, label):
        self.wm.campus3d_import_percent = max(0, min(100, int(percent)))
        self.wm.campus3d_import_stage = label
        if self._progress_started:
            try: self.wm.progress_update(self.wm.campus3d_import_percent)
            except Exception: pass
        _redraw_campus3d_panels(self.wm)

    def begin(self):
        self.wm.campus3d_import_running = True
        self.wm.campus3d_import_percent = 0
        self.wm.progress_begin(0, 100)
        self._progress_started = True
        self.progress(0, 'Starting: returning from the file browser')

    def complete(self, ok=True):
        global _CAMPUS3D_IMPORT_JOB
        if self._finished:
            return
        self._finished = True
        if self._progress_started:
            try: self.wm.progress_end()
            except Exception: pass
            self._progress_started = False
        self.wm.campus3d_import_running = False
        if _CAMPUS3D_IMPORT_JOB is self:
            _CAMPUS3D_IMPORT_JOB = None
        _redraw_campus3d_panels(self.wm)

    def abort(self, message):
        self.progress(self.wm.campus3d_import_percent, message)
        self.report({'ERROR'}, message)
        self.complete(ok=False)
        # A non-blocking message is displayed after Blender leaves the importer.
        try:
            def draw(popup, context):
                popup.layout.label(text=str(message)[:180], icon='ERROR')
            self.wm.popup_menu(draw, title='Campus3D Import', icon='ERROR')
        except Exception: pass

    def tick(self, context):
        if self.cancel_requested:
            self.abort('Campus3D import canceled (already imported objects remain)')
            return None
        if self._phase == 'prepare':
            s=self.scene.campus3d_settings
            if s.replace_existing_package:
                clear_campus3d_scene()
            root=ensure_collection('Campus3D')
            self._categories={name:ensure_collection(name,root) for name,_ in CATEGORY_MAP}
            self._categories['Other']=ensure_collection('Other',root)
            self._phase='announce_obj'
            self.progress(5,'Ready: importing OBJ files sequentially')
            return 0.12
        if self._phase == 'announce_obj':
            category, _ = self._tasks[self._step]
            self.progress(5+int(self._step*75/len(self._tasks)),
                          f'Importing: {category} ({self._step+1}/{len(self._tasks)})')
            self._phase='import_obj'
            return 0.12  # let the UI paint before the next large OBJ read
        if self._phase == 'import_obj':
            category, filepath=self._tasks[self._step]
            imported=import_obj_file(filepath)
            self._counts[category]=len(imported)
            for index, obj in enumerate(imported):
                obj['campus3d_package_category']=category
                obj.name=category if len(imported)==1 else f'{category}_{index+1:03d}'
                move_object_to_collection(obj,self._categories[category])
            self._all_imported.extend(imported)
            self._step += 1
            self.progress(5+int(self._step*75/len(self._tasks)),
                          f'Loaded: {category} ({self._step}/{len(self._tasks)})')
            self._phase = 'announce_obj' if self._step<len(self._tasks) else 'announce_finish'
            return 0.12
        if self._phase == 'announce_finish':
            self.progress(85,'Finalizing UVs, Terrain image, and coordinates')
            self._phase='finish'
            return 0.12
        if self._phase == 'finish':
            # Import/layout/materials are complete here, but Blender may still
            # spend time updating the dependency graph and drawing the viewport.
            # Do not immediately show a misleading "100% = UI ready" signal.
            CAMPUS3D_OT_import_package._finish_import(self, context)
            self.progress(96, 'Model loaded: preparing the 3D viewport...')
            self._phase = 'display_update'
            return 0.25
        if self._phase == 'display_update':
            # Yield a UI iteration between the geometry work and final status.
            # The redraw is intentionally only requested, not forced: blocking
            # redraw operators here can make large meshes appear frozen again.
            self.progress(98, 'Updating 3D viewport; Blender may remain busy...')
            self._phase = 'display_complete'
            return 0.5
        if self._phase == 'display_complete':
            # This confirms the import pipeline, NOT that the GPU finished all
            # viewport drawing. Keep the guidance visible after 100% as well.
            outcome = getattr(self, '_cube_outcome', '')
            self.progress(100, 'Import complete (100%)' + (' / ' + outcome if outcome else ''))
            self.complete(ok=True)
            return None
        raise RuntimeError('Unknown import phase: ' + str(self._phase))


class CAMPUS3D_OT_cancel_package(Operator):
    bl_idname='campus3d.cancel_package_import'
    bl_label='Cancel Campus3D Import'
    bl_description='Stop Campus3D import after the currently loading OBJ finishes'

    def execute(self, context):
        if _CAMPUS3D_IMPORT_JOB is not None:
            _CAMPUS3D_IMPORT_JOB.cancel_requested = True
            self.report({'INFO'}, 'Cancel requested; the current OBJ will finish before stopping.')
        return {'FINISHED'}


class CAMPUS3D_OT_import_package(Operator, ImportHelper):
    bl_idname="campus3d.import_package"
    bl_label="Import Campus3D Package"
    bl_description="Import Terrain, roads, and four building categories separately from a campus3d_manifest.json created by GSI2Campus3D."
    filename_ext=".json"
    filter_glob:StringProperty(default="*manifest*.json", options={'HIDDEN'})

    def execute(self, context):
        global _CAMPUS3D_IMPORT_JOB
        wm=context.window_manager
        if _CAMPUS3D_IMPORT_JOB is not None or wm.campus3d_import_running:
            self.report({'WARNING'}, 'A Campus3D package is already being imported.')
            return {'CANCELLED'}
        chosen_path=os.path.abspath(bpy.path.abspath(self.filepath))
        if os.path.basename(chosen_path).lower() == 'campus3d_metadata.json':
            sibling=os.path.join(os.path.dirname(chosen_path),'campus3d_manifest.json')
            if os.path.isfile(sibling):
                chosen_path=sibling
                self.report({'INFO'},'Using campus3d_manifest.json from the same folder.')
        try:
            with open(chosen_path,'r',encoding='utf-8') as fh:
                manifest=json.load(fh)
            if not isinstance(manifest,dict) or manifest.get('format') != 'Campus3D Blender Package':
                raise ValueError('Please select campus3d_manifest.json.')
            base=os.path.dirname(chosen_path)
            files=manifest.get('files',{})
            if not isinstance(files,dict):
                raise ValueError('Invalid files section in manifest.')
            required=['Terrain','Building_Normal_Small','Building_Normal_Large','Building_Solid_Small','Building_Solid_Large']
            missing=[k for k in required if k not in files or not os.path.isfile(os.path.join(base,files[k]))]
            if missing:
                raise FileNotFoundError('Missing required OBJ files: '+', '.join(missing))
            order=['Terrain','Roads','Building_Normal_Small','Building_Normal_Large','Building_Solid_Small','Building_Solid_Large']
            tasks=[]
            for category in order:
                rel=files.get(category)
                if not rel:
                    continue
                fp=os.path.abspath(os.path.join(base,rel))
                if os.path.isfile(fp):
                    tasks.append((category,fp))
            if not tasks:
                raise ValueError('No OBJ files available to import.')
            job=_Campus3DImportJob(context, chosen_path, manifest, files, base, tasks)
            _CAMPUS3D_IMPORT_JOB = job
            job.begin()
            # Independent of Blender's transient file browser/modal events.
            bpy.app.timers.register(_campus3d_import_timer, first_interval=0.2)
        except Exception as exc:
            if _CAMPUS3D_IMPORT_JOB is not None:
                _CAMPUS3D_IMPORT_JOB.abort('Could not start import: '+str(exc))
            self.report({'ERROR'}, f'Campus3D package import: {exc}')
            return {'CANCELLED'}
        self.report({'INFO'},'Campus3D import started. Progress appears in the Campus3D sidebar.')
        return {'FINISHED'}

    def _finish_import(self,context):
        manifest=self._manifest
        files=self._files
        base=self._base
        s=context.scene.campus3d_settings
        all_imported=self._all_imported
        counts=self._counts

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
        store_manifest_in_scene(context.scene, manifest, self.filepath)

        # Package Terrain texture is aligned to the full DEM extent.
        s.map_shift_x_pct=0.0; s.map_shift_y_pct=0.0
        s.map_zoom_x_pct=100.0; s.map_zoom_y_pct=100.0
        s.map_rotation_deg=0.0; s.map_flip_x=False; s.map_flip_y=False
        generate_global_uv(all_imported,s)

        tex_rel=files.get('Terrain_Texture')
        tex_applied=False
        if s.apply_package_texture and tex_rel:
            tex_path=os.path.join(base,tex_rel)
            if os.path.isfile(tex_path):
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

        # Delete *only* the original untouched startup Cube, detected before
        # the import. Never use `obj in bpy_prop_collection`: Blender expects
        # a string key there and raises TypeError for an Object instance.
        # This optional convenience must never invalidate an otherwise
        # successful multi-OBJ import (or leave the progress bar at 85%).
        default_cube = getattr(self, '_cube_to_remove', None)
        if default_cube is not None:
            try:
                candidate = context.scene.objects.get('Cube')
                if (candidate is not None and default_cube.name == 'Cube'
                        and candidate.as_pointer() == default_cube.as_pointer()):
                    bpy.data.objects.remove(default_cube, do_unlink=True)
                    self._cube_outcome = 'Removed untouched startup Cube'
                else:
                    self._cube_outcome = 'Cube preserved: object changed during import'
            except Exception as exc:
                self._cube_outcome = 'Cube preserved: deletion failed'
                self.report({'WARNING'}, 'Could not remove startup Cube: ' + str(exc))
        elif s.remove_factory_cube and context.scene.objects.get('Cube') is not None:
            self._cube_outcome = 'Cube preserved: ' + getattr(self, '_cube_skip_reason', 'could not verify untouched startup Cube')

        summary=' / '.join(f'{k}:{v}' for k,v in counts.items())
        vz=float(manifest.get('vertical_scale',1.0) or 1.0)
        building_mode=manifest.get('building_vertical_mode','legacy_all_z_scaled')
        if vz != 1.0 and building_mode != 'terrain_exaggeration_only':
            self.report({'WARNING'},
                'Legacy Campus3D package: building heights may be exaggerated when the Z multiplier differs from 1.')
        self.report({'INFO'},f"Campus3D package import complete 1:{denom} / Z×{vz:.2f} / Terrain image:{'ON' if tex_applied else 'None/OFF'} / {summary}")

class CAMPUS3D_OT_import(Operator, ImportHelper):
    bl_idname="campus3d.import_obj"
    bl_label="Import Campus3D OBJ"
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
            self.report({'ERROR'},"No newly imported objects were detected")
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
            self.report({'ERROR'},"Terrain could not be detected")
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
        self.report({'INFO'},f"Import complete / OBJ names {bound}/{source_count} merged / Terrain {post[0]:.3f}×{post[1]:.3f}m / UV ready")
        return {'FINISHED'}


class CAMPUS3D_OT_apply_map(Operator):
    bl_idname="campus3d.apply_map"
    bl_label="Apply / Update Map"
    def execute(self,context):
        s=context.scene.campus3d_settings
        path=bpy.path.abspath(s.map_image_path).strip()
        if not path or not os.path.exists(path):
            self.report({'ERROR'},"Select a map image")
            return {'CANCELLED'}

        objs=list(context.scene.objects)
        if not terrain_objects(objs):
            self.report({'ERROR'},"Terrain could not be detected")
            return {'CANCELLED'}

        generate_global_uv(objs,s)
        # Hard reset inherited QGIS/OBJ terrain materials so the new map always wins.
        strip_qgis_terrain_materials(objs, False)
        try:
            img=load_or_reload_image(path)
        except Exception as e:
            self.report({'ERROR'},f"Could not read image: {e}")
            return {'CANCELLED'}

        mat=ensure_map_material(img,s.map_opacity)
        n=assign_material(objs,mat)
        self.report({'INFO'},f"{n} Terrain object(s) updated with map image")
        return {'FINISHED'}

class CAMPUS3D_OT_guide(Operator):
    bl_idname="campus3d.show_guide"
    bl_label="Show Alignment Guide"
    def execute(self,context):
        s=context.scene.campus3d_settings
        objs=list(context.scene.objects)
        if generate_global_uv(objs,s)==0:
            self.report({'ERROR'},"Terrain could not be detected")
            return {'CANCELLED'}
        strip_qgis_terrain_materials(objs, False)
        assign_material(objs,make_guide_material())
        self.report({'INFO'},"Displayed a 10×10 alignment guide")
        return {'FINISHED'}

class CAMPUS3D_OT_reset_map(Operator):
    bl_idname="campus3d.reset_map"
    bl_label="Reset Alignment"
    def execute(self,context):
        s=context.scene.campus3d_settings
        s.map_shift_x_pct=0.0; s.map_shift_y_pct=0.0
        s.map_zoom_x_pct=100.0; s.map_zoom_y_pct=100.0
        s.map_rotation_deg=0.0; s.map_flip_x=False; s.map_flip_y=False
        generate_global_uv(list(context.scene.objects),s)
        self.report({'INFO'},"Alignment reset to defaults")
        return {'FINISHED'}


class CAMPUS3D_OT_clear_qgis_texture(Operator):
    bl_idname="campus3d.clear_qgis_texture"
    bl_label="Remove QGIS Terrain Image Completely"
    def execute(self,context):
        s=context.scene.campus3d_settings
        objs=list(context.scene.objects)
        tc, slots, imgs = strip_qgis_terrain_materials(objs, s.purge_unused_qgis_images)
        if tc == 0:
            self.report({'ERROR'},"Terrain could not be detected")
            return {'CANCELLED'}
        self.report({'INFO'},f"{tc} Terrain / {slots} material slot(s) removed, images {imgs} removed")
        return {'FINISHED'}


class CAMPUS3D_OT_reclassify(Operator):
    bl_idname="campus3d.reclassify"
    bl_label="Reclassify Building Collections"
    bl_description="Reclassify buildings into four categories using OBJ names, mesh names, material names, and related data."

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
        self.report({'INFO'}, "Reclassified: " + msg)
        return {'FINISHED'}


class CAMPUS3D_OT_water_add(Operator):
    bl_idname="campus3d.water_add"
    bl_label="Add / Update Water Plane"
    bl_description="Create or update a water plane covering the Terrain using the specified real-world elevation and margin."

    def execute(self,context):
        try:
            obj, z, margin = create_or_update_water(context)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        denom, vscale, origin_z = campus3d_scale_info(context.scene)
        self.report(
            {'INFO'},
            f"Water plane updated: elevation {context.scene.campus3d_settings.water_elevation_m:.2f} m "
            f"→ Blender Z {z:.4f} m / 1:{int(denom)} / Z×{vscale:.2f}"
        )
        return {'FINISHED'}

class CAMPUS3D_OT_water_delete(Operator):
    bl_idname="campus3d.water_delete"
    bl_label="Delete Water Plane"
    bl_description="Delete the Campus3D Water object."

    def execute(self,context):
        n = delete_water()
        if n:
            self.report({'INFO'}, "Water plane deleted")
        else:
            self.report({'INFO'}, "No water plane to delete")
        return {'FINISHED'}

class CAMPUS3D_OT_focus(Operator):
    bl_idname="campus3d.focus"
    bl_label="Frame Entire Terrain"
    def execute(self,context):
        ts=terrain_objects(list(context.scene.objects))
        if not ts:
            self.report({'ERROR'},"Terrain could not be detected")
            return {'CANCELLED'}
        frame_objects(context,ts)
        return {'FINISHED'}

class CAMPUS3D_OT_recalculate_building_normals(Operator):
    """Recalculate outward-facing normals for original or GLB_READY building meshes.

    Especially useful for packages exported before GSI2Campus3D v0.4.24.
    Never changes Terrain, roads, or unrelated meshes.
    """
    bl_idname = "campus3d.recalculate_building_normals"
    bl_label = "Recalculate Building Normals"
    bl_description = "Recalculate normals of building meshes in the chosen collection (for legacy QGIS OBJ packages)"
    bl_options = {'REGISTER', 'UNDO'}

    target: EnumProperty(
        name="Target",
        items=[
            ('GLB_READY', "GLB_READY (export copy)", "Modify only exported building copies"),
            ('Campus3D', "Campus3D (original)", "Modify original building meshes"),
        ],
        default='GLB_READY',
    )

    def execute(self, context):
        root = bpy.data.collections.get(self.target)
        if root is None:
            self.report({'ERROR'}, f"Collection {self.target} was not found")
            return {'CANCELLED'}
        count = 0
        faces = 0
        for obj in root.all_objects:
            if obj.type != 'MESH':
                continue
            cat = str(obj.get('campus3d_package_category', ''))
            if not (obj.name.startswith('Building_') or cat.startswith('Building_')):
                continue
            if not obj.data.polygons:
                continue
            # Never mutate a shared mesh datablock outside the target collection.
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            bm = bmesh.new()
            try:
                bm.from_mesh(obj.data)
                bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
                faces += len(bm.faces)
                bm.to_mesh(obj.data)
                obj.data.update()
                count += 1
            finally:
                bm.free()
        if not count:
            self.report({'WARNING'}, "No building mesh objects found")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Recalculated normals for {count} building objects / {faces:,} faces")
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
        brand.label(text="Add-on v0.4.18")
        flow=brand.row()
        flow.scale_y=0.95
        flow.label(text="[A] QGIS  →  [B] Blender  →  GLB")
        brand.label(text="← Continues from [A] GSI2Campus3D")

        b=layout.box()
        b.label(text="[B-1] Recommended: GSI2Campus3D Package")
        b.prop(s,"replace_existing_package")
        b.prop(s,"apply_package_texture")
        b.prop(s,"remove_factory_cube")
        wm=context.window_manager
        row=b.row()
        row.enabled=not wm.campus3d_import_running
        row.operator("campus3d.import_package",icon='IMPORT')
        if wm.campus3d_import_running:
            b.operator("campus3d.cancel_package_import",icon="CANCEL")
        if wm.campus3d_import_running or wm.campus3d_import_percent == 100:
            b.progress(factor=wm.campus3d_import_percent/100.0,
                       type='BAR',text=f"Import {wm.campus3d_import_percent}%")
            b.label(text=wm.campus3d_import_stage,
                    icon='TIME' if wm.campus3d_import_running else 'CHECKMARK')
            if wm.campus3d_import_running and wm.campus3d_import_percent >= 85:
                b.label(text='Updating viewport: please wait until Blender responds', icon='INFO')
            elif wm.campus3d_import_percent == 100:
                b.label(text='Viewport updates may briefly make Blender unresponsive', icon='INFO')
        elif wm.campus3d_import_stage:
            # Do not leave a misleading 85% bar after an import failure.
            b.label(text=wm.campus3d_import_stage[:95],icon='ERROR')
        b.label(text="Select campus3d_manifest.json")
        b.label(text="Categories, scale, origin, and Z multiplier are read automatically from the manifest.")
        b.label(text="QGIS packages from v0.3.2 onward can include a Terrain image.")

        b=layout.box()
        b.label(text="[B-Legacy] Manual Single-OBJ Import")
        b.prop(s,"real_width_m")
        b.prop(s,"real_height_m")
        b.prop(s,"target_scale")
        b.prop(s,"auto_calibrate")

        b=layout.box()
        b.label(text="Legacy OBJ Options")
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
        b.label(text="[B-Normals] Legacy building face repair")
        b.label(text="QGIS v0.4.24+ fixes new exports at source.")
        op=b.operator("campus3d.recalculate_building_normals", text="Repair GLB_READY Building Normals", icon='NORMALS_FACE')
        op.target='GLB_READY'
        op=b.operator("campus3d.recalculate_building_normals", text="Repair Campus3D Building Normals", icon='NORMALS_FACE')
        op.target='Campus3D'
        b.label(text="For GLB Preflight export, repair GLB_READY copies.")

        b=layout.box()
        b.label(text="[B-2] Apply Map to Terrain")
        b.prop(s,"map_image_path",text="Map image")
        b.operator("campus3d.clear_qgis_texture",icon='TRASH')
        b.operator("campus3d.apply_map",icon='IMAGE_DATA')
        b.operator("campus3d.show_guide",icon='UV')

        b.separator()
        b.label(text="Adjust partial maps or alignment offsets")
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
            b.operator("campus3d.apply_map",text="Apply Adjustment",icon='FILE_REFRESH')
        else:
            b.label(text="Numeric changes are applied automatically after about 0.05 seconds.")
        b.operator("campus3d.reset_map",icon='LOOP_BACK')

        b=layout.box()
        b.label(text="[B-3] Water Plane")
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
                b.label(text=f"Manifest scale: 1:{int(denom)} / Z×{vz:.2f}")
        except Exception:
            pass

        b=layout.box()
        b.label(text="[B-4] Display Tools")
        b.operator("campus3d.focus",icon='VIEWZOOM')
        b.label(text="No need to open UV Editing or Shading.")

        info=layout.box()
        info.label(text="Campus3D Workflow [B] | v0.4.15")
        info.label(text="• Automatic package Terrain image application is enabled by default.")
        info.label(text="• Existing material removal is optional for legacy OBJ imports.")
        info.label(text="• Six files are reliably organized into four building categories.")
        info.label(text="• Map alignment supports live updates.")
        info.label(text="• Create a water plane at real-world elevation 0 m with one click.")
        info.label(text="[A] QGIS -> [B] Blender -> GLB -> CampusMint")
        info.label(text="Powered by IINO Lab.")
        info.label(text="Same-extent image → apply directly")
        info.label(text="Partial image → adjust shift, scale, and rotation manually")

classes=(
    Campus3DSettings,
    CAMPUS3D_OT_import_package,
    CAMPUS3D_OT_cancel_package,
    CAMPUS3D_OT_import,
    CAMPUS3D_OT_apply_map,
    CAMPUS3D_OT_guide,
    CAMPUS3D_OT_reset_map,
    CAMPUS3D_OT_clear_qgis_texture,
    CAMPUS3D_OT_reclassify,
    CAMPUS3D_OT_water_add,
    CAMPUS3D_OT_water_delete,
    CAMPUS3D_OT_focus,
    CAMPUS3D_OT_recalculate_building_normals,
    CAMPUS3D_PT_panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.campus3d_settings=PointerProperty(type=Campus3DSettings)
    bpy.types.WindowManager.campus3d_import_running=BoolProperty(default=False,options={'SKIP_SAVE'})
    bpy.types.WindowManager.campus3d_import_percent=IntProperty(default=0,min=0,max=100,options={'SKIP_SAVE'})
    bpy.types.WindowManager.campus3d_import_stage=StringProperty(default='',options={'SKIP_SAVE'})

def unregister():
    global _CAMPUS3D_IMPORT_JOB
    if _CAMPUS3D_IMPORT_JOB is not None:
        _CAMPUS3D_IMPORT_JOB.abort('Import aborted because the add-on was disabled')
        _CAMPUS3D_IMPORT_JOB = None
    try:
        if bpy.app.timers.is_registered(_campus3d_import_timer):
            bpy.app.timers.unregister(_campus3d_import_timer)
    except Exception:
        pass
    del bpy.types.Scene.campus3d_settings
    for name in ('campus3d_import_stage','campus3d_import_percent','campus3d_import_running'):
        try: delattr(bpy.types.WindowManager,name)
        except AttributeError: pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__=="__main__":
    register()
