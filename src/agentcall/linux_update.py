"""Linux worktree updates, dispatched outside the Edge systemd cgroup."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from .app_version import BUILD

INSTALL_HINT = "请先在 Pi 上运行一次 install.sh 的升级组件安装：bash scripts/linux/install.sh --update-components"
START_COMMAND = ["sudo", "-n", "systemctl", "start", "--no-block", "callpilot-update.service"]


class LinuxUpdater:
    def __init__(self, root: Path, *, runner: Callable = subprocess.run):
        self.root = root
        self.runner = runner
        self.lock = asyncio.Lock()
        self.cached: dict = {}
        self.checked = -float("inf")

    def run(self, args: list[str]) -> str:
        result = self.runner(
            args, cwd=self.root, capture_output=True, text=True, timeout=15, check=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return str(result.stdout).strip()

    async def unit_status(self) -> dict[str, str]:
        try:
            output = await asyncio.to_thread(self.run, [
                "systemctl", "show", "callpilot-update.service",
                "--property=LoadState,ActiveState,Result,ExecMainStatus,InactiveEnterTimestamp",
            ])
            return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        except (OSError, subprocess.SubprocessError):
            return {}

    def fetch_status(self) -> dict:
        status: dict = {"ahead": None, "latest": "", "error": ""}
        try:
            upstream = self.run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"])
            if not upstream.startswith("origin/"):
                raise ValueError("Expected an origin tracking branch")
            branch = upstream.removeprefix("origin/")
            self.run(["git", "fetch", "--", "origin", f"refs/heads/{branch}:refs/remotes/{upstream}"])
            status["ahead"] = int(self.run(["git", "rev-list", "--count", f"HEAD..{upstream}"]))
            status["latest"] = self.run(["git", "log", "-1", "--format=%s", upstream])
        except (OSError, subprocess.SubprocessError, ValueError):
            status["error"] = "无法检查更新（git fetch 失败或超时），请检查网络和 Git 上游配置。"
        return status

    async def status(self) -> dict:
        async with self.lock:
            if time.monotonic() - self.checked >= 180:
                self.cached = await asyncio.to_thread(self.fetch_status)
                self.checked = time.monotonic()
        try:
            sha = await asyncio.to_thread(self.run, ["git", "rev-parse", "--short", "HEAD"])
        except (OSError, subprocess.SubprocessError):
            sha = ""
        unit = await self.unit_status()
        return {**self.cached, "available": True, "build": BUILD, "sha": sha,
                "installed": unit.get("LoadState") == "loaded", "last_result": unit}

    async def start(self) -> bool:
        unit = await self.unit_status()
        if unit.get("LoadState") != "loaded":
            return False
        try:
            await asyncio.to_thread(self.run, START_COMMAND.copy())
            return True
        except (OSError, subprocess.SubprocessError):
            return False
