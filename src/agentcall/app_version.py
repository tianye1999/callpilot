"""Process-start version snapshot; never reread a replaced bundle as live code."""

import json
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version() -> str:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    try:
        with (root / "pyproject.toml").open("rb") as stream:
            return str(tomllib.load(stream)["project"]["version"])
    except (OSError, ValueError, KeyError):
        try:
            return version("callpilot")
        except PackageNotFoundError:
            return "unknown"


VERSION = _version()
RELEASE_NOTES = json.loads(Path(__file__).with_name("release_notes.json").read_text(encoding="utf-8"))
