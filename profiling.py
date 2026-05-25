"""Frame by Plane profiling utilities.

Small, dependency-free helper used by core.py.
It stores the last import timing report in a Blender Text datablock named:
FBP_Last_Import_Profile
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class ProfileSection:
    name: str
    seconds: float = 0.0
    calls: int = 0


@dataclass
class ImportProfile:
    label: str
    started_at: float = field(default_factory=time.perf_counter)
    ended_at: float | None = None
    sections: dict[str, ProfileSection] = field(default_factory=dict)

    @property
    def total_seconds(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return max(0.0, end - self.started_at)


def begin_profile(label: str = "Import") -> ImportProfile:
    return ImportProfile(label=label)


def finish_profile(profile: ImportProfile | None) -> None:
    if profile is not None and profile.ended_at is None:
        profile.ended_at = time.perf_counter()


@contextmanager
def section(profile: ImportProfile | None, name: str):
    if profile is None:
        yield
        return

    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = max(0.0, time.perf_counter() - start)
        item = profile.sections.get(name)
        if item is None:
            item = ProfileSection(name=name)
            profile.sections[name] = item
        item.seconds += elapsed
        item.calls += 1


def format_profile(profile: ImportProfile | None) -> str:
    if profile is None:
        return "Frame by Plane Import Profile\nNo profile available."

    total = profile.total_seconds
    lines = [
        "Frame by Plane - Last Import Profile",
        "====================================",
        f"Profile: {profile.label}",
        f"Total:   {total:.3f}s",
        "",
        "Sections:",
    ]

    if not profile.sections:
        lines.append("- No measured sections.")
        return "\n".join(lines)

    rows = sorted(profile.sections.values(), key=lambda item: item.seconds, reverse=True)
    for item in rows:
        percent = (item.seconds / total * 100.0) if total > 0 else 0.0
        lines.append(f"- {item.name}: {item.seconds:.3f}s | {item.calls} call(s) | {percent:.1f}%")

    lines.extend([
        "",
        "Notes:",
        "- Sections are cumulative. Nested sections may add up to more than Total.",
        "- The report is meant to identify bottlenecks, not to be a perfect profiler.",
    ])
    return "\n".join(lines)


def write_profile_text(bpy_module, profile: ImportProfile | None, text_name: str = "FBP_Last_Import_Profile"):
    text = format_profile(profile)
    txt = bpy_module.data.texts.get(text_name) or bpy_module.data.texts.new(text_name)
    txt.clear()
    txt.write(text)
    return txt
