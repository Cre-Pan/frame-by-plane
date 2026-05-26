# Changelog

## 1.5.1

### Changed

- Renamed the extension from **Blender Slides PRO** to **Slides Pro** to comply with Blender Extensions naming rules.
- Updated the extension description to explain that the add-on is a PowerPoint-style presentation manager for Blender scenes.
- Replaced Gumroad as the public support channel with GitHub Issues.
- Moved the general product description out of the version history and into the proper description/README area.

### Fixed

- Removed startup registration of the notes draw handler.
- Presentation handlers now start only when the user manually starts a presentation.
- Presentation handlers are removed when playback is stopped or when the add-on is disabled.

## 1.5.0

### Added

- Slide list management from the 3D View sidebar.
- Per-slide frame ranges.
- Per-slide camera assignment.
- Slide notes.
- Checkpoints that pause playback at specific frames.
- Smart next/previous navigation.
- Hard-skip navigation without transitions.
- Projection window for clean presentation playback.
- Keyboard shortcuts for slide navigation.
