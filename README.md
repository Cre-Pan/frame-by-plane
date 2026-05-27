# Frame by Plane v2.20.1

Frame by Plane imports 2D image sequences as controllable animation planes inside Blender.

## Main workflow

- Import image folders as layers.
- Organize layers in Blender Collections.
- Use vertical 2D multiplane setups by default.
- Fit planes to a 4:3 camera.
- Build larger projects as separate Blender Scenes.
- Render frame ranges through a clean background render launcher.

## Blender Extensions status

This package includes:

- `blender_manifest.toml`
- SPDX license declaration
- `LICENSE.txt`
- no third-party Python dependencies
- no bundled fonts or binary assets
- declared file-system permission

## Developer structure

```text
__init__.py
core.py
constants.py
path_utils.py
profiling.py
blender_manifest.toml
README.md
CHANGELOG.md
LICENSE.txt
```

`core.py` is still the main module. Refactor is intentionally gradual to avoid breaking the addon.
