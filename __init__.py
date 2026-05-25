bl_info = {
    "name": "Frame by Plane",
    "author": "Alessandro Pannoli",
    "version": (2, 20, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Frame by Plane",
    "description": "Import image sequences as controllable animation planes with folders, fast import, scene split and profiling.",
    "category": "Animation",
}

# Frame by Plane package entry point.
# blender_manifest.toml is used by Blender Extensions.
# bl_info is kept for legacy local add-on installation compatibility.

if "bpy" in locals():
    import importlib
    from . import constants
    from . import path_utils
    from . import profiling
    from . import core
    importlib.reload(constants)
    importlib.reload(path_utils)
    importlib.reload(profiling)
    importlib.reload(core)
else:
    from . import constants
    from . import path_utils
    from . import profiling
    from . import core


def register():
    core.register()


def unregister():
    core.unregister()
