"""Server-only release checking and per-machine update preferences."""

from __future__ import annotations

import asyncio
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .app_version import VERSION

CHECK_INTERVAL = 24 * 60 * 60


def version_parts(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,9}){1,7}", value):
        return ()
    parts = tuple(int(part) for part in value.split("."))
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def is_newer(candidate: object, current: object) -> bool:
    new, old = version_parts(candidate), version_parts(current)
    return bool(new and old and new > old)


def https_url(value: object) -> bool:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        return False
    try:
        url = urlsplit(value)
        return bool(url.scheme == "https" and url.hostname and not url.username and not url.password
                    and (url.port is None or 0 < url.port <= 65535))
    except ValueError:
        return False


async def fetch_release(session, url: str) -> dict | None:
    if not https_url(url):
        return None
    try:
        # Total deadline includes DNS, TLS and body; redirects cannot downgrade to HTTP.
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=False) as response:
            if response.status != 200:
                return None
            data = await response.json()
        if not isinstance(data, dict):
            return None
        notes, published = data.get("notes"), data.get("publishedAt")
        if (data.get("product") != "physical-sim" or data.get("platform") != "macos"
                or not version_parts(data.get("version")) or not https_url(data.get("downloadUrl"))
                or not isinstance(published, (int, float)) or isinstance(published, bool)
                or not math.isfinite(published) or published <= 0
                or not isinstance(notes, dict)
                or any(not isinstance(notes.get(lang), str) or not notes[lang].strip() for lang in ("zh", "en"))):
            return None
        return {key: data[key] for key in ("product", "platform", "version", "publishedAt", "notes", "downloadUrl")}
    except (aiohttp.ClientError, OSError, ValueError, TypeError, OverflowError):
        return None


class UpdateChecker:
    def __init__(self, path: Path, *, version: str = VERSION):
        self.path, self.version = path, version
        self.latest: dict | None = None
        self.next_check = -float("inf")
        self.last_check = -float("inf")
        self.settings: dict[str, list[str]] = {"dismissed": [], "notified": []}
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            for key in self.settings:
                values = saved.get(key)
                if isinstance(values, list):
                    self.settings[key] = [v for v in values if isinstance(v, str) and version_parts(v)]
        except (OSError, ValueError, AttributeError):
            pass

    def _remember(self, key: str, version: str) -> None:
        values = list(dict.fromkeys([*self.settings[key], version]))
        updated = {**self.settings, key: values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(updated), encoding="utf-8")
        temporary.replace(self.path)
        self.settings = updated

    def dismiss(self, version: str) -> None:
        if self.latest and version == self.latest["version"]:
            self._remember("dismissed", version)

    def snapshot(self) -> dict | None:
        if not self.latest:
            return None
        return {**self.latest, "dismissed": self.latest["version"] in self.settings["dismissed"]}

    async def check(self, session, url: str, *, now: float) -> None:
        if now < self.next_check:
            return
        self.next_check = now + CHECK_INTERVAL
        self.last_check = now
        release = await fetch_release(session, url)
        self.latest = release if release and is_newer(release["version"], self.version) else None

    def request_check(self, *, now: float) -> None:
        if now - self.last_check >= 60:
            self.next_check = -float("inf")

    def notify(self, feed, *, busy: bool) -> None:
        if busy or not self.latest or self.latest["version"] in self.settings["notified"]:
            return
        kind = f"update:{self.latest['version']}"
        if any(event["kind"] == kind for event in feed.read(0)["events"]):
            return
        feed.info(kind, f"有新版本 {self.latest['version']}",
                  "打开 CallPilot 查看更新说明和下载。", target="#settings")

    def claim_notification(self, version: str, *, busy: bool) -> bool:
        if (busy or not self.latest or version != self.latest["version"]
                or version in self.settings["notified"]):
            return False
        try:
            # Native sender claims after permission check, immediately before submission.
            self._remember("notified", version)
        except OSError:
            return False
        return True


async def watch_updates(app):
    from . import config, platforms

    wake = asyncio.Event()
    app["update_wake"] = wake

    async def run():
        async with aiohttp.ClientSession() as session:
            while True:
                if wake.is_set():
                    wake.clear()
                    # Coalesce window + tray startup without an unbounded check endpoint.
                    app["updates"].request_check(now=asyncio.get_running_loop().time())
                if platforms.IS_MACOS and config.get_bool("UPDATE_CHECK_ENABLED"):
                    await app["updates"].check(session, config.get_str("UPDATE_CHECK_URL"),
                                               now=asyncio.get_running_loop().time())
                else:
                    app["updates"].latest = None
                try:
                    await asyncio.wait_for(wake.wait(), timeout=60)
                except TimeoutError:
                    pass

    task = asyncio.create_task(run())
    yield
    task.cancel()
    lease = app.get("upgrade_lease")
    if lease:
        lease["release"].set()
        await lease["task"]
    try:
        await task
    except asyncio.CancelledError:
        pass
