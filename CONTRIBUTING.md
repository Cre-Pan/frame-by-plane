# Contributing

Thanks for helping improve Frame by Plane.

## Good first contributions

- Report bugs with clear reproduction steps
- Improve documentation
- Test the add-on on different operating systems
- Suggest workflow improvements for animation and multiplane scenes

## Development notes

This project is gradually being refactored.

Current structure:

- `__init__.py` add-on entry point
- `core.py` main add-on logic
- `constants.py` shared constants
- `path_utils.py` path and filename helpers
- `profiling.py` import timing report utilities

Please keep changes small and easy to test.
