"""Shared constants for Frame by Plane.

This module is intentionally Blender-light: it avoids bpy imports so it can be
tested and refactored independently.
"""

STRIP_COLORS_DICT = {
    'COLOR_01': (0.8, 0.1, 0.1, 1.0),
    'COLOR_02': (0.9, 0.4, 0.1, 1.0),
    'COLOR_03': (0.8, 0.8, 0.1, 1.0),
    'COLOR_04': (0.2, 0.8, 0.2, 1.0),
    'COLOR_05': (0.1, 0.6, 0.8, 1.0),
    'COLOR_06': (0.4, 0.2, 0.8, 1.0),
    'COLOR_07': (0.8, 0.2, 0.5, 1.0),
    'COLOR_08': (0.4, 0.2, 0.1, 1.0),
    'COLOR_09': (0.5, 0.5, 0.5, 1.0),
}

COLOR_ENUM_ITEMS = [
    ('COLOR_01', "Red",     "", 'STRIP_COLOR_01', 1),
    ('COLOR_02', "Orange",  "", 'STRIP_COLOR_02', 2),
    ('COLOR_03', "Yellow",  "", 'STRIP_COLOR_03', 3),
    ('COLOR_04', "Green",   "", 'STRIP_COLOR_04', 4),
    ('COLOR_05', "Cyan",    "", 'STRIP_COLOR_05', 5),
    ('COLOR_06', "Purple",  "", 'STRIP_COLOR_06', 6),
    ('COLOR_07', "Magenta", "", 'STRIP_COLOR_07', 7),
    ('COLOR_08', "Brown",   "", 'STRIP_COLOR_08', 8),
    ('COLOR_09', "Gray",    "", 'STRIP_COLOR_09', 9),
]

preview_collections = {}
FBP_SUPPORTED_IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.exr', '.tif', '.tiff'}

FBP_TECHNICAL_MAP_SUFFIXES = (
    '_normal', '_norm', '_nrm', '_displace', '_disp', '_height',
    '_spec', '_specular', '_roughness', '_rough', '_metallic', '_metalness',
    '_ao', '_ambientocclusion', '_bump'
)

FBP_PROJECT_COLLECTION_PREFIX = 'FBP - '

FBP_SUPPORT_EMAIL = "pannoli312@gmail.com"
