# Frame by Plane

Frame by Plane is a Blender add-on for importing image sequences as controllable 2D planes in 3D space.

It is designed for animation, multiplane setups, painted backgrounds, parallax scenes and stop-motion style workflows.

## Features

- Import folders of images as animated planes
- Organize layers using Blender Collections
- Build vertical multiplane scenes by default
- Fit planes automatically to a 4:3 camera
- Split main project folders into separate Blender Scenes
- Skip hidden folders and files starting with `_`
- Fast import mode for larger projects
- Background frame rendering helper
- Import profiling report for debugging slow projects

## Requirements

- Blender 5.1 or newer

## Installation

Download the latest release ZIP and install it from Blender:

```text
Edit > Preferences > Add-ons > Install...
```

Then enable **Frame by Plane**.

## Development install

Clone this repository into Blender's add-ons folder, or install the repository ZIP from Blender.

## Project structure

```text
__init__.py                 Add-on entry point
core.py                     Main add-on logic
constants.py                Shared constants
path_utils.py               Path and filename helpers
profiling.py                Import profiling utilities
blender_manifest.toml       Blender Extensions manifest
```

## License

GPL-3.0-or-later.
