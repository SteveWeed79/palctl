"""
Named server profiles — the foundation for managing more than one server.

palctl manages a single server today. This stores additional named Config
snapshots under ``config_dir()/profiles/<name>.json`` and tracks which one is
active, giving a future multi-server daemon a clean, tested model to build on.
Nothing here changes how the single-server daemon runs right now — it's the
groundwork for the fleet feature (the natural open-core line), not the feature
itself.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from .config import Config, config_dir


def _safe(name: str) -> str:
    """A filesystem-safe profile name — no traversal, no separators."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", name.strip())
    if not cleaned:
        raise ValueError("Profile name must contain a letter, digit, - or _.")
    return cleaned


def _profiles_dir() -> Path:
    d = config_dir() / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _active_pointer() -> Path:
    return config_dir() / "active_profile"


def list_profiles() -> list[str]:
    return sorted(p.stem for p in _profiles_dir().glob("*.json"))


def save_profile(name: str, cfg: Config) -> None:
    path = _profiles_dir() / f"{_safe(name)}.json"
    path.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")


def load_profile(name: str) -> Config:
    path = _profiles_dir() / f"{_safe(name)}.json"
    return Config.from_dict(json.loads(path.read_text(encoding="utf-8")))


def delete_profile(name: str) -> None:
    (_profiles_dir() / f"{_safe(name)}.json").unlink(missing_ok=True)


def set_active(name: str) -> None:
    _active_pointer().write_text(_safe(name), encoding="utf-8")


def active_profile() -> str | None:
    try:
        return _active_pointer().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
