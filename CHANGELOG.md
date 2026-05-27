# Changelog

## v2.20.1

- Fixed multi-file module reloading for add-on development reloads.
- Removed the advertising URL constant from the add-on UI/code path.
- Removed automatic keymap registration.
- Removed the depsgraph update handler.
- Simplified the background render script so it only configures output/range and renders.

## v2.20.0

- Added `blender_manifest.toml` for Blender Extensions.
- Replaced placeholder license with GPL-3.0-or-later license text.
- Split shared constants into `constants.py`.
- Split path/file helper functions into `path_utils.py`.
- Kept `profiling.py` from v2.19.
- Kept `core.py` as main module for stability.

## v2.19.0

- Added import profiling report.
- Added `FBP_Last_Import_Profile` text datablock.
- Added `Import Report` button.

## v2.18.0

- Fast import by default.
- Optional main folders as separate scenes.
- Default vertical import, 4:3 camera and Fit to Camera.
- Removed automatic timeline range resizing.
