bl_info = {
    "name": "Frame by Plane",
    "author": "Alessandro Pannoli",
    "version": (2, 20, 1),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > Frame by Plane",
    "description": "Import image sequences as controllable animation planes with folders, fast import, scene split and profiling.",
    "category": "Animation",
}

# Frame by Plane package entry point.
# blender_manifest.toml is used by Blender Extensions.
# bl_info is kept for legacy local add-on installation compatibility.

if "core" in locals():
    import importlib

    for _mod in (constants, path_utils, profiling, core):
        importlib.reload(_mod)
else:
    from . import constants
    from . import path_utils
    from . import profiling
    from . import core

modules = (
    constants,
    path_utils,
    profiling,
    core,
)


def register():
    for mod in modules:
        if hasattr(mod, "register"):
            mod.register()


def unregister():
    for mod in reversed(modules):
        if hasattr(mod, "unregister"):
            mod.unregister()
