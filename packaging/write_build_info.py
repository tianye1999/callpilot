"""Write an offline build identity before PyInstaller; never change release version."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def write_build_info(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short=7", "HEAD"], cwd=root,
        text=True, encoding="utf-8", timeout=2,
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("Invalid git SHA")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if run_id and (not run_id.isascii() or not run_id.isdecimal()):
        raise ValueError("Invalid CI run ID")
    suffix = "r" + run_id if run_id else datetime.now(timezone.utc).strftime("t%Y%m%dT%H%M%SZ")
    build = f"{version}+g{sha}.{suffix}"
    target = root / "build" / "app" / "build-info.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"build": build}) + "\n", encoding="utf-8")
    return build


if __name__ == "__main__":
    print(write_build_info(Path(__file__).resolve().parents[1]))
