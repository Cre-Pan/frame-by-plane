"""Path and import-name helpers for Frame by Plane."""

from __future__ import annotations

import os
import re

try:
    from .constants import FBP_SUPPORTED_IMAGE_EXT, FBP_TECHNICAL_MAP_SUFFIXES
except Exception:
    from constants import FBP_SUPPORTED_IMAGE_EXT, FBP_TECHNICAL_MAP_SUFFIXES


def natural_sort_key(s):
    """Human sorting for filenames: A1, A2, A12 instead of A1, A12, A2."""
    name = os.path.basename(str(s))
    stem, ext = os.path.splitext(name)
    parts = re.split(r'(\d+)', stem.lower())
    key = [int(part) if part.isdigit() else part for part in parts]
    key.append(ext.lower())
    return key


def is_supported_image_file(name):
    return os.path.splitext(str(name))[1].lower() in FBP_SUPPORTED_IMAGE_EXT


def is_hidden_import_name(name):
    return os.path.basename(str(name)).startswith('_')


def is_technical_map_file(name):
    stem = os.path.splitext(os.path.basename(str(name)))[0].lower()
    return any(stem.endswith(suffix) for suffix in FBP_TECHNICAL_MAP_SUFFIXES)


def clean_layer_name_from_path(path):
    base = os.path.basename(str(path).rstrip(os.sep))
    stem, ext = os.path.splitext(base)
    return stem if ext else base


def ensure_folder(path):
    os.makedirs(path, exist_ok=True)
    return path
