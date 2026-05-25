bl_info = {
    "name": "Frame by Plane",
    "author": "Alessandro Pannoli",
    "version": (2, 20, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Frame by Plane",
    "description": "Import image sequences as controllable animation planes with folders, fast import, scene split and profiling.",
    "category": "Animation",
}

import bpy
import os
import subprocess
import tempfile
import sys
import math
import mathutils
import time
import re
import bpy.utils.previews
from bpy.props import (
    StringProperty, IntProperty, BoolProperty, FloatProperty, FloatVectorProperty,
    CollectionProperty, PointerProperty, EnumProperty
)
from bpy.types import PropertyGroup, Operator, Panel, UIList

try:
    from . import profiling as fbp_profiling
except Exception:
    import profiling as fbp_profiling



try:
    from .constants import (
        STRIP_COLORS_DICT,
        COLOR_ENUM_ITEMS,
        preview_collections,
        addon_keymaps,
        FBP_SUPPORTED_IMAGE_EXT,
        FBP_TECHNICAL_MAP_SUFFIXES,
        FBP_PROJECT_COLLECTION_PREFIX,
        FBP_ADDON_URL,
        FBP_SUPPORT_EMAIL,
    )
    from .path_utils import (
        natural_sort_key,
        is_supported_image_file,
        is_hidden_import_name,
        is_technical_map_file,
        clean_layer_name_from_path,
        ensure_folder,
    )
except Exception:
    from constants import (
        STRIP_COLORS_DICT,
        COLOR_ENUM_ITEMS,
        preview_collections,
        addon_keymaps,
        FBP_SUPPORTED_IMAGE_EXT,
        FBP_TECHNICAL_MAP_SUFFIXES,
        FBP_PROJECT_COLLECTION_PREFIX,
        FBP_ADDON_URL,
        FBP_SUPPORT_EMAIL,
    )
    from path_utils import (
        natural_sort_key,
        is_supported_image_file,
        is_hidden_import_name,
        is_technical_map_file,
        clean_layer_name_from_path,
        ensure_folder,
    )

_last_fbp_update = 0.0
_fbp_render_guard_active = False



# ── HELPERS ──────────────────────────────────────────────────────────────────

def is_fbp_gp_object(obj):
    return False


def is_fbp_image_rig(obj):
    return bool(obj and getattr(obj, 'is_fbp_control', False))


def is_fbp_layer_object(obj):
    return is_fbp_image_rig(obj)


def fbp_layer_type_label(obj):
    return 'IMG' if is_fbp_image_rig(obj) else ''


def safe_collection_color_tag(collection, fallback='COLOR_09'):
    try:
        tag = getattr(collection, 'color_tag', fallback)
        return tag if tag in STRIP_COLORS_DICT else fallback
    except Exception:
        return fallback


def set_collection_color_tag(collection, color_tag):
    if not collection or color_tag not in STRIP_COLORS_DICT:
        return
    try:
        collection.color_tag = color_tag
    except Exception:
        pass


def make_color_variant(color_tag, index=0):
    base = STRIP_COLORS_DICT.get(color_tag, STRIP_COLORS_DICT['COLOR_09'])
    # Micro-variazioni leggere: mantengono il gruppo cromatico ma rendono i rig più leggibili.
    offsets = (-0.10, -0.05, 0.0, 0.06, 0.12, -0.02, 0.09)
    delta = offsets[index % len(offsets)]
    r, g, b, a = base
    if index % 3 == 0:
        r += delta
    elif index % 3 == 1:
        g += delta
    else:
        b += delta
    # Piccola compensazione di luminosità.
    lum = 1.0 + (delta * 0.35)
    return (
        max(0.0, min(1.0, r * lum)),
        max(0.0, min(1.0, g * lum)),
        max(0.0, min(1.0, b * lum)),
        a,
    )


def get_or_create_child_collection(parent_collection, name, color_tag=None):
    parent_collection = parent_collection or bpy.context.scene.collection
    for child in parent_collection.children:
        if child.name == name:
            coll = child
            break
    else:
        coll = bpy.data.collections.new(name)
        parent_collection.children.link(coll)
    try:
        coll.is_fbp_collection = True
    except Exception:
        pass
    if color_tag:
        set_collection_color_tag(coll, color_tag)
    return coll


def move_object_to_collection(obj, collection):
    if not obj or not collection:
        return
    try:
        if obj.name not in collection.objects:
            collection.objects.link(obj)
    except Exception:
        try:
            collection.objects.link(obj)
        except Exception:
            pass
    for coll in list(obj.users_collection):
        if coll != collection:
            try:
                coll.objects.unlink(obj)
            except Exception:
                pass


def get_primary_fbp_collection(obj):
    if not obj:
        return None
    try:
        if getattr(obj, 'fbp_collection_name', ''):
            coll = bpy.data.collections.get(obj.fbp_collection_name)
            if coll:
                return coll
    except Exception:
        pass
    try:
        for coll in obj.users_collection:
            if getattr(coll, 'is_fbp_collection', False):
                return coll
        return obj.users_collection[0] if obj.users_collection else None
    except Exception:
        return None


def is_layer_item_visible_in_collections(context, item):
    try:
        rig = item.obj
    except ReferenceError:
        return False
    if not rig or not is_fbp_layer_object(rig):
        return False
    try:
        # visible_get recepisce hide/exclude delle Collections nel View Layer.
        return bool(rig.visible_get(view_layer=context.view_layer))
    except TypeError:
        try:
            return bool(rig.visible_get())
        except Exception:
            return object_in_scene(rig, context.scene)
    except Exception:
        return object_in_scene(rig, context.scene)


def visible_layer_indices(context, same_collection_as=None):
    indices = []
    target_collection = get_primary_fbp_collection(same_collection_as) if same_collection_as else None
    for i, item in enumerate(context.scene.fbp_layers):
        try:
            rig = item.obj
            if not rig or not is_fbp_layer_object(rig):
                continue
            if target_collection and get_primary_fbp_collection(rig) != target_collection:
                continue
            if is_layer_item_visible_in_collections(context, item):
                indices.append(i)
        except ReferenceError:
            pass
    return indices


def apply_collection_color_to_layer(obj, color_tag=None, variant_index=None, push_collection=False):
    if not obj or not is_fbp_layer_object(obj):
        return
    coll = get_primary_fbp_collection(obj)
    if color_tag is None and coll:
        color_tag = safe_collection_color_tag(coll, getattr(obj, 'fbp_color_tag', 'COLOR_09'))
    if color_tag not in STRIP_COLORS_DICT:
        color_tag = getattr(obj, 'fbp_color_tag', 'COLOR_09')
        if color_tag not in STRIP_COLORS_DICT:
            color_tag = 'COLOR_09'
    if getattr(obj, 'fbp_color_tag', None) != color_tag:
        obj.fbp_color_tag = color_tag
    if variant_index is None:
        variant_index = getattr(obj, 'fbp_color_variant_index', 0)
    obj.color = make_color_variant(color_tag, variant_index)
    plane = getattr(obj, 'fbp_plane_target', None)
    if plane:
        try:
            plane.color = obj.color
        except Exception:
            pass
    if push_collection and coll:
        set_collection_color_tag(coll, color_tag)


def apply_collection_color_to_rig(rig, color_tag=None, variant_index=None, push_collection=False):
    apply_collection_color_to_layer(rig, color_tag, variant_index, push_collection)


def sync_collection_colors_to_rigs(context):
    if not context:
        return
    counters = {}
    for item in context.scene.fbp_layers:
        try:
            rig = item.obj
            if not rig or not is_fbp_layer_object(rig):
                continue
            if not getattr(rig, 'fbp_follow_collection_color', True):
                continue
            coll = get_primary_fbp_collection(rig)
            if not coll:
                continue
            tag = safe_collection_color_tag(coll, None)
            if tag not in STRIP_COLORS_DICT:
                continue
            key = coll.name
            idx = counters.get(key, 0)
            counters[key] = idx + 1
            rig.fbp_color_variant_index = idx
            apply_collection_color_to_layer(rig, tag, idx, push_collection=False)
        except ReferenceError:
            pass




# ── COLLECTION TREE / PROJECT HELPERS ───────────────────────────────────────

def find_layer_collection(layer_collection, collection):
    """Return the ViewLayer LayerCollection wrapper for a bpy.data Collection."""
    if not layer_collection or not collection:
        return None
    try:
        if layer_collection.collection == collection:
            return layer_collection
    except Exception:
        pass
    for child in getattr(layer_collection, 'children', []):
        found = find_layer_collection(child, collection)
        if found:
            return found
    return None


def collection_is_hidden_in_view_layer(context, collection):
    if not collection:
        return False
    try:
        if getattr(collection, 'hide_viewport', False):
            return True
    except Exception:
        pass
    try:
        layer_coll = find_layer_collection(context.view_layer.layer_collection, collection)
        if layer_coll and (getattr(layer_coll, 'hide_viewport', False) or getattr(layer_coll, 'exclude', False)):
            return True
    except Exception:
        pass
    return False


def collection_has_fbp_content(collection, recursive=True):
    if not collection:
        return False
    try:
        for obj in collection.objects:
            if is_fbp_layer_object(obj):
                return True
        if recursive:
            for child in collection.children:
                if collection_has_fbp_content(child, True):
                    return True
    except Exception:
        pass
    return False


def get_direct_fbp_rigs_in_collection(context, collection):
    rigs = []
    if not collection:
        return rigs
    order = []
    for item in context.scene.fbp_layers:
        try:
            if item.obj and is_fbp_layer_object(item.obj):
                order.append(item.obj)
        except ReferenceError:
            pass
    try:
        for rig in order:
            if any(coll == collection for coll in rig.users_collection):
                rigs.append(rig)
    except Exception:
        pass
    return sort_rigs_by_depth_for_layer_view(context, rigs)


def iter_fbp_rigs_in_collection(collection, recursive=True):
    if not collection:
        return
    seen = set()
    try:
        for obj in collection.objects:
            if is_fbp_layer_object(obj) and obj.name not in seen:
                seen.add(obj.name)
                yield obj
        if recursive:
            for child in collection.children:
                for rig in iter_fbp_rigs_in_collection(child, True):
                    if rig.name not in seen:
                        seen.add(rig.name)
                        yield rig
    except Exception:
        return


def get_child_fbp_collections(collection):
    if not collection:
        return []
    try:
        return sorted(
            [child for child in collection.children if collection_has_fbp_content(child, True)],
            key=lambda c: natural_sort_key(c.name)
        )
    except Exception:
        return []


def get_top_fbp_collections(context):
    scene_coll = context.scene.collection
    roots = []
    try:
        for coll in scene_coll.children:
            if collection_has_fbp_content(coll, True):
                roots.append(coll)
    except Exception:
        pass
    return sorted(roots, key=lambda c: natural_sort_key(c.name))


def get_layer_item_for_rig(context, rig):
    if not rig:
        return None
    for item in context.scene.fbp_layers:
        try:
            if item.obj == rig:
                return item
        except ReferenceError:
            pass
    return None


def indent_row(row, depth):
    for _ in range(max(0, depth)):
        row.label(text="", icon='BLANK1')


def draw_fbp_layer_row(layout, context, rig, depth=0):
    item = get_layer_item_for_rig(context, rig)
    if not item:
        return
    row = layout.row(align=True)
    indent_row(row, depth)

    sel_icon = 'CHECKBOX_HLT' if item.selected else 'CHECKBOX_DEHLT'
    row.prop(item, "selected", text="", icon=sel_icon, emboss=False)


    if context.scene.fbp_show_previews:
        preview = get_layer_thumbnail(rig)
        if preview:
            row.template_icon(icon_value=preview.icon_id, scale=0.8)
        else:
            row.label(text="", icon='STRIP_' + rig.fbp_color_tag)
    else:
        row.label(text="", icon='STRIP_' + rig.fbp_color_tag)

    op_name = row.operator("fbp.select_layer_exclusive", text=rig.name, emboss=False)
    op_name.rig_name = rig.name

    row.prop(rig, "fbp_cam_depth", text="")
    row.separator()
    row.label(text=f"F.{len(rig.fbp_images)}")

    lock_icon = 'LOCKED' if item.rig_locked else 'UNLOCKED'
    row.prop(item, "rig_locked", text="", icon=lock_icon, emboss=False)

    plane = rig.fbp_plane_target
    if plane:
        plane_icon = 'RESTRICT_SELECT_ON' if item.plane_locked else 'RESTRICT_SELECT_OFF'
        row.prop(item, "plane_locked", text="", icon=plane_icon, emboss=False)
    else:
        row.label(text="", icon='BLANK1')

    solo_icon = 'OUTLINER_OB_LIGHT' if item.solo_view else 'LIGHT'
    row.prop(item, "solo_view", text="", icon=solo_icon, emboss=False)

    vis_icon = 'HIDE_OFF' if rig.fbp_is_visible else 'HIDE_ON'
    row.prop(rig, "fbp_is_visible", text="", icon=vis_icon, icon_only=True, emboss=False)


def draw_fbp_collection_row(layout, context, collection, depth=0):
    if not collection_has_fbp_content(collection, True):
        return

    hidden = collection_is_hidden_in_view_layer(context, collection)
    collapsed = bool(getattr(collection, 'fbp_collapsed', False))
    row = layout.row(align=True)
    indent_row(row, depth)

    fold_icon = 'TRIA_RIGHT' if collapsed else 'TRIA_DOWN'
    op = row.operator("fbp.toggle_collection_collapse", text="", icon=fold_icon, emboss=False)
    op.collection_name = collection.name

    if hasattr(collection, 'color_tag'):
        row.prop(collection, 'color_tag', text="", icon_only=True)
    else:
        row.label(text="", icon='OUTLINER_COLLECTION')

    name_icon = 'OUTLINER_COLLECTION' if not hidden else 'HIDE_ON'
    op_sel = row.operator("fbp.select_collection_layers", text=collection.name, icon=name_icon, emboss=False)
    op_sel.collection_name = collection.name

    total_layers = sum(1 for _ in iter_fbp_rigs_in_collection(collection, True))
    row.label(text=str(total_layers))

    lock_state = all(getattr(rig, 'hide_select', False) for rig in iter_fbp_rigs_in_collection(collection, True)) if total_layers else False
    op_lock = row.operator("fbp.toggle_collection_lock", text="", icon=('LOCKED' if lock_state else 'UNLOCKED'), emboss=False)
    op_lock.collection_name = collection.name

    op_vis = row.operator("fbp.toggle_collection_visibility", text="", icon=('HIDE_ON' if hidden else 'HIDE_OFF'), emboss=False)
    op_vis.collection_name = collection.name

    op_del = row.operator("fbp.delete_collection_layers", text="", icon='TRASH', emboss=False)
    op_del.collection_name = collection.name

    if collapsed or hidden:
        return

    for child in get_child_fbp_collections(collection):
        draw_fbp_collection_row(layout, context, child, depth + 1)

    for rig in reversed(get_direct_fbp_rigs_in_collection(context, collection)):
        # Se la collection è visibile ma un singolo rig è nascosto, resta comunque in lista.
        draw_fbp_layer_row(layout, context, rig, depth + 1)


def draw_fbp_hierarchical_layer_view(layout, context):
    roots = get_top_fbp_collections(context)
    direct_scene_rigs = get_direct_fbp_rigs_in_collection(context, context.scene.collection)

    if not roots and not direct_scene_rigs:
        layout.label(text="No Frame by Plane layers", icon='INFO')
        return

    col = layout.column(align=True)
    for coll in roots:
        draw_fbp_collection_row(col, context, coll, 0)
    for rig in reversed(direct_scene_rigs):
        draw_fbp_layer_row(col, context, rig, 0)


def iter_material_image_nodes():
    for mat in bpy.data.materials:
        if not mat or not getattr(mat, 'use_nodes', False) or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and getattr(node, 'image', None):
                yield mat, node, node.image


def collect_project_image_paths():
    paths = []
    for _mat, _node, img in iter_material_image_nodes():
        p = getattr(img, 'filepath', '')
        if p:
            paths.append(p)
    return paths


def missing_project_images():
    missing = []
    for p in collect_project_image_paths():
        abs_p = bpy.path.abspath(p)
        if abs_p and not os.path.exists(abs_p):
            missing.append(p)
    return sorted(set(missing), key=natural_sort_key)


def build_project_file_index(root):
    index = {}
    root = bpy.path.abspath(root)
    if not root or not os.path.isdir(root):
        return index
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if not is_supported_image_file(filename) or is_technical_map_file(filename):
                continue
            index.setdefault(filename.lower(), []).append(os.path.join(dirpath, filename))
    return index


def relink_missing_images_from_root(root, make_relative=True):
    file_index = build_project_file_index(root)
    relinked = 0
    ambiguous = []
    still_missing = []
    for _mat, _node, img in iter_material_image_nodes():
        old_path = getattr(img, 'filepath', '')
        if not old_path:
            continue
        abs_old = bpy.path.abspath(old_path)
        if os.path.exists(abs_old):
            if make_relative:
                try:
                    img.filepath = bpy.path.relpath(abs_old)
                except Exception:
                    pass
            continue
        filename = os.path.basename(old_path).lower()
        matches = file_index.get(filename, [])
        if len(matches) == 1:
            new_path = matches[0]
            img.filepath = bpy.path.relpath(new_path) if make_relative else new_path
            relinked += 1
        elif len(matches) > 1:
            ambiguous.append(old_path)
        else:
            still_missing.append(old_path)
    return relinked, ambiguous, still_missing




def project_root_for_package(context):
    sc = context.scene
    root = bpy.path.abspath(getattr(sc, 'fbp_project_path', '') or '')
    if root and os.path.isdir(root):
        return root
    if bpy.data.is_saved:
        return os.path.dirname(bpy.data.filepath)
    return ''


def rig_has_missing_images(rig):
    plane = getattr(rig, 'fbp_plane_target', None)
    if not plane:
        return False
    for mat in plane.data.materials:
        if not mat or not getattr(mat, 'use_nodes', False) or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and getattr(node, 'image', None):
                p = getattr(node.image, 'filepath', '')
                if p and not os.path.exists(bpy.path.abspath(p)):
                    return True
    return False


def swap_layer_depth_only(context, rig_a, rig_b):
    if not rig_a or not rig_b:
        return
    # Scambia solo la profondità, senza ricalcolare tutti i layer.
    axis = 1 if (getattr(rig_a, 'fbp_is_vertical', False) or getattr(rig_b, 'fbp_is_vertical', False)) else 2
    loc_a = rig_a.location.copy()
    loc_b = rig_b.location.copy()
    loc_a[axis], loc_b[axis] = loc_b[axis], loc_a[axis]
    rig_a.location = loc_a
    rig_b.location = loc_b


def object_in_scene(obj, scene=None):
    if not obj:
        return False
    try:
        if bpy.data.objects.get(obj.name) != obj:
            return False
        scene = scene or (bpy.context.scene if bpy.context else None)
        if not scene:
            return True
        return any(scene_obj == obj for scene_obj in scene.objects)
    except ReferenceError:
        return False


def object_in_view_layer(obj, context=None):
    context = context or bpy.context
    if not obj or not context:
        return False
    try:
        if not object_in_scene(obj, context.scene):
            return False
        return any(view_obj == obj for view_obj in context.view_layer.objects)
    except ReferenceError:
        return False


def ensure_object_in_active_collection(obj, context=None):
    context = context or bpy.context
    if not obj or not context:
        return False
    try:
        if object_in_view_layer(obj, context):
            return True
        coll = context.collection or context.scene.collection
        if not any(existing == obj for existing in coll.objects):
            coll.objects.link(obj)
        context.view_layer.update()
        return object_in_view_layer(obj, context)
    except Exception:
        return False


def get_selected_rigs(context):
    return [ob for ob in context.selected_objects if getattr(ob, "is_fbp_control", False)]


def get_selected_fbp_roots(context):
    roots = []
    for ob in context.selected_objects:
        rig = None
        if getattr(ob, "is_fbp_control", False):
            rig = ob
        elif getattr(ob, "is_fbp_plane", False) and getattr(ob.parent, "is_fbp_control", False):
            rig = ob.parent
        if rig and rig not in roots:
            roots.append(rig)
    return roots


def clear_previews():
    for pcoll in preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    preview_collections.clear()


def update_global_visibility(context=None):
    if not context:
        context = bpy.context
    for item in context.scene.fbp_layers:
        try:
            obj = item.obj
            if not obj:
                continue
            vis = obj.fbp_is_visible and not item.mute
            plane = getattr(obj, 'fbp_plane_target', None)
            if not plane:
                continue
            plane.hide_viewport = not vis
            plane.hide_render = not vis
        except ReferenceError:
            pass


def update_mute_cb(self, context):
    update_global_visibility(context)


def get_preview_collection():
    pcoll = preview_collections.get("fbp_previews")
    if not pcoll:
        pcoll = bpy.utils.previews.new()
        preview_collections["fbp_previews"] = pcoll
    return pcoll


def load_preview(image_path):
    pcoll = get_preview_collection()
    abs_path = bpy.path.abspath(image_path)
    if abs_path in pcoll:
        return pcoll[abs_path]
    if os.path.exists(abs_path):
        try:
            return pcoll.load(abs_path, abs_path, 'IMAGE')
        except Exception:
            pass
    return None


def get_layer_thumbnail(obj):
    if not obj or not hasattr(obj, "fbp_preview_path") or not obj.fbp_preview_path:
        return None
    return load_preview(obj.fbp_preview_path)


def safe_get_socket(node, contains, excludes=[]):
    for inp in node.inputs:
        n = inp.name.lower()
        i = inp.identifier.lower()
        if all(c in n or c in i for c in contains) and not any(e in n or e in i for e in excludes):
            return inp
    return None


def set_viewport_object_color(context):
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.color_type = 'TEXTURE'


def get_eval_mat_index(rig, current_frame):
    if not getattr(rig, "fbp_images", None) or len(rig.fbp_images) == 0:
        return -1

    rel_frame = current_frame - rig.fbp_start_frame
    if rel_frame < 0:
        return -1

    N = len(rig.fbp_images)
    total_dur = sum(item.duration for item in rig.fbp_images)
    if total_dur <= 0:
        return 0

    loop_mode = rig.fbp_loop_mode

    if loop_mode == 'NONE':
        if rel_frame >= total_dur:
            return N - 1
    elif loop_mode == 'REPEAT':
        rel_frame = rel_frame % total_dur
    elif loop_mode == 'PINGPONG':
        if N == 1:
            return 0
        mid_dur = sum(item.duration for item in rig.fbp_images[1:-1])
        period = total_dur + mid_dur
        rel_frame = rel_frame % period

        if rel_frame >= total_dur:
            back_rel = rel_frame - total_dur
            accumulated = 0
            for j in range(N - 2, 0, -1):
                dur = rig.fbp_images[j].duration
                if accumulated <= back_rel < accumulated + dur:
                    return j
                accumulated += dur
            return 0

    accumulated = 0
    for i, item in enumerate(rig.fbp_images):
        if accumulated <= rel_frame < accumulated + item.duration:
            return i
        accumulated += item.duration
    return N - 1




def fbp_layer_depth_value(context, rig):
    """Return a stable depth value for sorting layers by distance from the active camera.
    Smaller values are closer to the camera. If no camera exists, use Y for vertical rigs and Z for horizontal rigs.
    """
    if not rig:
        return 0.0
    try:
        cam = context.scene.camera if context and context.scene else None
        if cam:
            cam_forward = cam.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))
            return float((rig.matrix_world.translation - cam.matrix_world.translation).dot(cam_forward))
        return float(rig.location.y if getattr(rig, "fbp_is_vertical", False) else rig.location.z)
    except Exception:
        return 0.0


def sort_rigs_by_depth_for_layer_view(context, rigs):
    if not context or not getattr(context.scene, 'fbp_auto_sort_layers_by_depth', False):
        return rigs
    # Farther layers first internally; the hierarchical UI reverses this so closest appears on top.
    return sorted(rigs, key=lambda rig: (fbp_layer_depth_value(context, rig), natural_sort_key(rig.name)), reverse=True)


def set_timeline_range_from_rigs(context, rigs):
    """Set scene timeline to cover the selected/generated FBP sequences."""
    valid = [rig for rig in rigs if rig and getattr(rig, "is_fbp_control", False)]
    if not valid:
        return False

    starts = []
    ends = []
    for rig in valid:
        total = sum(max(1, item.duration) for item in rig.fbp_images)
        if total <= 0:
            continue
        start = int(rig.fbp_start_frame)
        starts.append(start)
        ends.append(start + total - 1)

    if not starts or not ends:
        return False

    context.scene.frame_start = min(starts)
    context.scene.frame_end = max(ends)
    return True


# ── LAYER UI BOOLEAN HELPERS ─────────────────────────────────────────────────

def _safe_layer_obj(layer_item):
    try:
        obj = layer_item.obj
        if obj and object_in_scene(obj):
            return obj
    except ReferenceError:
        pass
    return None


def get_layer_selected(self):
    obj = _safe_layer_obj(self)
    return bool(obj and obj.select_get())


def set_layer_selected(self, value):
    obj = _safe_layer_obj(self)
    if not obj:
        return
    try:
        context = bpy.context
        if value and not object_in_view_layer(obj, context):
            if not ensure_object_in_active_collection(obj, context):
                sync_layer_collection(context)
                return
        obj.select_set(bool(value))
        if value and context and context.view_layer and object_in_view_layer(obj, context):
            context.view_layer.objects.active = obj
    except Exception:
        pass


def get_layer_rig_locked(self):
    obj = _safe_layer_obj(self)
    return bool(obj.hide_select) if obj else False


def set_layer_rig_locked(self, value):
    obj = _safe_layer_obj(self)
    if obj:
        obj.hide_select = bool(value)


def get_layer_plane_locked(self):
    obj = _safe_layer_obj(self)
    plane = getattr(obj, "fbp_plane_target", None) if obj else None
    return bool(plane.hide_select) if plane else False


def set_layer_plane_locked(self, value):
    obj = _safe_layer_obj(self)
    plane = getattr(obj, "fbp_plane_target", None) if obj else None
    if plane:
        plane.hide_select = bool(value)


def get_layer_solo_view(self):
    return bool(self.solo)


def set_layer_solo_view(self, value):
    context = bpy.context
    sc = context.scene if context else None
    rig = _safe_layer_obj(self)
    value = bool(value)

    if not sc:
        self.solo = value
        return

    if value:
        # First solo click isolates the layer. Further solo clicks add more layers.
        if not any(item.solo for item in sc.fbp_layers):
            for item in sc.fbp_layers:
                item.solo = False
                obj = _safe_layer_obj(item)
                if obj:
                    obj.fbp_is_visible = False

        self.solo = True
        if rig:
            rig.fbp_is_visible = True
    else:
        self.solo = False
        if rig:
            rig.fbp_is_visible = False

        # If no layer remains soloed, restore all layers.
        if not any(item.solo for item in sc.fbp_layers):
            for item in sc.fbp_layers:
                obj = _safe_layer_obj(item)
                if obj:
                    obj.fbp_is_visible = True

    update_global_visibility(context)


# ── PROPERTY GROUPS ───────────────────────────────────────────────────────────

class FBP_LayerItem(PropertyGroup):
    obj:    PointerProperty(type=bpy.types.Object)
    solo:   BoolProperty(default=False)
    mute:   BoolProperty(default=False, update=update_mute_cb)
    folded: BoolProperty(default=False)

    selected: BoolProperty(
        name="Selected",
        description="Select this layer in the viewport. Click-drag across rows to paint selection",
        get=get_layer_selected,
        set=set_layer_selected)
    rig_locked: BoolProperty(
        name="Lock Rig",
        description="Lock/unlock rig selection. Click-drag across rows to paint locks",
        get=get_layer_rig_locked,
        set=set_layer_rig_locked)
    plane_locked: BoolProperty(
        name="Lock Plane",
        description="Lock/unlock plane selection. Click-drag across rows to paint locks",
        get=get_layer_plane_locked,
        set=set_layer_plane_locked)
    solo_view: BoolProperty(
        name="Solo",
        description="Solo this layer. Click-drag across rows to paint solo visibility",
        get=get_layer_solo_view,
        set=set_layer_solo_view)


class FBP_ImageItem(PropertyGroup):
    name:        StringProperty(name="Name", default="Image")
    duration:    IntProperty(name="Duration", default=2, min=1)
    is_selected: BoolProperty(name="Select", default=True)
    is_empty:    BoolProperty(name="Empty", default=False)
    filepath:    StringProperty(name="File", subtype='FILE_PATH', default="")


class FBP_PendingPlaneItem(PropertyGroup):
    name:          StringProperty(name="Name", default="New Layer")
    directory:     StringProperty()
    files_str:     StringProperty()
    fbp_color_tag: EnumProperty(items=COLOR_ENUM_ITEMS, default='COLOR_01')


# ── LAYER / SYNC HELPERS ──────────────────────────────────────────────────────

def sync_layer_collection(context):
    sc = context.scene
    for i in range(len(sc.fbp_layers) - 1, -1, -1):
        try:
            item = sc.fbp_layers[i]
            if not item.obj or not object_in_scene(item.obj, sc):
                sc.fbp_layers.remove(i)
        except ReferenceError:
            sc.fbp_layers.remove(i)

    existing_objs = []
    for item in sc.fbp_layers:
        try:
            if item.obj and object_in_scene(item.obj, sc):
                existing_objs.append(item.obj)
                plane = getattr(item.obj, "fbp_plane_target", None)
                if plane and object_in_scene(plane, sc):
                    plane.is_fbp_plane = True
        except ReferenceError:
            pass

    for obj in sc.objects:
        if is_fbp_layer_object(obj) and obj not in existing_objs:
            item = sc.fbp_layers.add()
            item.obj = obj
            plane = getattr(obj, "fbp_plane_target", None)
            if plane and object_in_scene(plane, sc):
                plane.is_fbp_plane = True
            sc.fbp_layers.move(len(sc.fbp_layers) - 1, 0)


def delete_fbp_rigs(context, rigs):
    unique_layers = []
    for rig in rigs:
        if rig and is_fbp_layer_object(rig) and rig not in unique_layers:
            unique_layers.append(rig)

    if not unique_layers:
        return 0

    deleted = 0
    for rig in unique_layers:
        try:

            plane = getattr(rig, "fbp_plane_target", None)
            if plane and bpy.data.objects.get(plane.name) == plane:
                mats_to_remove = [mat for mat in plane.data.materials if mat]
                bpy.data.objects.remove(plane, do_unlink=True)
                for mat in mats_to_remove:
                    if mat and mat.users == 0:
                        bpy.data.materials.remove(mat)
            if bpy.data.objects.get(rig.name) == rig:
                bpy.data.objects.remove(rig, do_unlink=True)
                deleted += 1
        except ReferenceError:
            pass

    for img in list(bpy.data.images):
        if img.users == 0 and not getattr(img, "use_fake_user", False):
            bpy.data.images.remove(img)

    if context:
        sync_layer_collection(context)
    return deleted


def cleanup_orphan_fbp_planes(context):
    if not context:
        return 0
    removed = 0
    for obj in list(context.scene.objects):
        try:
            if not getattr(obj, "is_fbp_plane", False):
                continue
            parent = obj.parent
            if parent and getattr(parent, "is_fbp_control", False) and object_in_scene(parent, context.scene):
                continue
            mats_to_remove = [mat for mat in obj.data.materials if mat] if getattr(obj, "data", None) else []
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
            for mat in mats_to_remove:
                if mat and mat.users == 0:
                    bpy.data.materials.remove(mat)
        except ReferenceError:
            pass
    return removed


def apply_layer_depth(context):
    sc = context.scene
    offset = sc.fbp_layer_offset
    valid_objs = [item.obj for item in sc.fbp_layers if item.obj]
    if not valid_objs:
        return

    is_vert = getattr(valid_objs[0], "fbp_is_vertical", False)
    base_depth = (min(obj.location.y for obj in valid_objs) if is_vert
                  else max(obj.location.z for obj in valid_objs))

    for i, layer_idx in enumerate(range(len(sc.fbp_layers) - 1, -1, -1)):
        obj = sc.fbp_layers[layer_idx].obj
        if not obj:
            continue
        if getattr(obj, "fbp_is_vertical", False):
            obj.location.y = base_depth + (i * offset)
        else:
            obj.location.z = base_depth - (i * offset)
        if sc.fbp_auto_scale and sc.camera and not fbp_fast_import_is_active():
            context.view_layer.update()
            context.evaluated_depsgraph_get().update()
            apply_fit_to_camera(context, obj, sc.camera)


def sync_fbp_property(self, context, prop_name):
    if getattr(context, "active_object", None) != self:
        return
    val = getattr(self, prop_name)
    for obj in context.selected_objects:
        if obj != self and getattr(obj, "is_fbp_control", False):
            if getattr(obj, prop_name) != val:
                setattr(obj, prop_name, val)


# ── CORE OPERATIONS ───────────────────────────────────────────────────────────

def do_update_animation(rig):
    plane = rig.fbp_plane_target
    if not plane or not plane.data.materials:
        return
    if plane.parent != rig:
        return
    if plane.data.animation_data:
        plane.data.animation_data_clear()
    if plane.data.polygons and rig.fbp_images:
        try:
            idx = get_eval_mat_index(rig, bpy.context.scene.frame_current)
            if idx < 0:
                idx = 0
            if idx < len(plane.data.materials):
                plane.data.polygons[0].material_index = idx
        except Exception as e:
            print(f"[FBP] Animation update error: {e}")


def do_update_emission(rig):
    plane = rig.fbp_plane_target
    if not plane:
        return
    
    plane.visible_shadow = not rig.fbp_use_emission

    for mat in plane.data.materials:
        if mat and mat.use_nodes:
            if hasattr(mat, "shadow_method"):
                mat.shadow_method = 'NONE' if rig.fbp_use_emission else 'OPAQUE'
            
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    em_str = safe_get_socket(node, ['emission', 'strength'])
                    if em_str:
                        em_str.default_value = 1.0 if rig.fbp_use_emission else 0.0

                    base_col_weight = safe_get_socket(node, ['base', 'weight'])
                    if base_col_weight:
                        base_col_weight.default_value = 0.0 if rig.fbp_use_emission else 1.0
                    
                    spec = safe_get_socket(node, ['specular'])
                    if spec:
                        spec.default_value = 0.0 if rig.fbp_use_emission else 0.5
                        
                    spec_ior_level = safe_get_socket(node, ['specular', 'ior', 'level'])
                    if spec_ior_level:
                        spec_ior_level.default_value = 0.0 if rig.fbp_use_emission else 0.5


def set_fbp_material_transparency(mat, opacity=1.0):
    if not mat:
        return
    try:
        mat.diffuse_color = (mat.diffuse_color[0], mat.diffuse_color[1], mat.diffuse_color[2], opacity)
    except Exception:
        pass
    for attr, value in (
        ('surface_render_method', 'BLENDED'),
        ('blend_method', 'BLEND'),
        ('show_transparent_back', True),
        ('use_screen_refraction', True),
    ):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except Exception:
                pass


def is_fbp_empty_material(mat):
    try:
        return bool(mat and mat.get("fbp_empty_frame", False))
    except Exception:
        return False


def create_fbp_empty_material(mat_name="FBP_Empty_Frame"):
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
    mat["fbp_empty_frame"] = True
    mat.use_nodes = True
    set_fbp_material_transparency(mat, 0.0)

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (250, 0)
    transparent = nodes.new(type='ShaderNodeBsdfTransparent')
    transparent.location = (0, 0)
    links.new(transparent.outputs[0], out.inputs[0])

    try:
        mat.diffuse_color = (0.0, 0.0, 0.0, 0.0)
    except Exception:
        pass
    return mat


def do_update_opacity(rig):
    plane = rig.fbp_plane_target
    if not plane:
        return
    opacity = max(0.0, min(1.0, float(getattr(rig, 'fbp_opacity', 1.0))))
    try:
        plane.show_transparent = opacity < 1.0
    except Exception:
        pass
    for mat in plane.data.materials:
        if is_fbp_empty_material(mat):
            set_fbp_material_transparency(mat, 0.0)
            continue
        set_fbp_material_transparency(mat, opacity)
        if mat and mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.name == "FBP_Opacity" and len(node.inputs) > 1:
                    node.inputs[1].default_value = opacity


def do_update_track(rig, context):
    cam = context.scene.camera
    if rig.fbp_track_cam and cam:
        cons = rig.constraints.get("FBP_Track")
        if not cons:
            cons = rig.constraints.new(type='DAMPED_TRACK')
            cons.name = "FBP_Track"
        cons.target = cam
        cons.track_axis = 'TRACK_Z'
    else:
        cons = rig.constraints.get("FBP_Track")
        if cons:
            rig.constraints.remove(cons)


# ── CAMERA DEPTH GETTER/SETTER ────────────────────────────────────────────────

def get_fbp_cam_depth(self):
    cam = bpy.context.scene.camera
    if not cam:
        return self.location.y if getattr(self, "fbp_is_vertical", False) else self.location.z
    cam_z = cam.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))
    return (self.location - cam.location).dot(cam_z)


def set_fbp_cam_depth(self, value):
    context = bpy.context
    sc = context.scene
    cam = sc.camera

    valid_objs = [item.obj for item in sc.fbp_layers
                  if item.obj and getattr(item.obj, "is_fbp_control", False)]

    if self in valid_objs and len(valid_objs) > 1:
        my_idx = valid_objs.index(self)
        depths = []
        for obj in valid_objs:
            if not cam:
                d = obj.location.y if getattr(obj, "fbp_is_vertical", False) else obj.location.z
            else:
                cam_z = cam.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))
                d = (obj.location - cam.location).dot(cam_z)
            depths.append(d)

        my_depth = depths[my_idx]
        if my_idx > 0:
            d_prev = depths[my_idx - 1]
            value = (max(value, d_prev + 0.001) if my_depth >= d_prev
                     else min(value, d_prev - 0.001))
        if my_idx < len(valid_objs) - 1:
            d_next = depths[my_idx + 1]
            value = (max(value, d_next + 0.001) if my_depth >= d_next
                     else min(value, d_next - 0.001))

    if not cam:
        if getattr(self, "fbp_is_vertical", False):
            self.location.y = value
        else:
            self.location.z = value
        return

    cam_z = cam.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))
    vec = self.location - cam.location
    current_depth = vec.dot(cam_z)
    if abs(current_depth) < 0.001:
        return

    scale_factor = value / current_depth
    self.location = cam.location + vec * scale_factor
    self.scale = (
        self.scale.x * abs(scale_factor),
        self.scale.y * abs(scale_factor),
        self.scale.z * abs(scale_factor),
    )


# ── UPDATE CALLBACKS ──────────────────────────────────────────────────────────

def update_loop_mode_cb(self, context):
    sync_fbp_property(self, context, "fbp_loop_mode")
    do_update_animation(self)

def update_start_frame_cb(self, context):
    sync_fbp_property(self, context, "fbp_start_frame")
    do_update_animation(self)

def update_emission_cb(self, context):
    sync_fbp_property(self, context, "fbp_use_emission")
    do_update_emission(self)

def update_opacity_cb(self, context):
    sync_fbp_property(self, context, "fbp_opacity")
    do_update_opacity(self)

def update_track_cb(self, context):
    sync_fbp_property(self, context, "fbp_track_cam")
    do_update_track(self, context)

def update_global_duration_cb(self, context):
    sync_fbp_property(self, context, "fbp_global_duration")

def update_visibility_cb(self, context):
    sync_fbp_property(self, context, "fbp_is_visible")
    update_global_visibility(context)

def update_color_tag_cb(self, context):
    sync_fbp_property(self, context, "fbp_color_tag")
    if is_fbp_layer_object(self):
        apply_collection_color_to_layer(
            self,
            self.fbp_color_tag,
            getattr(self, "fbp_color_variant_index", 0),
            push_collection=getattr(self, "fbp_follow_collection_color", True)
        )

def update_image_index_cb(self, context):
    # Do not move the timeline when selecting an image row.
    # The visible frame is evaluated from the current timeline position.
    if not getattr(self, "is_fbp_control", False):
        return
    do_update_animation(self)

def update_layer_stack_index_cb(self, context):
    try:
        idx = self.fbp_layer_stack_index
        if 0 <= idx < len(self.fbp_layers):
            obj = self.fbp_layers[idx].obj
            if obj and is_fbp_layer_object(obj):
                if context.view_layer.objects.active != obj:
                    # Keep previous selections alive so the layer list can support multi-select painting.
                    obj.select_set(True)
                    context.view_layer.objects.active = obj
    except Exception as e:
        print(f"[FBP] Stack index error: {e}")

def update_cam_ratio_cb(self, context):
    ratio = self.fbp_cam_ratio
    rd = context.scene.render
    if   ratio == '16_9': rd.resolution_x, rd.resolution_y = 1920, 1080
    elif ratio == '9_16': rd.resolution_x, rd.resolution_y = 1080, 1920
    elif ratio == '4_3':  rd.resolution_x, rd.resolution_y = 1920, 1440
    elif ratio == '1_1':  rd.resolution_x, rd.resolution_y = 2000, 2000




# ── RENDER STABILITY HELPERS ─────────────────────────────────────────────────

def fbp_is_rendering_now():
    """Best-effort render guard. Avoid UI/depsgraph side effects while Blender renders."""
    global _fbp_render_guard_active
    if _fbp_render_guard_active:
        return True
    try:
        return bool(bpy.app.is_job_running('RENDER'))
    except Exception:
        return False


def fbp_safe_empty_material():
    """Opaque-ish zero alpha material used to fill invalid material slots safely."""
    mat = bpy.data.materials.get("FBP_SAFE_EMPTY_RENDER_MAT")
    if not mat:
        mat = bpy.data.materials.new("FBP_SAFE_EMPTY_RENDER_MAT")
    mat["fbp_empty_frame"] = True
    mat.use_nodes = True
    try:
        mat.diffuse_color = (0.0, 0.0, 0.0, 0.0)
    except Exception:
        pass
    for attr, value in (
        ('surface_render_method', 'BLENDED'),
        ('blend_method', 'BLEND'),
        ('show_transparent_back', True),
    ):
        if hasattr(mat, attr):
            try:
                setattr(mat, attr, value)
            except Exception:
                pass
    try:
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()
        out = nodes.new(type='ShaderNodeOutputMaterial')
        transparent = nodes.new(type='ShaderNodeBsdfTransparent')
        links.new(transparent.outputs[0], out.inputs[0])
    except Exception:
        pass
    return mat


def fbp_ensure_plane_render_safe(rig, frame=None):
    """Make sure a FBP plane has valid materials, UVs and polygon material indices."""
    if not rig or not getattr(rig, "is_fbp_control", False):
        return False
    plane = getattr(rig, "fbp_plane_target", None)
    if not plane or not getattr(plane, "data", None):
        return False
    mesh = plane.data
    safe_mat = fbp_safe_empty_material()

    try:
        target_count = max(len(getattr(rig, "fbp_images", [])), 1)
    except Exception:
        target_count = max(len(mesh.materials), 1)

    try:
        while len(mesh.materials) < target_count:
            mesh.materials.append(safe_mat)
        for i in range(len(mesh.materials)):
            if mesh.materials[i] is None:
                mesh.materials[i] = safe_mat
    except Exception:
        pass

    try:
        if not mesh.uv_layers:
            mesh.uv_layers.new(name="UVMap")
    except Exception:
        pass

    idx = 0
    try:
        if frame is None:
            frame = bpy.context.scene.frame_current
        idx = get_eval_mat_index(rig, frame)
        if idx < 0:
            idx = 0
        if len(mesh.materials) > 0:
            idx = max(0, min(idx, len(mesh.materials) - 1))
        else:
            idx = 0
    except Exception:
        idx = 0

    try:
        for poly in mesh.polygons:
            if len(mesh.materials) > 0:
                poly.material_index = idx
            else:
                poly.material_index = 0
        mesh.update()
    except Exception:
        pass

    try:
        plane.hide_render = not bool(getattr(rig, "fbp_is_visible", True))
    except Exception:
        pass

    return True


def fbp_repair_all_render_state(scene=None, frame=None):
    scene = scene or bpy.context.scene
    fixed = 0
    for obj in list(scene.objects):
        try:
            if getattr(obj, "is_fbp_control", False):
                if fbp_ensure_plane_render_safe(obj, frame):
                    fixed += 1
                try:
                    obj.hide_render = True
                except Exception:
                    pass
        except ReferenceError:
            pass
    return fixed


@bpy.app.handlers.persistent
def fbp_render_guard_pre(scene):
    global _fbp_render_guard_active
    _fbp_render_guard_active = True
    try:
        fbp_repair_all_render_state(scene, scene.frame_current)
    except Exception as e:
        print(f"[FBP] Render guard pre error: {e}")


@bpy.app.handlers.persistent
def fbp_render_guard_post(scene):
    global _fbp_render_guard_active
    _fbp_render_guard_active = False


def fbp_remove_live_handlers_for_background():
    """Used by background render scripts to avoid UI/depsgraph side effects."""
    handler_names = {
        'fbp_frame_change_handler',
        'fbp_depsgraph_handler',
        'fbp_render_guard_pre',
        'fbp_render_guard_post',
    }
    for handler_list in (
        bpy.app.handlers.frame_change_pre,
        bpy.app.handlers.frame_change_post,
        bpy.app.handlers.depsgraph_update_post,
        bpy.app.handlers.render_pre,
        bpy.app.handlers.render_post,
        bpy.app.handlers.render_cancel,
        bpy.app.handlers.render_complete,
    ):
        for handler in list(handler_list):
            if getattr(handler, "__name__", "") in handler_names:
                try:
                    handler_list.remove(handler)
                except Exception:
                    pass


# ── HANDLERS ─────────────────────────────────────────────────────────────────

def sync_layer_collection_timer():
    if bpy.context:
        sync_layer_collection(bpy.context)
    return None


def cleanup_orphan_fbp_planes_timer():
    if bpy.context:
        cleanup_orphan_fbp_planes(bpy.context)
        sync_layer_collection(bpy.context)
    return None


@bpy.app.handlers.persistent
def fbp_depsgraph_handler(scene, depsgraph):
    if fbp_is_rendering_now():
        return
    global _last_fbp_update
    now = time.time()
    if now - _last_fbp_update < 0.25:
        return
    _last_fbp_update = now

    if not bpy.context:
        return

    fbp_objs = [obj for obj in scene.objects if getattr(obj, "is_fbp_control", False)]
    needs_sync = len(scene.fbp_layers) != len(fbp_objs)

    if not needs_sync:
        for item in scene.fbp_layers:
            try:
                if not item.obj or not object_in_scene(item.obj, scene):
                    needs_sync = True
                    break
            except ReferenceError:
                needs_sync = True
                break

    if needs_sync and not bpy.app.timers.is_registered(sync_layer_collection_timer):
        bpy.app.timers.register(sync_layer_collection_timer, first_interval=0.05)

    orphan_planes = []
    for obj in scene.objects:
        try:
            if getattr(obj, "is_fbp_plane", False):
                parent = obj.parent
                if not parent or not getattr(parent, "is_fbp_control", False) or not object_in_scene(parent, scene):
                    orphan_planes.append(obj)
        except ReferenceError:
            pass

    if orphan_planes and not bpy.app.timers.is_registered(cleanup_orphan_fbp_planes_timer):
        bpy.app.timers.register(cleanup_orphan_fbp_planes_timer, first_interval=0.05)

    try:
        sync_collection_colors_to_rigs(bpy.context)
    except Exception:
        pass

    try:
        obj = bpy.context.active_object
        if obj and getattr(obj, "is_fbp_control", False):
            for i, item in enumerate(scene.fbp_layers):
                try:
                    if item.obj == obj and scene.fbp_layer_stack_index != i:
                        scene.fbp_layer_stack_index = i
                        break
                except ReferenceError:
                    pass
    except Exception as e:
        print(f"[FBP] Depsgraph sync error: {e}")


@bpy.app.handlers.persistent
def fbp_frame_change_handler(scene):
    # Render-safe update: only change the evaluated material index.
    # Do NOT force viewport redraw here; in Blender 5.1 this can crash Workbench
    # while Eevee/animation render is changing frames.
    frame = scene.frame_current
    for item in scene.fbp_layers:
        try:
            obj = item.obj
            if not obj or not getattr(obj, "is_fbp_control", False):
                continue
            plane = obj.fbp_plane_target
            if not plane or not plane.data or not plane.data.polygons:
                continue
            fbp_ensure_plane_render_safe(obj, frame)
        except ReferenceError:
            pass
        except Exception as e:
            print(f"[FBP] Frame update skipped: {e}")

# ── PROPERTY REGISTRATION ─────────────────────────────────────────────────────

def register_properties():
    bpy.types.Scene.fbp_last_directory = StringProperty(subtype='DIR_PATH', default="")
    bpy.types.Scene.fbp_project_path = StringProperty(
        name="Project Folder", subtype='DIR_PATH', default="")
    bpy.types.Scene.fbp_parent_import_path = StringProperty(
        name="Project Folder", subtype='DIR_PATH')
    bpy.types.Scene.fbp_import_main_folders_as_scenes = BoolProperty(
        name="Main Folders as Scenes",
        description="Create one Blender Scene for each top-level project folder. Folders starting with _ are ignored",
        default=False)
    bpy.types.Scene.fbp_cam_ratio = EnumProperty(
        name="Camera Ratio",
        items=[
            ('16_9',   "16:9",   "Horizontal (1920x1080)"),
            ('9_16',   "9:16",   "Vertical (1080x1920)"),
            ('4_3',    "4:3",    "Classic TV (1920x1440)"),
            ('1_1',    "1:1",    "Square (2000x2000)"),
            ('CUSTOM', "Custom", "Free resolution"),
        ],
        default='4_3', update=update_cam_ratio_cb)
    bpy.types.Scene.fbp_show_previews = BoolProperty(name="Show Thumbnails", default=False)
    bpy.types.Scene.fbp_use_hierarchical_layers = BoolProperty(name="Hierarchical Layer View", default=True)
    bpy.types.Scene.fbp_auto_sort_layers_by_depth = BoolProperty(
        name="Auto Depth Sort",
        description="Sort the Layer View by camera distance. Falls back to Y for vertical layers and Z for horizontal layers",
        default=True)
    bpy.types.Scene.fbp_show_create_tools = BoolProperty(name="Show Create Tools", default=False)
    bpy.types.Scene.fbp_emergency_render_start = IntProperty(name="Start", default=0, min=0)
    bpy.types.Scene.fbp_emergency_render_end = IntProperty(name="End", default=0, min=0)
    bpy.types.Scene.fbp_emergency_render_prefix = StringProperty(name="Prefix", default="frame_")
    bpy.types.Scene.fbp_auto_collection_color_variants = BoolProperty(name="Collection Color Variants", description="Give layers small viewport color variations based on their collection color", default=True)
    bpy.types.Scene.fbp_layers = CollectionProperty(type=FBP_LayerItem)
    bpy.types.Scene.fbp_layer_stack_index = IntProperty(
        name="Layer Index", default=0, update=update_layer_stack_index_cb)
    bpy.types.Scene.fbp_creation_mode = EnumProperty(
        name="Mode",
        items=[
            ('SINGLE', "Single Plane",      "Single independent plane", 'IMAGE_DATA',   0),
            ('MULTI',  "MultiPlane Camera", "Parallax plane hierarchy", 'RENDERLAYERS', 1),
        ],
        default='SINGLE')
    bpy.types.Scene.fbp_pending_planes = CollectionProperty(type=FBP_PendingPlaneItem)
    bpy.types.Scene.fbp_pending_planes_idx = IntProperty(default=0)
    bpy.types.Scene.fbp_pre_duration = IntProperty(
        name="Duration (Frames)", default=2, min=1)
    bpy.types.Scene.fbp_pre_shadeless = BoolProperty(name="Shadeless", default=True)
    bpy.types.Scene.fbp_pre_loop_mode = EnumProperty(
        name="Playback",
        items=[
            ('NONE',     "One Shot",  "Play once",      'FORWARD',        0),
            ('REPEAT',   "Loop",      "Repeat forever", 'FILE_REFRESH',   1),
            ('PINGPONG', "Ping-Pong", "Back and forth", 'UV_SYNC_SELECT', 2),
        ],
        default='NONE')
    bpy.types.Scene.fbp_pre_interpolation = EnumProperty(
        name="",
        items=[
            ('Closest', "Pixel",  "Sharp edges (pixel art)", 'ALIASED',     0),
            ('Linear',  "Smooth", "Bilinear filter",         'ANTIALIASED', 1),
        ],
        default='Closest')
    bpy.types.Scene.fbp_pre_orientation = EnumProperty(
        name="",
        items=[
            ('HORIZ', "Horizontal", "Planes on the floor", 'AXIS_TOP',   0),
            ('VERT',  "Vertical",   "Standing planes",     'AXIS_FRONT', 1),
        ],
        default='VERT')
    bpy.types.Scene.fbp_gen_camera   = BoolProperty(name="Generate Camera",            default=True)
    bpy.types.Scene.fbp_cam_pivot    = BoolProperty(name="Pivot on Camera",            default=True)
    bpy.types.Scene.fbp_layer_offset = FloatProperty(name="Plane Distance (m)",        default=0.2, min=0.001)
    bpy.types.Scene.fbp_auto_scale   = BoolProperty(name="Auto-Scale (Fit to Cam)",   default=True)

    bpy.types.Collection.is_fbp_collection = BoolProperty(default=False)
    bpy.types.Collection.fbp_collapsed = BoolProperty(name="Collapsed", default=False)

    bpy.types.Object.is_fbp_control     = BoolProperty(default=False)
    bpy.types.Object.is_fbp_plane       = BoolProperty(default=False)
    bpy.types.Object.fbp_collection_name = StringProperty(default="")
    bpy.types.Object.fbp_follow_collection_color = BoolProperty(name="Follow Collection Color", default=True)
    bpy.types.Object.fbp_color_variant_index = IntProperty(default=0)
    bpy.types.Object.fbp_base_scale     = FloatProperty(default=1.0)
    bpy.types.Object.fbp_base_scale_vec = FloatVectorProperty(default=(1.0, 1.0, 1.0))
    bpy.types.Object.fbp_preview_path   = StringProperty(default="")
    bpy.types.Object.fbp_is_vertical    = BoolProperty(default=False)
    bpy.types.Object.fbp_images         = CollectionProperty(type=FBP_ImageItem)
    bpy.types.Object.fbp_images_index   = IntProperty(update=update_image_index_cb)
    bpy.types.Object.fbp_color_tag      = EnumProperty(
        items=COLOR_ENUM_ITEMS, default='COLOR_01', update=update_color_tag_cb)
    bpy.types.Object.fbp_depth_order    = IntProperty(default=0)
    bpy.types.Object.fbp_cam_depth      = FloatProperty(
        name="Depth",
        description="Visual depth (clamped by adjacent layers to avoid overlapping)",
        get=get_fbp_cam_depth,
        set=set_fbp_cam_depth,
        step=5)
    bpy.types.Object.fbp_loop_mode = EnumProperty(
        name="Playback",
        items=[
            ('NONE',     "One Shot",  "", 'FORWARD',        0),
            ('REPEAT',   "Loop",      "", 'FILE_REFRESH',   1),
            ('PINGPONG', "Ping-Pong", "", 'UV_SYNC_SELECT', 2),
        ],
        default='NONE', update=update_loop_mode_cb)
    bpy.types.Object.fbp_use_emission   = BoolProperty(
        name="Shadeless", default=False, update=update_emission_cb)
    bpy.types.Object.fbp_interpolation  = EnumProperty(
        name="Filter",
        items=[
            ('Closest', "Pixel",  "", 'SNAP_GRID', 0),
            ('Linear',  "Smooth", "", 'IMAGE_RGB', 1),
        ],
        default='Closest')
    bpy.types.Object.fbp_plane_target    = PointerProperty(type=bpy.types.Object)
    bpy.types.Object.fbp_global_duration = IntProperty(
        name="Global Duration", default=2, min=1, update=update_global_duration_cb)
    bpy.types.Object.fbp_start_frame     = IntProperty(
        name="Start Frame", default=1, update=update_start_frame_cb)
    bpy.types.Object.fbp_opacity         = FloatProperty(
        name="Opacity", default=1.0, min=0.0, max=1.0,
        subtype='FACTOR', update=update_opacity_cb)
    bpy.types.Object.fbp_track_cam       = BoolProperty(
        name="Track Camera", default=False, update=update_track_cb)
    bpy.types.Object.fbp_is_visible      = BoolProperty(
        name="Visible", default=True, update=update_visibility_cb)


# ── MATERIAL CREATION ─────────────────────────────────────────────────────────

def create_fbp_material(mat_name, image_path, interp='Closest', opacity=1.0):
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
    
    mat.use_nodes = True
    set_fbp_material_transparency(mat, opacity)

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    out  = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (500, 0)
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (150, 0)
    tex  = nodes.new(type='ShaderNodeTexImage')
    tex.location = (-300, 0)
    tex.interpolation = interp

    math_node = nodes.new(type='ShaderNodeMath')
    math_node.operation = 'MULTIPLY'
    math_node.location = (-50, -200)
    math_node.name = "FBP_Opacity"
    math_node.inputs[1].default_value = opacity

    try:
        img = bpy.data.images.load(image_path, check_existing=True)
        tex.image = img
        img.alpha_mode = 'STRAIGHT'
    except Exception as e:
        print(f"[FBP] Image load error: {e}")

    base_color = safe_get_socket(bsdf, ['base', 'color']) or bsdf.inputs[0]
    links.new(tex.outputs['Color'], base_color)

    em_color = safe_get_socket(bsdf, ['emission'], excludes=['strength'])
    if em_color:
        links.new(tex.outputs['Color'], em_color)

    alpha_sock = safe_get_socket(bsdf, ['alpha'])
    if alpha_sock:
        links.new(tex.outputs['Alpha'],       math_node.inputs[0])
        links.new(math_node.outputs['Value'], alpha_sock)

    links.new(bsdf.outputs[0], out.inputs[0])
    return mat


# ── FIT TO CAMERA ─────────────────────────────────────────────────────────────

def apply_fit_to_camera(context, rig, cam):
    if not rig or not cam:
        return
    cam_z = cam.matrix_world.to_3x3() @ mathutils.Vector((0.0, 0.0, -1.0))
    vec   = rig.matrix_world.translation - cam.matrix_world.translation
    dist  = abs(vec.dot(cam_z))
    if dist < 0.001:
        return

    frame        = cam.data.view_frame(scene=context.scene)
    frame_width  = abs(frame[0].x - frame[3].x) * dist
    frame_height = abs(frame[0].y - frame[1].y) * dist

    base_x = max(rig.fbp_base_scale_vec[0], 0.0001)
    base_y = max(rig.fbp_base_scale_vec[1], 0.0001)
    base_z = max(rig.fbp_base_scale_vec[2], 0.0001)

    img_width  = 3 * base_x
    img_height = 3 * base_y
    if img_width == 0 or img_height == 0:
        return

    factor = min(frame_width / img_width, frame_height / img_height)
    rig.scale = (base_x * factor, base_y * factor, base_z * factor)


# ── RIG BUILDER ───────────────────────────────────────────────────────────────

def build_fbp_rig(context, rig_name, directory, files_list, location, color_tag='COLOR_01', target_collection=None, color_variant_index=0):
    sc = context.scene

    bpy.ops.mesh.primitive_plane_add(size=2.1, location=location)
    rig = context.active_object
    rig.name = rig_name
    rig.display_type = 'WIRE'
    rig.is_fbp_control = True
    rig.fbp_global_duration = sc.fbp_pre_duration
    rig.fbp_use_emission = sc.fbp_pre_shadeless
    rig.fbp_loop_mode = sc.fbp_pre_loop_mode
    rig.fbp_interpolation = sc.fbp_pre_interpolation
    rig.fbp_color_tag = color_tag
    rig.fbp_color_variant_index = color_variant_index
    if target_collection:
        rig.fbp_collection_name = target_collection.name
        set_collection_color_tag(target_collection, color_tag)
    apply_collection_color_to_rig(rig, color_tag, color_variant_index, push_collection=False)
    rig.hide_render = True

    if sc.fbp_pre_orientation == 'VERT':
        rig.rotation_euler[0] = math.radians(90)
        rig.fbp_is_vertical = True

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.delete(type='ONLY_FACE')
    bpy.ops.object.mode_set(mode='OBJECT')

    bpy.ops.mesh.primitive_plane_add(size=2, location=location)
    plane = context.active_object
    plane.name = "Plane_" + rig_name
    plane.is_fbp_plane = True
    plane.parent = rig
    plane.location = (0, 0, 0)
    plane.rotation_euler = (0, 0, 0)
    plane.hide_select = True
    rig.fbp_plane_target = plane
    if target_collection:
        plane.fbp_collection_name = target_collection.name
        move_object_to_collection(rig, target_collection)
        move_object_to_collection(plane, target_collection)

    first_img = None
    for f in files_list:
        img_path = os.path.join(directory, f)
        mat = create_fbp_material(
            f"Mat_{f}", img_path,
            interp=rig.fbp_interpolation,
            opacity=rig.fbp_opacity)
        plane.data.materials.append(mat)
        item = rig.fbp_images.add()
        item.name = f
        item.duration = rig.fbp_global_duration
        item.is_selected = True
        item.is_empty = False
        item.filepath = img_path
        if not first_img:
            try:
                first_img = bpy.data.images.load(img_path, check_existing=True)
            except Exception:
                pass

    if first_img:
        width, height = first_img.size
        if width > 0 and height > 0:
            if width > height:
                rig.scale = (1, height / width, 1)
            else:
                rig.scale = (width / height, 1, 1)
            rig.fbp_base_scale = rig.scale.x
            rig.fbp_base_scale_vec = rig.scale
            rig.fbp_preview_path = first_img.filepath

    apply_collection_color_to_rig(rig, color_tag, color_variant_index, push_collection=False)
    if fbp_fast_import_is_active():
        fbp_fast_import_queue_rig(rig)
        do_update_emission(rig)
    else:
        context.view_layer.objects.active = rig
        do_update_animation(rig)
        do_update_emission(rig)
        sync_layer_collection(context)
    return rig


# ── UI LISTS ──────────────────────────────────────────────────────────────────

class FBP_UL_LayerStack(UIList):
    def filter_items(self, context, data, propname):
        objs = getattr(data, propname)
        flt_flags = []
        flt_neworder = list(range(len(objs)))
        if getattr(context.scene, 'fbp_auto_sort_layers_by_depth', False):
            flt_neworder.sort(key=lambda i: (fbp_layer_depth_value(context, getattr(objs[i], 'obj', None)), natural_sort_key(getattr(getattr(objs[i], 'obj', None), 'name', ''))))
        else:
            flt_neworder.reverse()
        for item in objs:
            if is_layer_item_visible_in_collections(context, item):
                flt_flags.append(self.bitflag_filter_item)
            else:
                flt_flags.append(0)
        return flt_flags, flt_neworder

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        try:
            rig = item.obj
            if not rig or not is_fbp_layer_object(rig):
                layout.label(text="<Deleted Layer>")
                return

            row = layout.row(align=True)

            sel_icon = 'CHECKBOX_HLT' if item.selected else 'CHECKBOX_DEHLT'
            row.prop(item, "selected", text="", icon=sel_icon, emboss=False)

            if context.scene.fbp_show_previews:
                preview = get_layer_thumbnail(rig)
                if preview:
                    row.template_icon(icon_value=preview.icon_id, scale=1.0)
                else:
                    row.label(text="", icon='STRIP_' + rig.fbp_color_tag)
            else:
                row.label(text="", icon='STRIP_' + rig.fbp_color_tag)

            op_name = row.operator("fbp.select_layer_exclusive", text=rig.name, emboss=False)
            op_name.rig_name = rig.name

            sub = row.row(align=True)
            sub.prop(rig, "fbp_cam_depth", text="")

            row.separator()
            row.label(text=f"F.{len(rig.fbp_images)}")

            lock_icon = 'LOCKED' if item.rig_locked else 'UNLOCKED'
            row.prop(item, "rig_locked", text="", icon=lock_icon, emboss=False)

            plane = rig.fbp_plane_target
            if plane:
                plane_icon = 'RESTRICT_SELECT_ON' if item.plane_locked else 'RESTRICT_SELECT_OFF'
                row.prop(item, "plane_locked", text="", icon=plane_icon, emboss=False)
            else:
                row.label(text="", icon='BLANK1')

            solo_icon = 'OUTLINER_OB_LIGHT' if item.solo_view else 'LIGHT'
            row.prop(item, "solo_view", text="", icon=solo_icon, emboss=False)

            vis_icon = 'HIDE_OFF' if rig.fbp_is_visible else 'HIDE_ON'
            row.prop(rig, "fbp_is_visible", text="", icon=vis_icon, icon_only=True, emboss=False)

        except ReferenceError:
            layout.label(text="<Deleted Layer>")


class FBP_UL_ImageList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        rig = data
        plane = getattr(rig, "fbp_plane_target", None)
        custom_icon = 'IMAGE_DATA'
        is_missing = False

        is_empty = bool(getattr(item, "is_empty", False))
        if is_empty:
            custom_icon = 'FILE_FOLDER'

        if plane and index < len(plane.data.materials) and not is_empty:
            try:
                mat = plane.data.materials[index]
                if is_fbp_empty_material(mat):
                    custom_icon = 'FILE_FOLDER'
                    is_empty = True
                elif mat and mat.use_nodes:
                    for node in mat.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image:
                            img_path = node.image.filepath
                            if img_path and not os.path.exists(bpy.path.abspath(img_path)):
                                is_missing = True
                            if context.scene.fbp_show_previews:
                                thumb = load_preview(node.image.filepath)
                                if thumb:
                                    custom_icon = thumb.icon_id
                            break
            except Exception:
                pass

        eval_idx = get_eval_mat_index(rig, context.scene.frame_current)
        row = layout.row(align=True)
        split = row.split(factor=0.70)
        left = split.row(align=True)

        if index == eval_idx:
            row.alert = True
            left.label(text="", icon='RECORD_ON')
        else:
            left.label(text="", icon='DOT')

        if is_missing:
            left.label(text="", icon='ERROR')

        display_name = item.name if not is_empty else "Empty Frame"
        if isinstance(custom_icon, int):
            left.label(text=f"{index + 1} - ({display_name})", icon_value=custom_icon)
        else:
            left.label(text=f"{index + 1} - ({display_name})", icon=custom_icon)

        right = split.row(align=False)
        op_link = right.operator("fbp.link_image_frame", text="", icon='FILE_FOLDER', emboss=False)
        op_link.index = index
        op_link.rig_name = rig.name
        right.prop(item, "duration", text="")
        right.prop(item, "is_selected", text="")


class FBP_UL_PendingList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        split = layout.split(factor=0.03, align=True)
        split.label(text=f"{index + 1}")
        row = split.row(align=True)
        row.prop(item, "fbp_color_tag", text="", icon_only=True)
        row.separator()
        row.prop(item, "name", text="", emboss=True)
        folder_icon = 'NEWFOLDER' if not item.files_str else 'FOLDER_REDIRECT'
        op = row.operator("fbp.edit_pending_plane", icon=folder_icon, text="")
        op.index = index


# ── UI HELPERS ────────────────────────────────────────────────────────────────

def draw_creation_ui(layout, context):
    sc = context.scene

    row = layout.row(align=False)
    row.scale_y = 1.3
    row.prop(sc, "fbp_creation_mode", expand=True)

    if sc.fbp_creation_mode == 'SINGLE':
        box = layout.box()
        box.label(text="Create Sequence", icon='OPTIONS')
        row = box.row(align=False)
        row.prop(sc, "fbp_pre_duration", text='Frame Duration')
        shadeless_icon = ('OUTLINER_OB_LIGHTPROBE' if sc.fbp_pre_shadeless else 'OUTLINER_DATA_LIGHTPROBE')
        row.prop(sc, "fbp_pre_shadeless", text="", icon=shadeless_icon, toggle=True)
        row = box.row(align=True)
        row.prop(sc, "fbp_pre_loop_mode", expand=True)
        box.prop(sc, "fbp_pre_interpolation", expand=False)
        box.prop(sc, "fbp_pre_orientation",   expand=False)
        layout.separator()
        row = layout.row()
        row.scale_y = 1.2
        row.operator("fbp.import_sequence", text="Generate Single Plane", icon='FILE_IMAGE')

    else:
        box = layout.box()
        box.label(text="Pre-settings", icon='OPTIONS')
        row = box.row(align=False)
        row.prop(sc, "fbp_pre_duration", text="Frame Duration")
        shadeless_icon = ('OUTLINER_OB_LIGHTPROBE' if sc.fbp_pre_shadeless else 'OUTLINER_DATA_LIGHTPROBE')
        row.prop(sc, "fbp_pre_shadeless", text="Emission Texture", icon=shadeless_icon, toggle=True)
        box.prop(sc, "fbp_pre_loop_mode",     expand=False)
        box.prop(sc, "fbp_pre_interpolation", expand=False)
        box.prop(sc, "fbp_pre_orientation",   expand=False)

        box = layout.box()
        box.label(text="Camera Setup", icon='RESTRICT_VIEW_ON')
        row = box.row(align=False)
        cam_icon = 'VIEW_CAMERA' if sc.fbp_gen_camera else 'CAMERA_DATA'
        row.prop(sc, "fbp_gen_camera", icon=cam_icon, toggle=True)
        row.prop(sc, "fbp_cam_pivot", text='3D Cursor on Camera', icon='PIVOT_CURSOR', toggle=True)
        row = box.row(align=False)
        row.prop(sc, "fbp_layer_offset", text='Plane Distance')
        row.prop(sc, "fbp_auto_scale", text='Fit to Camera', icon='FULLSCREEN_ENTER', toggle=True)

        layout.separator()
        
        box = layout.box()
        box.label(text="Auto Build Project", icon='OUTLINER_COLLECTION')
        box.prop(sc, "fbp_project_path", text="")
        box.prop(sc, "fbp_import_main_folders_as_scenes", text="Main folders as separate Scenes")
        row = box.row(align=True)
        row.operator("fbp.auto_scene_builder", icon='IMPORT', text="Build Project")
        row.operator("fbp.open_project_folder", icon='FILE_FOLDER', text="")
        box.label(text="Uses the MultiPlane settings above: camera, orientation, spacing, fit and timeline.", icon='INFO')

        box = layout.box()
        box.label(text="Add Layers (Multiplane Setup)", icon='RENDERLAYERS')
        row = box.row()
        row.template_list("FBP_UL_PendingList", "",
                          sc, "fbp_pending_planes",
                          sc, "fbp_pending_planes_idx", rows=4)
        col = row.column(align=False)
        col.operator("fbp.add_pending_plane",   icon='ADD',    text="")
        col.operator("fbp.remove_pending_plane",icon='REMOVE', text="")
        col.separator()
        col.operator("fbp.move_pending_plane", icon='SORT_DESC', text="").direction = 'UP'
        col.operator("fbp.move_pending_plane", icon='SORT_ASC',  text="").direction = 'DOWN'

        row = layout.row()
        row.alignment = 'CENTER'
        row.operator("fbp.clear_pending_planes", icon='TRASH', text="Clear Setup")

        row = layout.row()
        row.scale_y = 1.2
        row.operator("fbp.generate_multiplane", text="Generate Multi Plane", icon='RENDERLAYERS')


# ── PANELS ────────────────────────────────────────────────────────────────────

class FBP_PT_Settings(Panel):
    bl_label       = "Settings"
    bl_idname      = "FBP_PT_settings"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Frame by Plane"
    bl_options     = {'DEFAULT_CLOSED'}
    bl_order       = 0

    def draw_header(self, context):
        self.layout.label(text="", icon='PREFERENCES')

    def draw(self, context):
        layout = self.layout
        sc = context.scene

        box = layout.box()
        box.label(text="Project Folder", icon='FILE_FOLDER')
        box.prop(sc, "fbp_project_path", text="")
        box.prop(sc, "fbp_import_main_folders_as_scenes", text="Main folders as separate Scenes")
        row = box.row(align=True)
        row.operator("fbp.auto_scene_builder", icon='OUTLINER_COLLECTION', text="Auto Build")
        row.operator("fbp.open_project_folder", icon='FILE_FOLDER', text="Open")
        row = box.row(align=True)
        row.operator("fbp.relink_from_project_root", icon='LINKED', text="Relink Missing")
        row.operator("fbp.select_missing_layers", icon='ERROR', text="Select Missing")
        row = box.row(align=True)
        row.operator("fbp.project_health_check", icon='CHECKMARK', text="Health Check")
        row.operator("fbp.sync_collection_colors", icon='COLOR', text="Sync Colors")
        row = box.row(align=True)
        row.operator("fbp.show_import_profile", icon='TIME', text="Import Report")
        box.prop(sc, "fbp_auto_collection_color_variants", text="Color Variants")

        box = layout.box()
        box.label(text="Emergency Render", icon='RENDER_ANIMATION')
        row = box.row(align=True)
        row.prop(sc, "fbp_emergency_render_start", text="Start")
        row.prop(sc, "fbp_emergency_render_end", text="End")
        box.prop(sc, "fbp_emergency_render_prefix", text="Prefix")
        row = box.row(align=True)
        row.operator("fbp.repair_render_state", icon='MODIFIER', text="Repair")
        row.operator("fbp.background_render_frames", icon='RENDER_ANIMATION', text="Background Render")
        box.label(text="Use this instead of Render Animation if Blender crashes.", icon='INFO')

        box = layout.box()
        box.label(text="Output Format (Camera)", icon='SCENE_DATA')
        box.prop(sc, "fbp_cam_ratio", text="Ratio")
        if sc.fbp_cam_ratio == 'CUSTOM':
            col = box.column(align=True)
            col.prop(sc.render, "resolution_x", text="X (px)")
            col.prop(sc.render, "resolution_y", text="Y (px)")

        layout.prop(sc, "fbp_show_previews", text="Show Thumbnails")
        layout.separator()
        layout.operator("fbp.save_file", text="Save Project", icon='FILE_TICK')

        selected_rigs = get_selected_rigs(context)
        if selected_rigs:
            box = layout.box()
            box.label(text="Stats", icon='INFO')
            col = box.column(align=False)

            num_images   = sum(len(rig.fbp_images) for rig in selected_rigs)
            total_frames = sum(sum(item.duration for item in rig.fbp_images) for rig in selected_rigs)

            missing_count = 0
            for rig in selected_rigs:
                plane = rig.fbp_plane_target
                if plane:
                    for mat in plane.data.materials:
                        if mat and mat.use_nodes:
                            for node in mat.node_tree.nodes:
                                if node.type == 'TEX_IMAGE' and node.image:
                                    p = node.image.filepath
                                    if p and not os.path.exists(bpy.path.abspath(p)):
                                        missing_count += 1

            if len(selected_rigs) == 1:
                rig   = selected_rigs[0]
                start = rig.fbp_start_frame
                end   = start + total_frames - 1 if total_frames > 0 else start
                col.label(text=f" {num_images} total images")
                col.label(text=f" {total_frames} frames (from {start} to {end})")
            else:
                col.label(text=f"{len(selected_rigs)} selected layers")
                col.label(text=f"{num_images} total images in group")
                col.label(text=f"{total_frames} total frames in group")

            if missing_count > 0:
                col.separator()
                col.label(text=f"⚠ {missing_count} Missing Files!", icon='ERROR')


class FBP_PT_LayerStack(Panel):
    bl_label       = "Layers"
    bl_idname      = "FBP_PT_layer_stack"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Frame by Plane"
    bl_order       = 1

    @classmethod
    def poll(cls, context):
        return any(is_fbp_layer_object(obj) for obj in context.scene.objects)

    def draw_header(self, context):
        self.layout.label(text="", icon='RENDER_RESULT')

    def draw(self, context):
        layout = self.layout
        sc = context.scene

        top = layout.row(align=True)
        top.prop(sc, "fbp_use_hierarchical_layers", text="Tree View", toggle=True, icon='OUTLINER_COLLECTION')
        top.prop(sc, "fbp_auto_sort_layers_by_depth", text="Depth", toggle=True, icon='SORTSIZE')
        top.operator("fbp.sync_collection_colors", text="", icon='COLOR')

        if sc.fbp_use_hierarchical_layers:
            box = layout.box()
            draw_fbp_hierarchical_layer_view(box, context)
            row = layout.row(align=True)
            row.operator("fbp.move_layer_stack", text="", icon='SORT_DESC').direction = 'DOWN'
            row.operator("fbp.move_layer_stack", text="", icon='SORT_ASC').direction  = 'UP'
            row.separator()
            row.operator("fbp.open_create_rig", text="", icon='ADD')
            row.operator("fbp.duplicate_selected_layers", text="", icon='DUPLICATE')
            row.separator()
            row.operator("fbp.delete_sequence", text="", icon='TRASH')
            row.operator("fbp.select_all_layers", text="", icon='RESTRICT_SELECT_OFF')
        else:
            visible_count = len(visible_layer_indices(context))
            layer_rows = min(max(visible_count, 4), 15)

            row = layout.row()
            row.template_list(
                "FBP_UL_LayerStack", "",
                context.scene, "fbp_layers",
                context.scene, "fbp_layer_stack_index",
                rows=layer_rows)

            col = row.column(align=False)
            col.operator("fbp.move_layer_stack", text="", icon='SORT_DESC').direction = 'DOWN'
            col.operator("fbp.move_layer_stack", text="", icon='SORT_ASC').direction  = 'UP'
            col.separator()
            col.operator("fbp.open_create_rig", text="", icon='ADD')
            col.separator()
            col.operator("fbp.duplicate_selected_layers", text="", icon='DUPLICATE')
            col.operator("fbp.delete_sequence", text="", icon='TRASH')
            col.separator()
            col.operator("fbp.select_all_layers", text="", icon='RESTRICT_SELECT_OFF')


class FBP_PT_Sequence(Panel):
    bl_label       = "Sequence"
    bl_idname      = "FBP_PT_sequence"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Frame by Plane"
    bl_order       = 2

    @classmethod
    def poll(cls, context):
        return bool(get_selected_rigs(context))

    def draw_header(self, context):
        self.layout.label(text="", icon='IMAGE_BACKGROUND')

    def draw(self, context):
        layout = self.layout
        selected_rigs = get_selected_rigs(context)
        if not selected_rigs:
            return

        rig = selected_rigs[0]

        box = layout.box()

        row = box.row(align=False)
        row.prop(rig, "fbp_color_tag", text="", icon_only=False)
        row.prop(rig, "name", text="", icon='IMAGE_BACKGROUND')
        row.operator("fbp.replace_sequence", text="", icon='FOLDER_REDIRECT')

        row = box.row(align=False)
        vis_icon = 'HIDE_OFF' if rig.fbp_is_visible else 'HIDE_ON'
        row.prop(rig, "fbp_is_visible", text="", icon=vis_icon)
        row.prop(rig, "fbp_opacity", text="Opacity", slider=True)
        emiss_icon = ('OUTLINER_OB_LIGHTPROBE' if rig.fbp_use_emission
                      else 'OUTLINER_DATA_LIGHTPROBE')
        row.prop(rig, "fbp_use_emission", text="", icon=emiss_icon, toggle=True)

        row = box.row(align=False)
        row.prop(rig, "fbp_track_cam", toggle=True, icon='CON_CAMERASOLVER')
        if len(selected_rigs) > 1:
            row.operator("fbp.multi_fit_camera", text="Fit To Cam", icon='FULLSCREEN_ENTER')
        else:
            row.operator("fbp.fit_camera", icon='FULLSCREEN_ENTER', text="Fit Camera")

        row = box.row(align=False)
        rot_text = "Horizontal" if getattr(rig, "fbp_is_vertical", False) else "Vertical"
        row.operator("fbp.transform", text=rot_text, icon='FILE_REFRESH').mode = 'TOGGLE_ROT'
        row.operator("fbp.transform", text="To Ground", icon='GRID').mode = 'TO_GROUND'

        box = layout.box()
        box.label(text="Animation", icon='ONIONSKIN_ON')
        row = box.row(align=False)
        sub1 = row.row(align=True)
        sub1.prop(rig, "fbp_start_frame")
        sub1.operator("fbp.set_current_frame", text="", icon='EYEDROPPER')
        row.prop(rig, "fbp_loop_mode", text="")
        row.operator("fbp.reverse_sequence", text="", icon='ARROW_LEFTRIGHT')

        if len(selected_rigs) <= 1:
            box = layout.box()
            box.label(text="Images", icon='RENDER_RESULT')
            row = box.row()
            row.template_list("FBP_UL_ImageList", "",
                              rig, "fbp_images",
                              rig, "fbp_images_index", rows=8)
            col = row.column(align=False)
            col.operator("fbp.list_action", icon='TRIA_UP', text="").action = 'MOVE_UP'
            col.operator("fbp.list_action", icon='TRIA_DOWN', text="").action = 'MOVE_DOWN'
            col.separator()
            col.operator("fbp.insert_images_after_selected", icon='ADD', text="")
            col.operator("fbp.list_action", icon='SORT_ASC', text="").action = 'SORT_NATURAL'
            col.operator("fbp.list_action", icon='DUPLICATE', text="").action = 'DUPLICATE_SELECTED'
            col.operator("fbp.list_action", icon='PANEL_CLOSE', text="").action = 'REMOVE'

            row = box.row(align=False)
            row.operator("fbp.select_all", text="All",    icon="PROP_ON").action  = 'ALL'
            row.operator("fbp.select_all", text="None",   icon="PROP_OFF").action = 'NONE'
            row.operator("fbp.select_all", text="Invert", icon="PROP_CON").action = 'INVERT'

            row = box.row(align=False)
            row.operator("fbp.list_action", text="Remove Unchecked", icon='TRASH').action = 'REMOVE_UNCHECKED'

        row = layout.row(align=False)
        row.prop(rig, "fbp_global_duration", text="Duration")
        row.operator("fbp.batch_apply", text="Apply", icon='CHECKMARK')


class FBP_PT_CreateFirst(Panel):
    bl_label       = "Create Sequence"
    bl_idname      = "FBP_PT_create_first"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Frame by Plane"
    bl_order       = 3

    @classmethod
    def poll(cls, context):
        return not any(getattr(obj, "is_fbp_control", False) for obj in context.scene.objects)

    def draw_header(self, context):
        self.layout.label(text="", icon='ADD')

    def draw(self, context):
        draw_creation_ui(self.layout, context)


class FBP_PT_CreateExisting(Panel):
    bl_label       = "Create New Rig"
    bl_idname      = "FBP_PT_create_existing"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Frame by Plane"
    bl_order       = 4

    @classmethod
    def poll(cls, context):
        return (any(getattr(obj, "is_fbp_control", False) for obj in context.scene.objects)
                and getattr(context.scene, "fbp_show_create_tools", False))

    def draw_header(self, context):
        self.layout.label(text="", icon='ADD')

    def draw(self, context):
        draw_creation_ui(self.layout, context)


# ── OPERATORS ─────────────────────────────────────────────────────────────────

class FBP_OT_SaveFile(Operator):
    bl_idname      = "fbp.save_file"
    bl_label       = "Save File"
    bl_description = "Quickly save the current .blend file"

    def execute(self, context):
        if not bpy.data.is_saved:
            bpy.ops.wm.save_as_mainfile('INVOKE_DEFAULT')
        else:
            bpy.ops.wm.save_mainfile()
            self.report({'INFO'}, "Project saved!")
        return {'FINISHED'}


class FBP_OT_OpenCreateRig(Operator):
    bl_idname      = "fbp.open_create_rig"
    bl_label       = "Create New Frame by Plane Rig"
    bl_description = "Deselect layers and show the Create New Rig panel"
    bl_options     = {'UNDO'}

    def execute(self, context):
        bpy.ops.object.select_all(action='DESELECT')
        context.scene.fbp_show_create_tools = True
        return {'FINISHED'}


class FBP_OT_SelectLayerExclusive(Operator):
    bl_idname      = "fbp.select_layer_exclusive"
    bl_label       = "Select Layer"
    bl_description = "Select only this layer. Use the checkbox for additive multi-selection"
    bl_options     = {'UNDO'}

    rig_name: StringProperty(default="")

    def execute(self, context):
        rig = bpy.data.objects.get(self.rig_name)
        if not rig or not is_fbp_layer_object(rig):
            return {'CANCELLED'}

        if not object_in_view_layer(rig, context):
            if not ensure_object_in_active_collection(rig, context):
                sync_layer_collection(context)
                self.report({'WARNING'}, "Layer is not in the active View Layer")
                return {'CANCELLED'}

        bpy.ops.object.select_all(action='DESELECT')
        rig.select_set(True)
        context.view_layer.objects.active = rig

        for i, item in enumerate(context.scene.fbp_layers):
            try:
                if item.obj == rig:
                    context.scene.fbp_layer_stack_index = i
                    break
            except ReferenceError:
                pass
        return {'FINISHED'}


class FBP_OT_DuplicateOrDefault(Operator):
    bl_idname      = "fbp.duplicate_or_default"
    bl_label       = "Duplicate"
    bl_description = "Shift+D: duplicate FBP rigs safely, otherwise use Blender's standard duplicate"
    bl_options     = {'UNDO'}

    def invoke(self, context, event):
        if get_selected_rigs(context):
            result = bpy.ops.fbp.duplicate_selected_layers()
            if 'FINISHED' in result:
                return bpy.ops.transform.translate('INVOKE_DEFAULT')
            return result
        return bpy.ops.object.duplicate_move('INVOKE_DEFAULT')



class FBP_OT_SelectAllLayers(Operator):
    bl_idname      = "fbp.select_all_layers"
    bl_label       = "Select All Layers"
    bl_description = "Select all Frame By Plane rigs in the scene"

    def execute(self, context):
        bpy.ops.object.select_all(action='DESELECT')
        count = 0
        for idx in visible_layer_indices(context):
            item = context.scene.fbp_layers[idx]
            obj = _safe_layer_obj(item)
            if obj and is_fbp_layer_object(obj):
                obj.select_set(True)
                context.view_layer.objects.active = obj
                count += 1
        self.report({'INFO'}, f"{count} layers selected")
        return {'FINISHED'}


class FBP_OT_ToggleLock(Operator):
    bl_idname      = "fbp.toggle_lock"
    bl_label       = "Toggle Lock"
    bl_description = "Toggle object selectability in viewport. Shift+Click to apply to all selected"
    bl_options     = {'UNDO'}

    rig_name: StringProperty(default="")
    target:   StringProperty(default="RIG")
    shift:    BoolProperty(default=False)

    def invoke(self, context, event):
        self.shift = event.shift
        return self.execute(context)

    def execute(self, context):
        rigs = (get_selected_rigs(context) if self.shift
                else ([bpy.data.objects.get(self.rig_name)] if self.rig_name
                      else get_selected_rigs(context)))
        for rig in rigs:
            if not rig:
                continue
            if self.target == 'RIG':
                rig.hide_select = not rig.hide_select
            elif self.target == 'PLANE':
                plane = rig.fbp_plane_target
                if plane:
                    plane.hide_select = not plane.hide_select
        return {'FINISHED'}


class FBP_OT_ToggleSelectLayer(Operator):
    bl_idname      = "fbp.toggle_select_layer"
    bl_label       = "Toggle Layer Selection"
    bl_description = "Add or remove this layer from the selection"
    bl_options     = {'UNDO'}

    rig_name: StringProperty()

    def execute(self, context):
        rig = bpy.data.objects.get(self.rig_name)
        if rig:
            new_state = not rig.select_get()
            rig.select_set(new_state)
            if new_state:
                context.view_layer.objects.active = rig
        return {'FINISHED'}


class FBP_OT_ToggleSolo(Operator):
    bl_idname      = "fbp.toggle_solo"
    bl_label       = "Solo Layer"
    bl_description = "Isolate this layer. Click others to add them to the view"
    bl_options     = {'UNDO'}

    rig_name: StringProperty()

    def execute(self, context):
        sc = context.scene
        target_item = next(
            (item for item in sc.fbp_layers if item.obj and item.obj.name == self.rig_name),
            None)
        if not target_item:
            return {'CANCELLED'}

        active_items = [item for item in sc.fbp_layers if item.solo]

        if not active_items:
            for item in sc.fbp_layers:
                item.solo = False
                if item.obj:
                    item.obj.fbp_is_visible = False
            target_item.solo = True
            if target_item.obj:
                target_item.obj.fbp_is_visible = True
        elif len(active_items) == 1 and target_item.solo:
            for item in sc.fbp_layers:
                item.solo = False
                if item.obj:
                    item.obj.fbp_is_visible = True
        else:
            target_item.solo = not target_item.solo
            if target_item.obj:
                target_item.obj.fbp_is_visible = target_item.solo

        if not any(item.solo for item in sc.fbp_layers):
            for item in sc.fbp_layers:
                if item.obj:
                    item.obj.fbp_is_visible = True

        update_global_visibility(context)
        return {'FINISHED'}


class FBP_OT_MoveLayerStack(Operator):
    bl_idname      = "fbp.move_layer_stack"
    bl_label       = "Move Layer"
    bl_description = "Move this layer and recalculate depth automatically"

    direction: StringProperty()

    def execute(self, context):
        sc = context.scene
        idx = sc.fbp_layer_stack_index
        layers = sc.fbp_layers
        if not (0 <= idx < len(layers)):
            return {'CANCELLED'}

        current_rig = _safe_layer_obj(layers[idx])
        if not current_rig:
            return {'CANCELLED'}

        visible = visible_layer_indices(context, same_collection_as=current_rig)
        display_order = list(reversed(visible))
        if idx not in display_order or len(display_order) < 2:
            self.report({'WARNING'}, "No visible neighbour in this collection")
            return {'CANCELLED'}

        pos = display_order.index(idx)
        new_pos = pos - 1 if self.direction == 'UP' else pos + 1
        if not (0 <= new_pos < len(display_order)):
            return {'CANCELLED'}

        target_idx = display_order[new_pos]
        target_rig = _safe_layer_obj(layers[target_idx])
        if not target_rig:
            return {'CANCELLED'}

        swap_layer_depth_only(context, current_rig, target_rig)
        layers.move(idx, target_idx)
        sc.fbp_layer_stack_index = target_idx
        return {'FINISHED'}


class FBP_OT_IsolateLayer(Operator):
    bl_idname      = "fbp.isolate_layer"
    bl_label       = "Isolate Layer"
    bl_description = "Hide all other layers. Click again to show all"
    bl_options     = {'UNDO'}

    def execute(self, context):
        selected_rigs = get_selected_rigs(context)
        if not selected_rigs:
            return {'CANCELLED'}
        all_rigs = [ob for ob in context.scene.objects if getattr(ob, "is_fbp_control", False)]
        visible_rigs = [ob for ob in all_rigs if getattr(ob, "fbp_is_visible", False)]
        is_solo = set(visible_rigs) == set(selected_rigs)
        for rig in all_rigs:
            rig.fbp_is_visible = True if is_solo else (rig in selected_rigs)
        return {'FINISHED'}


class FBP_OT_FitToCamera(Operator):
    bl_idname      = "fbp.fit_camera"
    bl_label       = "Fit to Camera"
    bl_description = "Scale the layer to exactly cover the render frame"
    bl_options     = {'UNDO'}

    def execute(self, context):
        cam = context.scene.camera
        if not cam:
            self.report({'WARNING'}, "No active camera!")
            return {'CANCELLED'}
        rigs = get_selected_rigs(context)
        if not rigs:
            return {'CANCELLED'}
        context.view_layer.update()
        context.evaluated_depsgraph_get().update()
        for rig in rigs:
            apply_fit_to_camera(context, rig, cam)
        return {'FINISHED'}


class FBP_OT_MultiFitCamera(Operator):
    bl_idname      = "fbp.multi_fit_camera"
    bl_label       = "Fit All to Camera"
    bl_description = "Scale all selected rigs to fit the camera frame"
    bl_options     = {'UNDO'}

    def execute(self, context):
        cam = context.scene.camera
        if not cam:
            self.report({'WARNING'}, "No active camera!")
            return {'CANCELLED'}
        rigs = get_selected_rigs(context)
        if not rigs:
            self.report({'WARNING'}, "No rig selected!")
            return {'CANCELLED'}
        for rig in rigs:
            apply_fit_to_camera(context, rig, cam)
        self.report({'INFO'}, f"{len(rigs)} layers fitted to camera")
        return {'FINISHED'}


class FBP_OT_SetCurrentFrame(Operator):
    bl_idname      = "fbp.set_current_frame"
    bl_label       = "Set to Current Frame"
    bl_description = "Set the animation start to the current timeline frame"
    bl_options     = {'UNDO'}

    def execute(self, context):
        for rig in get_selected_rigs(context):
            rig.fbp_start_frame = context.scene.frame_current
        return {'FINISHED'}


class FBP_OT_ImportFolderHierarchy(Operator):
    bl_idname      = "fbp.import_folder_hierarchy"
    bl_label       = "Import from Folder"
    bl_description = "Auto-import mixed folders and single images to the Pending List"
    bl_options     = {'UNDO'}

    def execute(self, context):
        sc = context.scene
        base = bpy.path.abspath(sc.fbp_parent_import_path)
        if not os.path.isdir(base):
            self.report({'ERROR'}, "Invalid or unset directory!")
            return {'CANCELLED'}

        entries = []
        for name in os.listdir(base):
            if is_hidden_import_name(name):
                continue
            path = os.path.join(base, name)
            if os.path.isdir(path):
                files = sorted(
                    (f for f in os.listdir(path)
                     if not is_hidden_import_name(f) and is_supported_image_file(f) and not is_technical_map_file(f)),
                    key=natural_sort_key
                )
                if files:
                    entries.append((name, path, files, 'FOLDER'))
            elif is_supported_image_file(name) and not is_technical_map_file(name):
                entries.append((clean_layer_name_from_path(name), base, [name], 'IMAGE'))

        entries.sort(key=lambda e: natural_sort_key(e[0]))
        sc.fbp_pending_planes.clear()

        for index, (name, directory, files, kind) in enumerate(entries):
            item = sc.fbp_pending_planes.add()
            item.name = name
            item.directory = directory
            item.files_str = '|'.join(files)
            item.fbp_color_tag = f"COLOR_{(index % 9) + 1:02d}"

        if entries:
            self.report({'INFO'}, f"Imported {len(entries)} layer(s) from mixed folder")
        else:
            self.report({'WARNING'}, "No valid image sequences or single images found.")
        return {'FINISHED'}


class FBP_OT_AddPendingPlane(Operator):
    bl_idname      = "fbp.add_pending_plane"
    bl_label       = "Add Empty Layer"
    bl_description = "Add an empty row to the MultiPlane setup"

    def execute(self, context):
        sc = context.scene
        idx = len(sc.fbp_pending_planes)
        item = sc.fbp_pending_planes.add()
        item.name = f"Layer {idx + 1}"
        item.fbp_color_tag = f"COLOR_{(idx % 9) + 1:02d}"
        sc.fbp_pending_planes_idx = idx
        return {'FINISHED'}


class FBP_OT_EditPendingPlane(Operator):
    bl_idname      = "fbp.edit_pending_plane"
    bl_label       = "Choose Images"
    bl_description = "Open file manager to assign images to this layer"

    index:     IntProperty()
    filepath:  StringProperty(subtype='FILE_PATH')
    directory: StringProperty(subtype='DIR_PATH')
    files:     CollectionProperty(type=bpy.types.OperatorFileListElement)

    def invoke(self, context, event):
        path = context.scene.fbp_project_path or context.scene.fbp_last_directory
        if path:
            self.directory = path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.files:
            return {'CANCELLED'}
        sc = context.scene
        sc.fbp_last_directory = self.directory
        if 0 <= self.index < len(sc.fbp_pending_planes):
            item = sc.fbp_pending_planes[self.index]
            item.directory = self.directory
            sorted_files = sorted([f.name for f in self.files], key=natural_sort_key)
            item.files_str = "|".join(sorted_files)
            if len(sorted_files) == 1:
                item.name = clean_layer_name_from_path(sorted_files[0])
            else:
                folder_name = clean_layer_name_from_path(os.path.basename(os.path.normpath(self.directory)))
                if folder_name:
                    item.name = folder_name
        return {'FINISHED'}


class FBP_OT_MovePendingPlane(Operator):
    bl_idname      = "fbp.move_pending_plane"
    bl_label       = "Move Layer"
    bl_description = "Change the order of layers in the MultiPlane setup"

    direction: StringProperty()

    def execute(self, context):
        sc = context.scene
        idx = sc.fbp_pending_planes_idx
        new_idx = idx - 1 if self.direction == 'UP' else idx + 1
        if 0 <= new_idx < len(sc.fbp_pending_planes):
            sc.fbp_pending_planes.move(idx, new_idx)
            sc.fbp_pending_planes_idx = new_idx
        return {'FINISHED'}


class FBP_OT_RemovePendingPlane(Operator):
    bl_idname      = "fbp.remove_pending_plane"
    bl_label       = "Remove Layer"
    bl_description = "Delete the selected layer from the list"

    def execute(self, context):
        sc = context.scene
        idx = sc.fbp_pending_planes_idx
        if 0 <= idx < len(sc.fbp_pending_planes):
            sc.fbp_pending_planes.remove(idx)
            if idx > 0:
                sc.fbp_pending_planes_idx -= 1
        return {'FINISHED'}


class FBP_OT_ClearPendingPlanes(Operator):
    bl_idname      = "fbp.clear_pending_planes"
    bl_label       = "Clear List"
    bl_description = "Completely empty the MultiPlane setup"
    bl_options     = {'UNDO'}

    def execute(self, context):
        context.scene.fbp_pending_planes.clear()
        return {'FINISHED'}


class FBP_OT_AutoSceneBuilder(Operator):
    bl_idname      = "fbp.auto_scene_builder"
    bl_label       = "Auto Build Project"
    bl_description = "Build Collections, camera and Frame by Plane layers from the Project Folder"
    bl_options     = {'REGISTER', 'UNDO'}

    def _child_entries(self, path):
        entries = []
        try:
            names = os.listdir(path)
        except Exception:
            return entries
        for name in names:
            if is_hidden_import_name(name):
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                if self._folder_has_importable_content(full):
                    entries.append(('DIR', name, full))
            elif is_supported_image_file(name) and not is_technical_map_file(name):
                entries.append(('IMAGE', clean_layer_name_from_path(name), full))
        entries.sort(key=lambda e: natural_sort_key(e[1]))
        return entries

    def _folder_has_importable_content(self, path):
        try:
            for name in os.listdir(path):
                if is_hidden_import_name(name):
                    continue
                full = os.path.join(path, name)
                if os.path.isdir(full) and self._folder_has_importable_content(full):
                    return True
                if not is_hidden_import_name(name) and is_supported_image_file(name) and not is_technical_map_file(name):
                    return True
        except Exception:
            return False
        return False

    def _folder_direct_images(self, path):
        try:
            return sorted(
                [name for name in os.listdir(path)
                 if os.path.isfile(os.path.join(path, name))
                 and not is_hidden_import_name(name)
                 and is_supported_image_file(name)
                 and not is_technical_map_file(name)],
                key=natural_sort_key
            )
        except Exception:
            return []

    def _folder_direct_dirs(self, path):
        try:
            return sorted(
                [name for name in os.listdir(path)
                 if os.path.isdir(os.path.join(path, name))
                 and not is_hidden_import_name(name)
                 and self._folder_has_importable_content(os.path.join(path, name))],
                key=natural_sort_key
            )
        except Exception:
            return []

    def _build_folder(self, context, folder_path, parent_collection, cursor_loc, depth_counter, color_seed=0, depth=0):
        folder_name = clean_layer_name_from_path(folder_path)
        color_tag = f"COLOR_{(color_seed % 9) + 1:02d}"
        coll = get_or_create_child_collection(parent_collection, folder_name, color_tag)

        direct_images = self._folder_direct_images(folder_path)
        direct_dirs = self._folder_direct_dirs(folder_path)

        generated = []

        # Se una cartella interna contiene solo immagini, è una sequenza/layer.
        # Le cartelle al primo livello del Project Folder restano invece Collections,
        # così immagini tipo lvl1/lvl2 diventano piani statici separati.
        if direct_images and not direct_dirs and depth > 0:
            rig_loc = cursor_loc.copy()
            offset = context.scene.fbp_layer_offset * depth_counter[0]
            if context.scene.fbp_pre_orientation == 'HORIZ':
                rig_loc.z -= offset
            else:
                rig_loc.y += offset
            rig = build_fbp_rig(
                context,
                folder_name,
                folder_path,
                direct_images,
                rig_loc,
                color_tag=color_tag,
                target_collection=parent_collection,
                color_variant_index=depth_counter[0]
            )
            rig.fbp_depth_order = depth_counter[0]
            depth_counter[0] += 1
            return [rig]

        # Se contiene sottocartelle e immagini singole, diventa Collection e ordina tutto insieme.
        entries = self._child_entries(folder_path)
        local_variant = 0
        for kind, name, full in entries:
            if kind == 'IMAGE':
                rig_loc = cursor_loc.copy()
                offset = context.scene.fbp_layer_offset * depth_counter[0]
                if context.scene.fbp_pre_orientation == 'HORIZ':
                    rig_loc.z -= offset
                else:
                    rig_loc.y += offset
                rig = build_fbp_rig(
                    context,
                    name,
                    folder_path,
                    [os.path.basename(full)],
                    rig_loc,
                    color_tag=safe_collection_color_tag(coll, color_tag),
                    target_collection=coll,
                    color_variant_index=local_variant
                )
                rig.fbp_depth_order = depth_counter[0]
                depth_counter[0] += 1
                local_variant += 1
                generated.append(rig)
            elif kind == 'DIR':
                child_images = self._folder_direct_images(full)
                child_dirs = self._folder_direct_dirs(full)
                if child_images and not child_dirs:
                    rig_loc = cursor_loc.copy()
                    offset = context.scene.fbp_layer_offset * depth_counter[0]
                    if context.scene.fbp_pre_orientation == 'HORIZ':
                        rig_loc.z -= offset
                    else:
                        rig_loc.y += offset
                    rig = build_fbp_rig(
                        context,
                        clean_layer_name_from_path(full),
                        full,
                        child_images,
                        rig_loc,
                        color_tag=safe_collection_color_tag(coll, color_tag),
                        target_collection=coll,
                        color_variant_index=local_variant
                    )
                    rig.fbp_depth_order = depth_counter[0]
                    depth_counter[0] += 1
                    local_variant += 1
                    generated.append(rig)
                else:
                    generated.extend(self._build_folder(context, full, coll, cursor_loc, depth_counter, color_seed + local_variant + 1, depth + 1))
                    local_variant += 1
        return generated

    def execute(self, context):
        sc = context.scene
        base = bpy.path.abspath(sc.fbp_project_path)
        if not base or not os.path.isdir(base):
            self.report({'WARNING'}, "Set a valid Project Folder in Settings!")
            return {'CANCELLED'}

        root_name = FBP_PROJECT_COLLECTION_PREFIX + clean_layer_name_from_path(base)
        root_coll = get_or_create_child_collection(sc.collection, root_name, 'COLOR_09')
        cursor_loc = sc.cursor.location.copy()
        depth_counter = [0]

        bpy.ops.object.select_all(action='DESELECT')

        # The project builder now behaves like the MultiPlane generator:
        # it respects the setup box settings and can create/move the camera
        # directly inside the generated project Collection.
        if sc.fbp_gen_camera:
            cam_dist = 10.0
            cam_loc = cursor_loc.copy()
            if sc.fbp_pre_orientation == 'HORIZ':
                cam_loc.z += cam_dist
                bpy.ops.object.camera_add(location=cam_loc, rotation=(0, 0, 0))
            else:
                cam_loc.y -= cam_dist
                bpy.ops.object.camera_add(location=cam_loc, rotation=(math.radians(90), 0, 0))
            sc.camera = context.active_object
            move_object_to_collection(sc.camera, root_coll)
            if sc.fbp_cam_pivot:
                sc.cursor.location = cam_loc
                context.scene.tool_settings.transform_pivot_point = 'CURSOR'
            sc.fbp_gen_camera = False
            sc.fbp_cam_pivot = False

        generated = []

        top_entries = self._child_entries(base)
        for i, (kind, name, full) in enumerate(top_entries):
            if kind == 'DIR':
                generated.extend(self._build_folder(context, full, root_coll, cursor_loc, depth_counter, i, depth=0))
            elif kind == 'IMAGE':
                # Audio e file non immagine vengono già esclusi; immagine diretta = layer statico root.
                rig_loc = cursor_loc.copy()
                offset = sc.fbp_layer_offset * depth_counter[0]
                if sc.fbp_pre_orientation == 'HORIZ':
                    rig_loc.z -= offset
                else:
                    rig_loc.y += offset
                rig = build_fbp_rig(context, name, base, [os.path.basename(full)], rig_loc,
                                    color_tag=f"COLOR_{(i % 9) + 1:02d}", target_collection=root_coll,
                                    color_variant_index=i)
                rig.fbp_depth_order = depth_counter[0]
                depth_counter[0] += 1
                generated.append(rig)

        if not generated:
            self.report({'WARNING'}, "No valid image layers found in Project Folder")
            return {'CANCELLED'}

        if sc.fbp_auto_scale and sc.camera:
            context.view_layer.update()
            context.evaluated_depsgraph_get().update()
            for rig in generated:
                apply_fit_to_camera(context, rig, sc.camera)


        sync_layer_collection(context)
        sync_collection_colors_to_rigs(context)
        for rig in generated:
            if object_in_view_layer(rig, context):
                rig.select_set(True)
        if generated and object_in_view_layer(generated[-1], context):
            context.view_layer.objects.active = generated[-1]

        set_viewport_object_color(context)
        self.report({'INFO'}, f"Auto Build Project: {len(generated)} layer(s) created")
        return {'FINISHED'}


class FBP_OT_GenerateMultiplane(Operator):
    bl_idname      = "fbp.generate_multiplane"
    bl_label       = "Generate Multiplane"
    bl_description = "Generate the full plane system in 3D space"
    bl_options     = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene
        if not sc.fbp_pending_planes:
            self.report({'WARNING'}, "No layers added to the list!")
            return {'CANCELLED'}
        for p in sc.fbp_pending_planes:
            if not p.directory or not p.files_str:
                self.report({'ERROR'}, f"Layer '{p.name}' has no images assigned!")
                return {'CANCELLED'}

        cursor_loc = sc.cursor.location.copy()
        cam_dist = 10.0
        cam_loc = cursor_loc.copy()

        if sc.fbp_pre_orientation == 'HORIZ':
            cam_loc.z += cam_dist
        else:
            cam_loc.y -= cam_dist

        bpy.ops.object.select_all(action='DESELECT')

        source_path = bpy.path.abspath(sc.fbp_parent_import_path) if getattr(sc, "fbp_parent_import_path", "") else ""
        coll_base_name = clean_layer_name_from_path(source_path) if source_path else "Multi Plane"
        target_collection = get_or_create_child_collection(sc.collection, FBP_PROJECT_COLLECTION_PREFIX + coll_base_name, 'COLOR_09')

        if sc.fbp_gen_camera:
            if sc.fbp_pre_orientation == 'HORIZ':
                bpy.ops.object.camera_add(location=cam_loc, rotation=(0, 0, 0))
            else:
                bpy.ops.object.camera_add(
                    location=cam_loc, rotation=(math.radians(90), 0, 0))
            sc.camera = context.active_object
            move_object_to_collection(sc.camera, target_collection)
            if sc.fbp_cam_pivot:
                sc.cursor.location = cam_loc
                context.scene.tool_settings.transform_pivot_point = 'CURSOR'
            sc.fbp_gen_camera = False
            sc.fbp_cam_pivot = False

        cam = sc.camera
        last_rig = None
        generated_rigs = []

        for i, p_item in enumerate(sc.fbp_pending_planes):
            f_list = sorted(p_item.files_str.split("|"), key=natural_sort_key)
            rig_loc = cursor_loc.copy()
            offset = sc.fbp_layer_offset * i
            if sc.fbp_pre_orientation == 'HORIZ':
                rig_loc.z -= offset
            else:
                rig_loc.y += offset

            rig = build_fbp_rig(
                context, p_item.name, p_item.directory, f_list, rig_loc,
                p_item.fbp_color_tag, target_collection=target_collection, color_variant_index=i)
            rig.fbp_depth_order = i

            if sc.fbp_auto_scale and cam and not fbp_fast_import_is_active():
                context.view_layer.update()
                context.evaluated_depsgraph_get().update()
                apply_fit_to_camera(context, rig, cam)

            if not fbp_fast_import_is_active():
                rig.select_set(True)
            generated_rigs.append(rig)
            last_rig = rig


        if sc.fbp_auto_scale and cam:
            context.view_layer.update()
            context.evaluated_depsgraph_get().update()
            for rig in generated_rigs:
                apply_fit_to_camera(context, rig, cam)

        if fbp_fast_import_is_active():
            for rig in generated_rigs:
                if object_in_view_layer(rig, context):
                    rig.select_set(True)

        if last_rig:
            context.view_layer.objects.active = last_rig

        set_viewport_object_color(context)
        return {'FINISHED'}


class FBP_OT_ImportSequence(Operator):
    bl_idname      = "fbp.import_sequence"
    bl_label       = "Select Images"
    bl_description = "Open the file manager to import a sequence"
    bl_options     = {'REGISTER', 'UNDO'}

    filepath:  StringProperty(subtype='FILE_PATH')
    directory: StringProperty(subtype='DIR_PATH')
    files:     CollectionProperty(type=bpy.types.OperatorFileListElement)

    def invoke(self, context, event):
        path = context.scene.fbp_project_path or context.scene.fbp_last_directory
        if path:
            self.directory = path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.files:
            return {'CANCELLED'}
        context.scene.fbp_last_directory = self.directory
        f_list = sorted([f.name for f in self.files], key=natural_sort_key)
        single_name = clean_layer_name_from_path(f_list[0]) if len(f_list) == 1 else clean_layer_name_from_path(os.path.basename(os.path.normpath(self.directory))) or "Sequence_Rig"
        target_collection = context.collection if context.collection else context.scene.collection
        rig = build_fbp_rig(
            context, single_name, self.directory, f_list,
            context.scene.cursor.location.copy(), target_collection=target_collection)
        bpy.ops.object.select_all(action='DESELECT')
        rig.select_set(True)
        context.view_layer.objects.active = rig
        set_viewport_object_color(context)
        return {'FINISHED'}


class FBP_OT_ReplaceSequence(Operator):
    bl_idname      = "fbp.replace_sequence"
    bl_label       = "Replace Sequence"
    bl_description = "Replace plane files while keeping timing and keyframes"
    bl_options     = {'REGISTER', 'UNDO'}

    filepath:  StringProperty(subtype='FILE_PATH')
    directory: StringProperty(subtype='DIR_PATH')
    files:     CollectionProperty(type=bpy.types.OperatorFileListElement)

    def invoke(self, context, event):
        path = context.scene.fbp_project_path or context.scene.fbp_last_directory
        if path:
            self.directory = path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.files:
            return {'CANCELLED'}
        context.scene.fbp_last_directory = self.directory

        rig = context.object
        plane = rig.fbp_plane_target
        if not plane:
            return {'CANCELLED'}

        if plane.parent != rig:
            new_mesh = plane.data.copy()
            new_plane = plane.copy()
            new_plane.data = new_mesh
            context.collection.objects.link(new_plane)
            new_plane.parent = rig
            new_plane.matrix_local = plane.matrix_local
            rig.fbp_plane_target = new_plane
            plane = new_plane
            if plane.data.animation_data:
                plane.data.animation_data_clear()

        sorted_files = sorted([f.name for f in self.files], key=natural_sort_key)
        first_img = None
        plane.data.materials.clear()

        for i, f in enumerate(sorted_files):
            img_path = os.path.join(self.directory, f)
            mat = create_fbp_material(
                f"Mat_{f}", img_path,
                interp=getattr(rig, "fbp_interpolation", 'Closest'),
                opacity=rig.fbp_opacity)
            plane.data.materials.append(mat)
            if i < len(rig.fbp_images):
                rig.fbp_images[i].name = f
                rig.fbp_images[i].is_empty = False
                rig.fbp_images[i].filepath = img_path
            else:
                item = rig.fbp_images.add()
                item.name = f
                item.duration = rig.fbp_global_duration
                item.is_selected = True
                item.is_empty = False
                item.filepath = img_path
            if not first_img:
                try:
                    first_img = bpy.data.images.load(img_path, check_existing=True)
                except Exception:
                    pass

        while len(rig.fbp_images) > len(sorted_files):
            rig.fbp_images.remove(len(rig.fbp_images) - 1)

        if first_img:
            width, height = first_img.size
            if width > height:
                rig.scale = (1, height / width, 1)
            else:
                rig.scale = (width / height, 1, 1)
            rig.fbp_base_scale_vec = rig.scale
            rig.fbp_preview_path = first_img.filepath

        do_update_animation(rig)
        do_update_emission(rig)

        for img in list(bpy.data.images):
            if img.users == 0 and not getattr(img, "use_fake_user", False):
                bpy.data.images.remove(img)

        return {'FINISHED'}


class FBP_OT_UpdateAnimation(Operator):
    bl_idname  = "fbp.update_animation"
    bl_label   = "Update Animation"
    bl_options = {'UNDO', 'INTERNAL'}

    def execute(self, context):
        for rig in get_selected_rigs(context):
            do_update_animation(rig)
        return {'FINISHED'}


class FBP_OT_Transform(Operator):
    bl_idname      = "fbp.transform"
    bl_label       = "Transform"
    bl_description = "Rotate the plane or place it on the ground"
    bl_options     = {'UNDO'}

    mode: StringProperty()

    def execute(self, context):
        for rig in get_selected_rigs(context):
            if self.mode == 'TOGGLE_ROT':
                if rig.fbp_is_vertical:
                    rig.rotation_euler[0] = 0
                    rig.fbp_is_vertical = False
                else:
                    rig.rotation_euler[0] = math.radians(90)
                    rig.fbp_is_vertical = True
            elif self.mode == 'TO_GROUND':
                bbox_world = [rig.matrix_world @ mathutils.Vector(c) for c in rig.bound_box]
                min_z = min(v.z for v in bbox_world)
                rig.location.z -= min_z
        return {'FINISHED'}


class FBP_OT_UpdateEmission(Operator):
    bl_idname  = "fbp.update_emission"
    bl_label   = "Update Emission"
    bl_options = {'UNDO', 'INTERNAL'}

    def execute(self, context):
        for rig in get_selected_rigs(context):
            do_update_emission(rig)
        return {'FINISHED'}


class FBP_OT_UpdateOpacity(Operator):
    bl_idname  = "fbp.update_opacity"
    bl_label   = "Update Opacity"
    bl_options = {'UNDO', 'INTERNAL'}

    def execute(self, context):
        for rig in get_selected_rigs(context):
            do_update_opacity(rig)
        return {'FINISHED'}


class FBP_OT_UpdateTrack(Operator):
    bl_idname  = "fbp.update_track"
    bl_label   = "Update Track"
    bl_options = {'UNDO', 'INTERNAL'}

    def execute(self, context):
        for rig in get_selected_rigs(context):
            do_update_track(rig, context)
        return {'FINISHED'}


class FBP_OT_InsertImagesAfterSelected(Operator):
    bl_idname      = "fbp.insert_images_after_selected"
    bl_label       = "Add Empty Frame"
    bl_description = "Insert an empty transparent frame after the active image or after the last checked image"
    bl_options     = {'REGISTER', 'UNDO'}

    def execute(self, context):
        rig = context.object if context.object and getattr(context.object, "is_fbp_control", False) else None
        if not rig:
            rigs = get_selected_rigs(context)
            rig = rigs[0] if rigs else None
        if not rig or not rig.fbp_plane_target:
            self.report({'WARNING'}, "Select one Frame by Plane rig first")
            return {'CANCELLED'}

        plane = rig.fbp_plane_target
        image_data = [(item.name, item.duration, item.is_selected, getattr(item, 'is_empty', False), getattr(item, 'filepath', '')) for item in rig.fbp_images]
        material_data = [
            plane.data.materials[i] if i < len(plane.data.materials) else None
            for i in range(len(image_data))
        ]

        checked = [i for i, item in enumerate(rig.fbp_images) if item.is_selected]
        if checked:
            insert_at = checked[-1] + 1
        else:
            insert_at = min(max(rig.fbp_images_index, 0), len(image_data) - 1) + 1 if image_data else 0

        empty_mat = create_fbp_empty_material(f"Mat_Empty_{rig.name}_{insert_at + 1}")
        image_data.insert(insert_at, ("Empty Frame", rig.fbp_global_duration, True, True, ""))
        material_data.insert(insert_at, empty_mat)

        rig.fbp_images.clear()
        plane.data.materials.clear()
        for data, mat in zip(image_data, material_data):
            item = rig.fbp_images.add()
            item.name, item.duration, item.is_selected = data[0], data[1], data[2]
            item.is_empty = bool(data[3])
            item.filepath = data[4]
            if mat:
                plane.data.materials.append(mat)

        rig.fbp_images_index = min(insert_at, max(0, len(rig.fbp_images) - 1))
        do_update_animation(rig)
        do_update_emission(rig)
        do_update_opacity(rig)


        self.report({'INFO'}, "Inserted empty frame")
        return {'FINISHED'}


class FBP_OT_LinkImageFrame(Operator):
    bl_idname      = "fbp.link_image_frame"
    bl_label       = "Link Image to Frame"
    bl_description = "Link or replace the image used by this frame"
    bl_options     = {'REGISTER', 'UNDO'}

    index:     IntProperty(default=-1)
    rig_name:  StringProperty(default="")
    filepath:  StringProperty(subtype='FILE_PATH')
    directory: StringProperty(subtype='DIR_PATH')
    files:     CollectionProperty(type=bpy.types.OperatorFileListElement)

    def invoke(self, context, event):
        path = context.scene.fbp_project_path or context.scene.fbp_last_directory
        if path:
            self.directory = path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        rig = bpy.data.objects.get(self.rig_name) if self.rig_name else None
        if not rig or not getattr(rig, "is_fbp_control", False):
            rig = context.object if context.object and getattr(context.object, "is_fbp_control", False) else None
        if not rig:
            rigs = get_selected_rigs(context)
            rig = rigs[0] if rigs else None
        if not rig or not rig.fbp_plane_target:
            self.report({'WARNING'}, "Select one Frame by Plane rig first")
            return {'CANCELLED'}
        if not (0 <= self.index < len(rig.fbp_images)):
            self.report({'WARNING'}, "Invalid frame index")
            return {'CANCELLED'}

        chosen = None
        if self.files:
            for f in self.files:
                if os.path.splitext(f.name)[1].lower() in FBP_SUPPORTED_IMAGE_EXT:
                    chosen = f.name
                    break
        elif self.filepath and os.path.splitext(self.filepath)[1].lower() in FBP_SUPPORTED_IMAGE_EXT:
            chosen = os.path.basename(self.filepath)
            self.directory = os.path.dirname(self.filepath)

        if not chosen:
            self.report({'WARNING'}, "No supported image selected")
            return {'CANCELLED'}

        context.scene.fbp_last_directory = self.directory
        img_path = os.path.join(self.directory, chosen)
        mat = create_fbp_material(
            f"Mat_{chosen}", img_path,
            interp=getattr(rig, "fbp_interpolation", 'Closest'),
            opacity=rig.fbp_opacity)

        plane = rig.fbp_plane_target
        while len(plane.data.materials) < len(rig.fbp_images):
            plane.data.materials.append(create_fbp_empty_material("Mat_Empty_Autofill"))
        plane.data.materials[self.index] = mat

        item = rig.fbp_images[self.index]
        item.name = chosen
        item.filepath = img_path
        item.is_empty = False
        item.is_selected = True
        rig.fbp_images_index = self.index

        if not rig.fbp_preview_path:
            rig.fbp_preview_path = img_path

        do_update_animation(rig)
        do_update_emission(rig)
        do_update_opacity(rig)
        self.report({'INFO'}, f"Linked {chosen}")
        return {'FINISHED'}


class FBP_OT_SelectAll(Operator):
    bl_idname      = "fbp.select_all"
    bl_label       = "Select All"
    bl_description = "Quickly select/deselect images in the list"

    action: StringProperty()

    def execute(self, context):
        for rig in get_selected_rigs(context):
            for item in rig.fbp_images:
                if   self.action == 'ALL':    item.is_selected = True
                elif self.action == 'NONE':   item.is_selected = False
                elif self.action == 'INVERT': item.is_selected = not item.is_selected
        return {'FINISHED'}


class FBP_OT_ListAction(Operator):
    bl_idname      = "fbp.list_action"
    bl_label       = "List Action"
    bl_description = "Edit the image list while keeping material slots in sync"
    bl_options     = {'UNDO'}

    action: StringProperty()

    def _snapshot_item(self, item):
        return (item.name, item.duration, item.is_selected, getattr(item, 'is_empty', False), getattr(item, 'filepath', ''))

    def _restore_item(self, dst, data):
        dst.name = data[0]
        dst.duration = data[1]
        dst.is_selected = data[2]
        dst.is_empty = bool(data[3]) if len(data) > 3 else False
        dst.filepath = data[4] if len(data) > 4 else ""

    def _get_sequence_data(self, rig):
        plane = rig.fbp_plane_target
        if not plane:
            return [], []

        image_data = [self._snapshot_item(item) for item in rig.fbp_images]
        material_data = [
            plane.data.materials[i] if i < len(plane.data.materials) else None
            for i in range(len(image_data))
        ]
        return image_data, material_data

    def _rebuild_sequence(self, rig, image_data, material_data, new_index=None):
        plane = rig.fbp_plane_target
        if not plane:
            return

        rig.fbp_images.clear()
        plane.data.materials.clear()

        for data, mat in zip(image_data, material_data):
            item = rig.fbp_images.add()
            self._restore_item(item, data)
            if mat:
                plane.data.materials.append(mat)

        if len(rig.fbp_images) > 0:
            if new_index is None:
                new_index = min(rig.fbp_images_index, len(rig.fbp_images) - 1)
            rig.fbp_images_index = max(0, min(new_index, len(rig.fbp_images) - 1))
        else:
            rig.fbp_images_index = 0

        do_update_animation(rig)

    def _checked_indices(self, image_data):
        return [i for i, data in enumerate(image_data) if data[2]]

    def execute(self, context):
        for rig in get_selected_rigs(context):
            plane = rig.fbp_plane_target
            if not plane or len(rig.fbp_images) == 0:
                continue

            idx = max(0, min(rig.fbp_images_index, len(rig.fbp_images) - 1))
            image_data, material_data = self._get_sequence_data(rig)

            if self.action == 'REMOVE':
                # X = delete checked images. If none are checked, delete only the active row.
                remove_indices = self._checked_indices(image_data)
                if not remove_indices and idx < len(image_data):
                    remove_indices = [idx]

                for i in reversed(remove_indices):
                    if 0 <= i < len(image_data):
                        del image_data[i]
                        del material_data[i]

                new_index = min(idx, len(image_data) - 1) if image_data else 0
                self._rebuild_sequence(rig, image_data, material_data, new_index)

            elif self.action == 'MOVE_UP':
                if idx <= 0:
                    continue

                image_data[idx - 1], image_data[idx] = image_data[idx], image_data[idx - 1]
                material_data[idx - 1], material_data[idx] = material_data[idx], material_data[idx - 1]
                self._rebuild_sequence(rig, image_data, material_data, idx - 1)

            elif self.action == 'MOVE_DOWN':
                if idx >= len(image_data) - 1:
                    continue

                image_data[idx + 1], image_data[idx] = image_data[idx], image_data[idx + 1]
                material_data[idx + 1], material_data[idx] = material_data[idx], material_data[idx + 1]
                self._rebuild_sequence(rig, image_data, material_data, idx + 1)

            elif self.action == 'DUPLICATE_SELECTED':
                selected_indices = self._checked_indices(image_data)

                if not selected_indices:
                    self.report({'WARNING'}, "No checked images to duplicate")
                    continue

                insert_at = selected_indices[-1] + 1
                insert_images = [image_data[i] for i in selected_indices]
                insert_mats = [material_data[i] for i in selected_indices]

                image_data[insert_at:insert_at] = insert_images
                material_data[insert_at:insert_at] = insert_mats
                self._rebuild_sequence(rig, image_data, material_data, insert_at)

            elif self.action == 'SORT_NATURAL':
                pairs = list(zip(image_data, material_data))
                pairs.sort(key=lambda pair: natural_sort_key(pair[0][0]))
                image_data = [pair[0] for pair in pairs]
                material_data = [pair[1] for pair in pairs]
                self._rebuild_sequence(rig, image_data, material_data, 0)

            elif self.action == 'REMOVE_UNCHECKED':
                keep_indices = self._checked_indices(image_data)

                if not keep_indices:
                    self.report({'WARNING'}, "Cannot remove all images: no checked images")
                    continue

                image_data = [image_data[i] for i in keep_indices]
                material_data = [material_data[i] for i in keep_indices]
                new_index = min(idx, len(image_data) - 1)
                self._rebuild_sequence(rig, image_data, material_data, new_index)

        return {'FINISHED'}


class FBP_OT_BatchApply(Operator):
    bl_idname      = "fbp.batch_apply"
    bl_label       = "Apply"
    bl_description = "Apply the duration to all checked images"
    bl_options     = {'UNDO'}

    def execute(self, context):
        for rig in get_selected_rigs(context):
            for item in rig.fbp_images:
                if item.is_selected:
                    item.duration = rig.fbp_global_duration
            do_update_animation(rig)
        return {'FINISHED'}


class FBP_OT_ReverseSequence(Operator):
    bl_idname      = "fbp.reverse_sequence"
    bl_label       = "Reverse Sequence"
    bl_description = "Completely reverse the image order"
    bl_options     = {'UNDO'}

    def execute(self, context):
        for rig in get_selected_rigs(context):
            plane = rig.fbp_plane_target
            if not plane:
                continue
            reversed_data = [(item.name, item.duration, item.is_selected, getattr(item, 'is_empty', False), getattr(item, 'filepath', ''))
                             for item in rig.fbp_images]
            reversed_data.reverse()
            materials = list(plane.data.materials)
            materials.reverse()
            plane.data.materials.clear()
            for mat in materials:
                plane.data.materials.append(mat)
            rig.fbp_images.clear()
            for data in reversed_data:
                item = rig.fbp_images.add()
                item.name = data[0]
                item.duration = data[1]
                item.is_selected = data[2]
                item.is_empty = bool(data[3]) if len(data) > 3 else False
                item.filepath = data[4] if len(data) > 4 else ""
            do_update_animation(rig)
        return {'FINISHED'}


class FBP_OT_DuplicateSelectedLayers(Operator):
    bl_idname      = "fbp.duplicate_selected_layers"
    bl_label       = "Duplicate Selected Layers"
    bl_description = "Duplicate selected Frame By Plane rigs with their plane, materials and image list"
    bl_options     = {'UNDO'}

    def _copy_image_list(self, src_rig, dst_rig):
        dst_rig.fbp_images.clear()
        for src_item in src_rig.fbp_images:
            dst_item = dst_rig.fbp_images.add()
            dst_item.name = src_item.name
            dst_item.duration = src_item.duration
            dst_item.is_selected = src_item.is_selected
            dst_item.is_empty = getattr(src_item, 'is_empty', False)
            dst_item.filepath = getattr(src_item, 'filepath', '')
        dst_rig.fbp_images_index = min(src_rig.fbp_images_index, max(0, len(dst_rig.fbp_images) - 1))

    def _copy_materials(self, src_plane, dst_plane):
        dst_plane.data.materials.clear()
        for mat in src_plane.data.materials:
            if not mat:
                continue
            new_mat = mat.copy()
            new_mat.name = mat.name + "_Copy"
            dst_plane.data.materials.append(new_mat)

    def execute(self, context):
        selected_rigs = get_selected_rigs(context)
        duplicated = []

        if not selected_rigs:
            self.report({'WARNING'}, "No Frame By Plane rig selected")
            return {'CANCELLED'}

        for rig in selected_rigs:
            plane = rig.fbp_plane_target
            if not plane:
                continue

            source_collection = get_primary_fbp_collection(rig) or context.collection or context.scene.collection
            rig_collections = [source_collection]
            plane_collections = [source_collection]
            active_collection = source_collection

            new_rig = rig.copy()
            if rig.data:
                new_rig.data = rig.data.copy()
            new_rig.name = rig.name + "_Copy"
            new_rig.is_fbp_control = True
            new_rig.fbp_collection_name = source_collection.name if source_collection else ""

            if not any(existing == new_rig for existing in active_collection.objects):
                active_collection.objects.link(new_rig)
            for coll in rig_collections:
                if coll != active_collection and not any(existing == new_rig for existing in coll.objects):
                    coll.objects.link(new_rig)

            new_plane = plane.copy()
            if plane.data:
                new_plane.data = plane.data.copy()
            new_plane.name = plane.name + "_Copy"
            new_plane.is_fbp_plane = True
            new_plane.fbp_collection_name = source_collection.name if source_collection else ""

            if not any(existing == new_plane for existing in active_collection.objects):
                active_collection.objects.link(new_plane)
            for coll in plane_collections:
                if coll != active_collection and not any(existing == new_plane for existing in coll.objects):
                    coll.objects.link(new_plane)

            new_rig.matrix_world = rig.matrix_world.copy()
            plane_world = plane.matrix_world.copy()
            new_plane.matrix_world = plane_world
            new_plane.parent = new_rig
            new_plane.matrix_world = plane_world
            new_plane.hide_select = plane.hide_select

            self._copy_materials(plane, new_plane)
            self._copy_image_list(rig, new_rig)
            new_rig.fbp_plane_target = new_plane
            new_rig.fbp_preview_path = rig.fbp_preview_path

            do_update_animation(new_rig)
            do_update_emission(new_rig)
            do_update_opacity(new_rig)
            duplicated.append(new_rig)

        if not duplicated:
            self.report({'WARNING'}, "No valid Frame By Plane layers duplicated")
            return {'CANCELLED'}

        context.view_layer.update()
        bpy.ops.object.select_all(action='DESELECT')
        selectable = []
        for obj in duplicated:
            if not object_in_view_layer(obj, context):
                ensure_object_in_active_collection(obj, context)
            if object_in_view_layer(obj, context):
                obj.select_set(True)
                selectable.append(obj)
        if selectable:
            context.view_layer.objects.active = selectable[-1]

        sync_layer_collection(context)
        self.report({'INFO'}, f"Duplicated {len(duplicated)} layer(s)")
        return {'FINISHED'}


class FBP_OT_DeleteSequence(Operator):
    bl_idname      = "fbp.delete_sequence"
    bl_label       = "Delete Sequence"
    bl_description = "Delete selected Frame By Plane rigs and their planes"
    bl_options     = {'UNDO'}

    def execute(self, context):
        selected_rigs = get_selected_fbp_roots(context)
        if not selected_rigs:
            idx = context.scene.fbp_layer_stack_index
            if 0 <= idx < len(context.scene.fbp_layers):
                rig = _safe_layer_obj(context.scene.fbp_layers[idx])
                if rig and is_fbp_layer_object(rig):
                    selected_rigs = [rig]

        if not selected_rigs:
            sync_layer_collection(context)
            self.report({'WARNING'}, "No Frame By Plane rig selected")
            return {'CANCELLED'}
        deleted = delete_fbp_rigs(context, selected_rigs)
        if deleted <= 0:
            return {'CANCELLED'}
        self.report({'INFO'}, f"Deleted {deleted} Frame By Plane layer(s)")
        return {'FINISHED'}


class FBP_OT_DeleteOrDefault(Operator):
    bl_idname      = "fbp.delete_or_default"
    bl_label       = "Delete"
    bl_description = "Delete FBP rigs together with their planes, otherwise use Blender's standard delete"
    bl_options     = {'UNDO'}

    def invoke(self, context, event):
        roots = get_selected_fbp_roots(context)
        if roots:
            deleted = delete_fbp_rigs(context, roots)
            if deleted > 0:
                self.report({'INFO'}, f"Deleted {deleted} Frame By Plane layer(s)")
                return {'FINISHED'}
            return {'CANCELLED'}
        return bpy.ops.object.delete('INVOKE_DEFAULT')



class FBP_OT_ToggleCollectionCollapse(Operator):
    bl_idname      = "fbp.toggle_collection_collapse"
    bl_label       = "Collapse Collection"
    bl_description = "Open or collapse this collection in the Frame by Plane layer tree"
    bl_options     = {'UNDO'}

    collection_name: StringProperty(default="")

    def execute(self, context):
        coll = bpy.data.collections.get(self.collection_name)
        if not coll:
            return {'CANCELLED'}
        coll.fbp_collapsed = not coll.fbp_collapsed
        return {'FINISHED'}


class FBP_OT_SelectCollectionLayers(Operator):
    bl_idname      = "fbp.select_collection_layers"
    bl_label       = "Select Collection Layers"
    bl_description = "Select all Frame by Plane layers inside this collection. Shift-click adds to selection"
    bl_options     = {'UNDO'}

    collection_name: StringProperty(default="")
    extend: BoolProperty(default=False)

    def invoke(self, context, event):
        self.extend = bool(event.shift)
        return self.execute(context)

    def execute(self, context):
        coll = bpy.data.collections.get(self.collection_name)
        if not coll:
            return {'CANCELLED'}
        rigs = [rig for rig in iter_fbp_rigs_in_collection(coll, True) if object_in_view_layer(rig, context)]
        if not self.extend:
            bpy.ops.object.select_all(action='DESELECT')
        for rig in rigs:
            rig.select_set(True)
        if rigs:
            context.view_layer.objects.active = rigs[-1]
            # Use the active rig index for up/down buttons.
            for i, item in enumerate(context.scene.fbp_layers):
                try:
                    if item.obj == rigs[-1]:
                        context.scene.fbp_layer_stack_index = i
                        break
                except ReferenceError:
                    pass
        return {'FINISHED'}


class FBP_OT_ToggleCollectionVisibility(Operator):
    bl_idname      = "fbp.toggle_collection_visibility"
    bl_label       = "Toggle Collection Visibility"
    bl_description = "Hide/show this collection and all its Frame by Plane layers"
    bl_options     = {'UNDO'}

    collection_name: StringProperty(default="")

    def execute(self, context):
        coll = bpy.data.collections.get(self.collection_name)
        if not coll:
            return {'CANCELLED'}
        new_hidden = not collection_is_hidden_in_view_layer(context, coll)
        try:
            coll.hide_viewport = new_hidden
        except Exception:
            pass
        try:
            layer_coll = find_layer_collection(context.view_layer.layer_collection, coll)
            if layer_coll:
                layer_coll.hide_viewport = new_hidden
        except Exception:
            pass
        # Keep object-level visibility intact; the Collection is the parent switch.
        update_global_visibility(context)
        return {'FINISHED'}


class FBP_OT_ToggleCollectionLock(Operator):
    bl_idname      = "fbp.toggle_collection_lock"
    bl_label       = "Toggle Collection Lock"
    bl_description = "Lock/unlock all Frame by Plane rigs and planes inside this collection"
    bl_options     = {'UNDO'}

    collection_name: StringProperty(default="")

    def execute(self, context):
        coll = bpy.data.collections.get(self.collection_name)
        if not coll:
            return {'CANCELLED'}
        rigs = list(iter_fbp_rigs_in_collection(coll, True))
        if not rigs:
            return {'CANCELLED'}
        all_locked = all(getattr(rig, 'hide_select', False) for rig in rigs)
        new_state = not all_locked
        for rig in rigs:
            rig.hide_select = new_state
            plane = getattr(rig, 'fbp_plane_target', None)
            if plane:
                plane.hide_select = new_state
        return {'FINISHED'}


class FBP_OT_DeleteCollectionLayers(Operator):
    bl_idname      = "fbp.delete_collection_layers"
    bl_label       = "Delete Collection Layers"
    bl_description = "Delete all Frame by Plane layers inside this collection. The collection itself remains"
    bl_options     = {'UNDO'}

    collection_name: StringProperty(default="")

    def execute(self, context):
        coll = bpy.data.collections.get(self.collection_name)
        if not coll:
            return {'CANCELLED'}
        rigs = list(iter_fbp_rigs_in_collection(coll, True))
        deleted = delete_fbp_rigs(context, rigs)
        self.report({'INFO'}, f"Deleted {deleted} layer(s) from {coll.name}")
        return {'FINISHED'} if deleted else {'CANCELLED'}




class FBP_OT_RepairRenderState(Operator):
    bl_idname      = "fbp.repair_render_state"
    bl_label       = "Repair FBP Render State"
    bl_description = "Repair material slots, UVs and material indices before rendering"
    bl_options     = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sync_layer_collection(context)
        fixed = fbp_repair_all_render_state(context.scene, context.scene.frame_current)
        self.report({'INFO'}, f"Render state repaired on {fixed} FBP layer(s)")
        return {'FINISHED'}


class FBP_OT_BackgroundRenderFrames(Operator):
    bl_idname      = "fbp.background_render_frames"
    bl_label       = "Background Render FBP Frames"
    bl_description = "Render the animation frame by frame in a separate background Blender process, avoiding viewport crashes"
    bl_options     = {'REGISTER'}

    def execute(self, context):
        sc = context.scene
        if not bpy.data.is_saved:
            self.report({'WARNING'}, "Save the .blend file first")
            return {'CANCELLED'}

        start = int(sc.fbp_emergency_render_start) if sc.fbp_emergency_render_start > 0 else int(sc.frame_start)
        end = int(sc.fbp_emergency_render_end) if sc.fbp_emergency_render_end > 0 else int(sc.frame_end)
        if end < start:
            self.report({'WARNING'}, "End frame must be after Start frame")
            return {'CANCELLED'}

        out_dir = os.path.join(os.path.dirname(bpy.data.filepath), "FBP_Render_Frames")
        os.makedirs(out_dir, exist_ok=True)

        prefix = sc.fbp_emergency_render_prefix or "frame_"
        blend_path = bpy.data.filepath
        addon_path = os.path.abspath(__file__)
        blender_bin = bpy.app.binary_path

        # Repair and save before spawning the background instance.
        fbp_repair_all_render_state(sc, sc.frame_current)
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)

        script = f"""
import bpy, os, sys, traceback, importlib.util

ADDON_PATH = {addon_path!r}
OUT_DIR = {out_dir!r}
START = {start}
END = {end}
PREFIX = {prefix!r}

def try_register_addon():
    try:
        if ADDON_PATH and os.path.exists(ADDON_PATH):
            spec = importlib.util.spec_from_file_location("fbp_emergency_runtime", ADDON_PATH)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["fbp_emergency_runtime"] = mod
            spec.loader.exec_module(mod)
            try:
                mod.register()
            except Exception:
                pass
            return mod
    except Exception as exc:
        print("[FBP_BG] Addon register skipped:", exc)
    return None

mod = try_register_addon()

def is_rig(obj):
    try:
        return bool(getattr(obj, "is_fbp_control", False))
    except Exception:
        return False

def get_eval_mat_index_bg(rig, current_frame):
    try:
        images = rig.fbp_images
        if not images or len(images) == 0:
            return -1
        rel_frame = current_frame - int(rig.fbp_start_frame)
        if rel_frame < 0:
            return -1
        total_dur = sum(max(1, int(item.duration)) for item in images)
        if total_dur <= 0:
            return 0
        loop_mode = rig.fbp_loop_mode
        if loop_mode == 'NONE':
            if rel_frame >= total_dur:
                return len(images) - 1
        elif loop_mode == 'REPEAT':
            rel_frame = rel_frame % total_dur
        elif loop_mode == 'PINGPONG':
            if len(images) == 1:
                return 0
            mid_dur = sum(max(1, int(item.duration)) for item in images[1:-1])
            period = total_dur + mid_dur
            if period > 0:
                rel_frame = rel_frame % period
            if rel_frame >= total_dur:
                back_rel = rel_frame - total_dur
                acc = 0
                for j in range(len(images) - 2, 0, -1):
                    dur = max(1, int(images[j].duration))
                    if acc <= back_rel < acc + dur:
                        return j
                    acc += dur
                return 0
        acc = 0
        for i, item in enumerate(images):
            dur = max(1, int(item.duration))
            if acc <= rel_frame < acc + dur:
                return i
            acc += dur
        return len(images) - 1
    except Exception:
        return -1

def safe_empty_mat():
    mat = bpy.data.materials.get("FBP_BG_SAFE_EMPTY")
    if not mat:
        mat = bpy.data.materials.new("FBP_BG_SAFE_EMPTY")
    mat.use_nodes = True
    try:
        mat.diffuse_color = (0,0,0,0)
        if hasattr(mat, "blend_method"):
            mat.blend_method = 'BLEND'
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = 'BLENDED'
    except Exception:
        pass
    return mat

def repair_plane(rig, frame):
    plane = getattr(rig, "fbp_plane_target", None)
    if not plane or not getattr(plane, "data", None):
        return False
    mesh = plane.data
    mat = safe_empty_mat()
    target = max(len(getattr(rig, "fbp_images", [])), 1)
    while len(mesh.materials) < target:
        mesh.materials.append(mat)
    for i in range(len(mesh.materials)):
        if mesh.materials[i] is None:
            mesh.materials[i] = mat
    try:
        if not mesh.uv_layers:
            mesh.uv_layers.new(name="UVMap")
    except Exception:
        pass
    idx = get_eval_mat_index_bg(rig, frame)
    visible = bool(getattr(rig, "fbp_is_visible", True)) and idx >= 0
    if idx < 0:
        idx = 0
    idx = max(0, min(idx, len(mesh.materials) - 1))
    for poly in mesh.polygons:
        poly.material_index = idx
    try:
        mesh.update()
    except Exception:
        pass
    try:
        rig.hide_render = True
        plane.hide_render = not visible
    except Exception:
        pass
    return True

# Remove live addon handlers in the background process.
for handler_list in (
    bpy.app.handlers.frame_change_pre,
    bpy.app.handlers.frame_change_post,
    bpy.app.handlers.depsgraph_update_post,
    bpy.app.handlers.render_pre,
    bpy.app.handlers.render_post,
    bpy.app.handlers.render_cancel,
    bpy.app.handlers.render_complete,
):
    for h in list(handler_list):
        if getattr(h, "__name__", "").startswith("fbp_"):
            try:
                handler_list.remove(h)
            except Exception:
                pass

scene = bpy.context.scene
os.makedirs(OUT_DIR, exist_ok=True)

# Use existing render engine/settings. Output PNG sequence.
scene.render.image_settings.file_format = 'PNG'
if hasattr(scene.render, "use_lock_interface"):
    scene.render.use_lock_interface = True

rigs = [obj for obj in scene.objects if is_rig(obj)]
print(f"[FBP_BG] Rendering {{START}}-{{END}} with {{len(rigs)}} FBP rig(s)")
for frame in range(START, END + 1):
    try:
        scene.frame_set(frame)
        for rig in rigs:
            repair_plane(rig, frame)
        bpy.context.view_layer.update()
        scene.render.filepath = os.path.join(OUT_DIR, f"{{PREFIX}}{{frame:04d}}.png")
        print(f"[FBP_BG] Render frame {{frame}} -> {{scene.render.filepath}}")
        bpy.ops.render.render(write_still=True, animation=False)
    except Exception:
        traceback.print_exc()
        raise

print("[FBP_BG] DONE")
"""

        temp_dir = tempfile.mkdtemp(prefix="fbp_bg_render_")
        script_path = os.path.join(temp_dir, "fbp_background_render.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        cmd = [blender_bin, "-b", blend_path, "--python", script_path]
        try:
            self.report({'INFO'}, f"Background render started: {start}-{end}. Blender may freeze until it finishes.")
            result = subprocess.run(cmd, check=False)
            if result.returncode != 0:
                self.report({'ERROR'}, f"Background render failed with code {result.returncode}")
                return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Could not start background render: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Rendered frames to {out_dir}")
        return {'FINISHED'}



class FBP_OT_ProjectHealthCheck(Operator):
    bl_idname      = "fbp.project_health_check"
    bl_label       = "Project Health Check"
    bl_description = "Check linked images, collections and layers in the current Frame by Plane project"

    def execute(self, context):
        sync_layer_collection(context)
        rigs = [obj for obj in context.scene.objects if is_fbp_layer_object(obj)]
        fbp_colls = [coll for coll in bpy.data.collections if collection_has_fbp_content(coll, True)]
        image_paths = collect_project_image_paths()
        missing = missing_project_images()
        empty_fbp_colls = [coll.name for coll in fbp_colls if not any(True for _ in iter_fbp_rigs_in_collection(coll, True))]

        lines = [
            "Frame by Plane - Project Health",
            "================================",
            f"Layers: {len(rigs)}",
            f"Collections: {len(fbp_colls)}",
            f"Linked images: {len(image_paths)}",
            f"Missing images: {len(missing)}",
            f"Empty FBP collections: {len(empty_fbp_colls)}",
            "",
        ]
        if missing:
            lines.append("Missing files:")
            lines.extend(f"- {p}" for p in missing[:200])
            if len(missing) > 200:
                lines.append(f"...and {len(missing) - 200} more")
        else:
            lines.append("No missing files found.")

        txt = bpy.data.texts.get("FBP_Project_Health") or bpy.data.texts.new("FBP_Project_Health")
        txt.clear()
        txt.write("\n".join(lines))
        self.report({'INFO'}, f"Health: {len(rigs)} layers, {len(missing)} missing image(s)")
        return {'FINISHED'}


class FBP_OT_RelinkFromProjectRoot(Operator):
    bl_idname      = "fbp.relink_from_project_root"
    bl_label       = "Relink From Project Root"
    bl_description = "Relink missing images by searching inside the Project Folder"
    bl_options     = {'UNDO'}

    def execute(self, context):
        root = project_root_for_package(context)
        if not root or not os.path.isdir(root):
            self.report({'WARNING'}, "Set a valid Project Folder first")
            return {'CANCELLED'}
        relinked, ambiguous, still_missing = relink_missing_images_from_root(root, make_relative=True)
        msg = f"Relinked {relinked}; missing {len(still_missing)}; ambiguous {len(ambiguous)}"
        self.report({'INFO' if not still_missing else 'WARNING'}, msg)
        return {'FINISHED'}


class FBP_OT_OpenProjectFolder(Operator):
    bl_idname      = "fbp.open_project_folder"
    bl_label       = "Open Project Folder"
    bl_description = "Open the current Project Folder in the system file browser"

    def execute(self, context):
        root = project_root_for_package(context)
        if not root or not os.path.isdir(root):
            self.report({'WARNING'}, "Set a valid Project Folder first")
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=root)
        return {'FINISHED'}


class FBP_OT_SelectMissingLayers(Operator):
    bl_idname      = "fbp.select_missing_layers"
    bl_label       = "Select Missing Layers"
    bl_description = "Select Frame by Plane rigs that contain missing linked images"
    bl_options     = {'UNDO'}

    def execute(self, context):
        sync_layer_collection(context)
        bpy.ops.object.select_all(action='DESELECT')
        selected = 0
        skipped_hidden = 0
        active = None
        for rig in [obj for obj in context.scene.objects if getattr(obj, 'is_fbp_control', False)]:
            if not rig_has_missing_images(rig):
                continue
            if collection_is_hidden_in_view_layer(context, get_primary_fbp_collection(rig)):
                skipped_hidden += 1
                continue
            if not object_in_view_layer(rig, context):
                skipped_hidden += 1
                continue
            try:
                rig.select_set(True)
                active = rig
                selected += 1
            except Exception:
                skipped_hidden += 1
        if active:
            context.view_layer.objects.active = active
        level = 'WARNING' if skipped_hidden else 'INFO'
        self.report({level}, f"Selected {selected} missing layer(s); hidden/unavailable {skipped_hidden}")
        return {'FINISHED'} if selected or skipped_hidden else {'CANCELLED'}


class FBP_OT_SyncCollectionColors(Operator):
    bl_idname      = "fbp.sync_collection_colors"
    bl_label       = "Sync Collection Colors"
    bl_description = "Apply visible Collection color tags to Frame by Plane layer viewport colors"
    bl_options     = {'UNDO'}

    def execute(self, context):
        sync_collection_colors_to_rigs(context)
        self.report({'INFO'}, "Collection colors synced")
        return {'FINISHED'}


# ── FAST IMPORT / SCENE SPLIT ─────────────────────────────────────────────────

_FBP_FAST_IMPORT_DEPTH = 0
_FBP_FAST_IMPORT_STATE = {
    "undo": None,
    "view_shading": [],
    "queued_rigs": [],
    "profile": None,
}


def fbp_fast_import_is_active():
    return _FBP_FAST_IMPORT_DEPTH > 0


def fbp_fast_import_queue_rig(rig):
    if not rig:
        return
    if rig not in _FBP_FAST_IMPORT_STATE["queued_rigs"]:
        _FBP_FAST_IMPORT_STATE["queued_rigs"].append(rig)


def fbp_preserve_current_frame(context, func, *args, **kwargs):
    scene = context.scene if context else bpy.context.scene
    current_frame = None
    current_subframe = 0.0
    if scene:
        try:
            current_frame = int(scene.frame_current)
            current_subframe = float(getattr(scene, "frame_subframe", 0.0))
        except Exception:
            current_frame = None

    result = None
    error = None
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        error = exc
    finally:
        if scene and current_frame is not None:
            try:
                scene.frame_set(current_frame, subframe=current_subframe)
            except Exception:
                try:
                    scene.frame_current = current_frame
                except Exception:
                    pass
    if error:
        raise error
    return result


def fbp_current_profile():
    return _FBP_FAST_IMPORT_STATE.get("profile")


def fbp_profiled_section(label):
    return fbp_profiling.section(fbp_current_profile(), label)


def fbp_profile_wrap(func_name, label):
    func = globals().get(func_name)
    original_name = f"_FBP_PROFILE_ORIGINAL_{func_name}"
    if not callable(func) or original_name in globals():
        return
    globals()[original_name] = func

    def _wrapped(*args, **kwargs):
        with fbp_profiled_section(label):
            return globals()[original_name](*args, **kwargs)

    _wrapped.__name__ = getattr(func, "__name__", func_name)
    _wrapped.__doc__ = getattr(func, "__doc__", "")
    globals()[func_name] = _wrapped


def fbp_capture_viewport_state():
    saved = []
    wm = getattr(bpy.context, "window_manager", None)
    if not wm:
        return saved
    try:
        for window in wm.windows:
            screen = window.screen
            if not screen:
                continue
            for area in screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                for space in area.spaces:
                    if getattr(space, "type", None) == 'VIEW_3D':
                        saved.append((space, getattr(space.shading, "type", 'SOLID')))
    except Exception:
        pass
    return saved


def fbp_set_viewports_solid(saved):
    for space, _old in saved:
        try:
            space.shading.type = 'SOLID'
        except Exception:
            pass


def fbp_restore_viewport_state(saved):
    for space, old in saved:
        try:
            space.shading.type = old
        except Exception:
            pass


def fbp_begin_fast_import(context):
    global _FBP_FAST_IMPORT_DEPTH
    _FBP_FAST_IMPORT_DEPTH += 1
    if _FBP_FAST_IMPORT_DEPTH != 1:
        return

    _FBP_FAST_IMPORT_STATE["queued_rigs"].clear()
    _FBP_FAST_IMPORT_STATE["profile"] = fbp_profiling.begin_profile("Fast Import")

    with fbp_profiled_section("Prepare fast import"):
        prefs_edit = getattr(getattr(bpy.context, "preferences", None), "edit", None)
        if prefs_edit:
            try:
                _FBP_FAST_IMPORT_STATE["undo"] = prefs_edit.use_global_undo
                prefs_edit.use_global_undo = False
            except Exception:
                _FBP_FAST_IMPORT_STATE["undo"] = None

        saved = fbp_capture_viewport_state()
        _FBP_FAST_IMPORT_STATE["view_shading"] = saved
        fbp_set_viewports_solid(saved)

    try:
        bpy.context.window_manager.progress_begin(0, 100)
    except Exception:
        pass


def fbp_end_fast_import(context):
    global _FBP_FAST_IMPORT_DEPTH
    if _FBP_FAST_IMPORT_DEPTH <= 0:
        return
    _FBP_FAST_IMPORT_DEPTH -= 1
    if _FBP_FAST_IMPORT_DEPTH != 0:
        return

    scene = context.scene if context else bpy.context.scene
    current_frame = None
    current_subframe = 0.0
    if scene:
        try:
            current_frame = int(scene.frame_current)
            current_subframe = float(getattr(scene, "frame_subframe", 0.0))
        except Exception:
            current_frame = None

    with fbp_profiled_section("Finalize generated rigs"):
        seen = set()
        for rig in list(_FBP_FAST_IMPORT_STATE["queued_rigs"]):
            try:
                key = rig.as_pointer()
            except Exception:
                key = id(rig)
            if key in seen:
                continue
            seen.add(key)
            try:
                do_update_animation(rig)
                do_update_emission(rig)
                do_update_opacity(rig)
            except Exception:
                pass

    with fbp_profiled_section("Sync UI and collections"):
        try:
            sync_layer_collection(context)
            sync_collection_colors_to_rigs(context)
        except Exception:
            pass

    with fbp_profiled_section("Final view layer update"):
        try:
            if context and getattr(context, "view_layer", None):
                context.view_layer.update()
        except Exception:
            pass

    if scene and current_frame is not None:
        try:
            scene.frame_set(current_frame, subframe=current_subframe)
        except Exception:
            try:
                scene.frame_current = current_frame
            except Exception:
                pass

    fbp_restore_viewport_state(_FBP_FAST_IMPORT_STATE["view_shading"])
    _FBP_FAST_IMPORT_STATE["view_shading"] = []

    prefs_edit = getattr(getattr(bpy.context, "preferences", None), "edit", None)
    if prefs_edit and _FBP_FAST_IMPORT_STATE["undo"] is not None:
        try:
            prefs_edit.use_global_undo = _FBP_FAST_IMPORT_STATE["undo"]
        except Exception:
            pass
    _FBP_FAST_IMPORT_STATE["undo"] = None

    profile = _FBP_FAST_IMPORT_STATE.get("profile")
    if profile:
        try:
            fbp_profiling.finish_profile(profile)
            fbp_profiling.write_profile_text(bpy, profile)
            print(fbp_profiling.format_profile(profile))
        except Exception as exc:
            print(f"[FBP] Profile report error: {exc}")
    _FBP_FAST_IMPORT_STATE["profile"] = None
    _FBP_FAST_IMPORT_STATE["queued_rigs"].clear()

    try:
        bpy.context.window_manager.progress_update(100)
        bpy.context.window_manager.progress_end()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    except Exception:
        pass


def fbp_folder_has_images_recursive(path):
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if not is_hidden_import_name(d)]
            for filename in filenames:
                if not is_hidden_import_name(filename) and is_supported_image_file(filename) and not is_technical_map_file(filename):
                    return True
    except Exception:
        pass
    return False


def fbp_unique_scene_name(base_name):
    clean = clean_layer_name_from_path(base_name) or "Scene"
    clean = clean[:55]
    if clean not in bpy.data.scenes:
        return clean
    i = 2
    while f"{clean}.{i:03d}" in bpy.data.scenes:
        i += 1
    return f"{clean}.{i:03d}"


def fbp_apply_scene_defaults(scene):
    try:
        scene.fbp_pre_orientation = 'VERT'
    except Exception:
        pass
    try:
        scene.fbp_auto_scale = True
    except Exception:
        pass
    try:
        scene.fbp_cam_ratio = '4_3'
    except Exception:
        pass
    try:
        scene.render.resolution_x = 1920
        scene.render.resolution_y = 1440
    except Exception:
        pass
    try:
        scene.fbp_gen_camera = True
        scene.fbp_cam_pivot = True
    except Exception:
        pass


def fbp_auto_build_main_folders_as_scenes(operator, context):
    original_scene = context.scene
    original_window_scene = None
    try:
        original_window_scene = context.window.scene
    except Exception:
        pass

    base = bpy.path.abspath(getattr(original_scene, "fbp_project_path", "") or "")
    if not base or not os.path.isdir(base):
        operator.report({'ERROR'}, "Set a valid Project Folder first")
        return {'CANCELLED'}

    top_folders = []
    for name in sorted(os.listdir(base), key=natural_sort_key):
        if is_hidden_import_name(name):
            continue
        full = os.path.join(base, name)
        if os.path.isdir(full) and fbp_folder_has_images_recursive(full):
            top_folders.append((name, full))

    if not top_folders:
        operator.report({'WARNING'}, "No valid main folders found")
        return {'CANCELLED'}

    made = 0
    errors = []
    for name, full in top_folders:
        scene = bpy.data.scenes.new(fbp_unique_scene_name(name))
        made += 1

        try:
            scene.render.fps = original_scene.render.fps
            scene.frame_start = original_scene.frame_start
            scene.frame_end = original_scene.frame_end
        except Exception:
            pass

        fbp_apply_scene_defaults(scene)

        try:
            scene.fbp_project_path = full
            scene.fbp_parent_import_path = full
            scene.fbp_import_main_folders_as_scenes = False
        except Exception:
            pass

        try:
            context.window.scene = scene
        except Exception:
            errors.append(f"{name}: could not switch scene")
            continue

        try:
            result = _FBP_ORIGINAL_AUTO_SCENE_BUILDER_EXECUTE(operator, context)
            if 'CANCELLED' in result:
                errors.append(f"{name}: build cancelled")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    try:
        if original_window_scene:
            context.window.scene = original_window_scene
    except Exception:
        pass

    if errors:
        print("Frame by Plane scene split issues:")
        for err in errors:
            print(" -", err)
        operator.report({'WARNING'}, f"Created {made} scene(s), with {len(errors)} issue(s). Check console.")
    else:
        operator.report({'INFO'}, f"Created {made} scene(s) from main folders")
    return {'FINISHED'}


# Keep original operators and wrap them so Fast Import is always the default.
_FBP_ORIGINAL_AUTO_SCENE_BUILDER_EXECUTE = FBP_OT_AutoSceneBuilder.execute
_FBP_ORIGINAL_GENERATE_MULTIPLANE_EXECUTE = FBP_OT_GenerateMultiplane.execute
_FBP_ORIGINAL_IMPORT_SEQUENCE_EXECUTE = FBP_OT_ImportSequence.execute


def _fbp_auto_scene_builder_execute_fast(self, context):
    if getattr(context.scene, "fbp_import_main_folders_as_scenes", False):
        fbp_begin_fast_import(context)
        try:
            return fbp_auto_build_main_folders_as_scenes(self, context)
        finally:
            fbp_end_fast_import(context)

    fbp_begin_fast_import(context)
    try:
        return _FBP_ORIGINAL_AUTO_SCENE_BUILDER_EXECUTE(self, context)
    finally:
        fbp_end_fast_import(context)


def _fbp_generate_multiplane_execute_fast(self, context):
    fbp_begin_fast_import(context)
    try:
        return _FBP_ORIGINAL_GENERATE_MULTIPLANE_EXECUTE(self, context)
    finally:
        fbp_end_fast_import(context)


def _fbp_import_sequence_execute_fast(self, context):
    fbp_begin_fast_import(context)
    try:
        return _FBP_ORIGINAL_IMPORT_SEQUENCE_EXECUTE(self, context)
    finally:
        fbp_end_fast_import(context)


FBP_OT_AutoSceneBuilder.execute = _fbp_auto_scene_builder_execute_fast
FBP_OT_GenerateMultiplane.execute = _fbp_generate_multiplane_execute_fast
FBP_OT_ImportSequence.execute = _fbp_import_sequence_execute_fast

# Profile the heaviest reusable functions. This is intentionally conservative:
# the real behavior stays unchanged, but the last import creates FBP_Last_Import_Profile.
for _fbp_func_name, _fbp_label in (
    ("create_fbp_material", "Create materials / load images"),
    ("build_fbp_rig", "Build rig objects"),
    ("apply_fit_to_camera", "Fit to camera"),
    ("sync_layer_collection", "Sync layer collection"),
    ("sync_collection_colors_to_rigs", "Sync collection colors"),
    ("set_viewport_object_color", "Set viewport texture color"),
):
    fbp_profile_wrap(_fbp_func_name, _fbp_label)



class FBP_OT_ShowImportProfile(Operator):
    bl_idname      = "fbp.show_import_profile"
    bl_label       = "Show Import Profile"
    bl_description = "Open the last Frame by Plane import profiling report"

    def execute(self, context):
        txt = bpy.data.texts.get("FBP_Last_Import_Profile")
        if not txt:
            txt = bpy.data.texts.new("FBP_Last_Import_Profile")
            txt.write("No import profile yet. Run Auto Build Project or Generate Multi Plane first.")
        try:
            for area in context.screen.areas:
                if area.type == 'TEXT_EDITOR':
                    area.spaces.active.text = txt
                    break
        except Exception:
            pass
        self.report({'INFO'}, "Opened FBP_Last_Import_Profile")
        return {'FINISHED'}


# ── MENU / KEYMAP HELPERS ─────────────────────────────────────────────────────

def draw_fbp_image_add_menu(self, context):
    layout = self.layout
    layout.separator()
    layout.operator("fbp.import_sequence", text="Frame by Plane Sequence", icon='FILE_IMAGE')


def register_fbp_keymaps():
    wm = bpy.context.window_manager if bpy.context else None
    kc = wm.keyconfigs.addon if wm and wm.keyconfigs else None
    if not kc:
        return

    for keymap_name, space_type in [('Object Mode', None), ('3D View', 'VIEW_3D')]:
        km = kc.keymaps.new(name=keymap_name, space_type=space_type) if space_type else kc.keymaps.new(name=keymap_name)
        kmi = km.keymap_items.new("fbp.duplicate_or_default", type='D', value='PRESS', shift=True)
        addon_keymaps.append((km, kmi))

        kmi = km.keymap_items.new("fbp.delete_or_default", type='X', value='PRESS')
        addon_keymaps.append((km, kmi))

        kmi = km.keymap_items.new("fbp.delete_or_default", type='DEL', value='PRESS')
        addon_keymaps.append((km, kmi))


def unregister_fbp_keymaps():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()


def register_fbp_menus():
    menu_cls = getattr(bpy.types, "VIEW3D_MT_image_add", None)
    if menu_cls:
        menu_cls.append(draw_fbp_image_add_menu)
    else:
        bpy.types.VIEW3D_MT_add.append(draw_fbp_image_add_menu)


def unregister_fbp_menus():
    for menu_name in ("VIEW3D_MT_image_add", "VIEW3D_MT_add"):
        menu_cls = getattr(bpy.types, menu_name, None)
        if not menu_cls:
            continue
        try:
            menu_cls.remove(draw_fbp_image_add_menu)
        except Exception:
            pass


# ── REGISTRATION ──────────────────────────────────────────────────────────────

classes = (
    FBP_LayerItem,
    FBP_ImageItem,
    FBP_PendingPlaneItem,
    FBP_UL_ImageList,
    FBP_UL_PendingList,
    FBP_UL_LayerStack,
    FBP_PT_Settings,
    FBP_PT_LayerStack,
    FBP_PT_Sequence,
    FBP_PT_CreateFirst,
    FBP_PT_CreateExisting,
    FBP_OT_SaveFile,
    FBP_OT_OpenCreateRig,
    FBP_OT_SelectLayerExclusive,
    FBP_OT_DuplicateOrDefault,
    FBP_OT_MoveLayerStack,
    FBP_OT_ToggleSelectLayer,
    FBP_OT_ToggleSolo,
    FBP_OT_ToggleLock,
    FBP_OT_SelectAllLayers,
    FBP_OT_IsolateLayer,
    FBP_OT_FitToCamera,
    FBP_OT_MultiFitCamera,
    FBP_OT_SetCurrentFrame,
    FBP_OT_AddPendingPlane,
    FBP_OT_EditPendingPlane,
    FBP_OT_MovePendingPlane,
    FBP_OT_RemovePendingPlane,
    FBP_OT_ClearPendingPlanes,
    FBP_OT_AutoSceneBuilder,
    FBP_OT_GenerateMultiplane,
    FBP_OT_ImportFolderHierarchy,
    FBP_OT_ImportSequence,
    FBP_OT_ReplaceSequence,
    FBP_OT_UpdateAnimation,
    FBP_OT_InsertImagesAfterSelected,
    FBP_OT_LinkImageFrame,
    FBP_OT_ListAction,
    FBP_OT_BatchApply,
    FBP_OT_Transform,
    FBP_OT_UpdateEmission,
    FBP_OT_UpdateOpacity,
    FBP_OT_UpdateTrack,
    FBP_OT_SelectAll,
    FBP_OT_ReverseSequence,
    FBP_OT_DuplicateSelectedLayers,
    FBP_OT_DeleteSequence,
    FBP_OT_DeleteOrDefault,
    FBP_OT_ToggleCollectionCollapse,
    FBP_OT_SelectCollectionLayers,
    FBP_OT_ToggleCollectionVisibility,
    FBP_OT_ToggleCollectionLock,
    FBP_OT_DeleteCollectionLayers,
    FBP_OT_RepairRenderState,
    FBP_OT_BackgroundRenderFrames,
    FBP_OT_ProjectHealthCheck,
    FBP_OT_RelinkFromProjectRoot,
    FBP_OT_OpenProjectFolder,
    FBP_OT_SelectMissingLayers,
    FBP_OT_SyncCollectionColors,
    FBP_OT_ShowImportProfile,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    register_properties()

    if bpy.context and not bpy.app.timers.is_registered(sync_layer_collection_timer):
        bpy.app.timers.register(sync_layer_collection_timer, first_interval=0.05)

    if fbp_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(fbp_depsgraph_handler)

    # Remove possible duplicated legacy handlers before registering the safer one.
    for _handlers in (bpy.app.handlers.frame_change_pre, bpy.app.handlers.frame_change_post):
        for _h in list(_handlers):
            if getattr(_h, "__name__", "") == "fbp_frame_change_handler":
                try:
                    _handlers.remove(_h)
                except Exception:
                    pass
    bpy.app.handlers.frame_change_post.append(fbp_frame_change_handler)

    if fbp_render_guard_pre not in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.append(fbp_render_guard_pre)
    if fbp_render_guard_post not in bpy.app.handlers.render_post:
        bpy.app.handlers.render_post.append(fbp_render_guard_post)
    if fbp_render_guard_post not in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.append(fbp_render_guard_post)
    if fbp_render_guard_post not in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.append(fbp_render_guard_post)

    register_fbp_keymaps()
    register_fbp_menus()


def unregister():
    clear_previews()
    unregister_fbp_keymaps()
    unregister_fbp_menus()

    if fbp_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(fbp_depsgraph_handler)

    for _handlers in (bpy.app.handlers.frame_change_pre, bpy.app.handlers.frame_change_post):
        for _h in list(_handlers):
            if getattr(_h, "__name__", "") == "fbp_frame_change_handler":
                try:
                    _handlers.remove(_h)
                except Exception:
                    pass
    for _handlers in (bpy.app.handlers.render_pre, bpy.app.handlers.render_post, bpy.app.handlers.render_cancel, bpy.app.handlers.render_complete):
        for _h in list(_handlers):
            if getattr(_h, "__name__", "") in {"fbp_render_guard_pre", "fbp_render_guard_post"}:
                try:
                    _handlers.remove(_h)
                except Exception:
                    pass

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    props_scene = [
        "fbp_last_directory", "fbp_project_path", "fbp_cam_ratio",
        "fbp_show_previews", "fbp_use_hierarchical_layers", "fbp_auto_sort_layers_by_depth", "fbp_show_create_tools", "fbp_emergency_render_start", "fbp_emergency_render_end", "fbp_emergency_render_prefix",
        "fbp_auto_collection_color_variants", "fbp_layers", "fbp_layer_stack_index",
        "fbp_creation_mode", "fbp_pending_planes", "fbp_pending_planes_idx",
        "fbp_pre_duration", "fbp_pre_shadeless", "fbp_pre_loop_mode",
        "fbp_pre_interpolation", "fbp_pre_orientation",
        "fbp_gen_camera", "fbp_cam_pivot", "fbp_layer_offset", "fbp_auto_scale",
        "fbp_parent_import_path", "fbp_import_main_folders_as_scenes",
    ]
    for p in props_scene:
        if hasattr(bpy.types.Scene, p):
            delattr(bpy.types.Scene, p)

    if hasattr(bpy.types.Collection, "is_fbp_collection"):
        delattr(bpy.types.Collection, "is_fbp_collection")
    if hasattr(bpy.types.Collection, "fbp_collapsed"):
        delattr(bpy.types.Collection, "fbp_collapsed")

    props_object = [
        "is_fbp_control", "is_fbp_plane", "fbp_collection_name", "fbp_follow_collection_color",
        "fbp_color_variant_index", "fbp_base_scale", "fbp_base_scale_vec", "fbp_preview_path",
        "fbp_is_vertical", "fbp_images", "fbp_images_index", "fbp_color_tag",
        "fbp_depth_order", "fbp_loop_mode", "fbp_use_emission", "fbp_interpolation",
        "fbp_plane_target", "fbp_global_duration", "fbp_start_frame",
        "fbp_opacity", "fbp_track_cam", "fbp_is_visible", "fbp_cam_depth",
    ]
    for p in props_object:
        if hasattr(bpy.types.Object, p):
            delattr(bpy.types.Object, p)


if __name__ == "__main__":
    register()