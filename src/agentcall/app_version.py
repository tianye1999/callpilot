"""Process-start version snapshot; never reread a replaced bundle as live code."""

import json
import re
import subprocess
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


def _build() -> str:
    if getattr(sys, "frozen", False):
        try:
            root = Path(sys._MEIPASS)  # type: ignore[attr-defined]
            data = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
            build = data.get("build") if isinstance(data, dict) else None
            if isinstance(build, str) and re.fullmatch(
                re.escape(VERSION) + r"\+g[0-9a-f]{7,40}\.(?:r[0-9]+|t[0-9]{8}T[0-9]{6}Z)", build
            ):
                return build
        except (OSError, ValueError, AttributeError):
            pass
        return VERSION
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"], cwd=Path(__file__).resolve().parents[2],
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", timeout=2,
        ).strip()
        if re.fullmatch(r"[0-9a-f]{7,40}", sha):
            return f"{VERSION}+g{sha}.dev"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return f"{VERSION}+dev"


BUILD = _build()
RELEASE_NOTES = json.loads(Path(__file__).with_name("release_notes.json").read_text(encoding="utf-8"))
