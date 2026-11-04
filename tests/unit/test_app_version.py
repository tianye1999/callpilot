import json
import subprocess

import pytest

from agentcall import app_version


@pytest.mark.parametrize("payload", [None, "broken", "[]", '{"build":42}', '{"build":"old+g1234567.r1"}'])
def test_frozen_build_info_failure_is_silent(monkeypatch, tmp_path, payload):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    if payload is not None:
        (tmp_path / "build-info.json").write_text(payload, encoding="utf-8")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: pytest.fail("Frozen app must not run git"))
    assert app_version._build() == app_version.VERSION


def test_frozen_build_info(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    build = app_version.VERSION + "+g96e1622.r41839780534"
    (tmp_path / "build-info.json").write_text(json.dumps({"build": build}), encoding="utf-8")
    assert app_version._build() == build


@pytest.mark.parametrize("sha", ["96e1622", None])
def test_source_build_uses_local_git_or_dev(monkeypatch, sha):
    monkeypatch.delattr("sys.frozen", raising=False)
    def git(*args, **kwargs):
        if sha is None:
            raise OSError("git unavailable")
        assert kwargs["timeout"] <= 2
        return sha + "\n"
    monkeypatch.setattr(subprocess, "check_output", git)
    assert app_version._build() == app_version.VERSION + ("+g" + sha + ".dev" if sha else "+dev")


def test_source_git_timeout_is_silent(monkeypatch):
    monkeypatch.delattr("sys.frozen", raising=False)
    def git(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 2)
    monkeypatch.setattr(subprocess, "check_output", git)
    assert app_version._build() == app_version.VERSION + "+dev"


@pytest.mark.parametrize("run_id", ["41839780534", ""])
def test_build_writer_overwrites_identity_offline(monkeypatch, tmp_path, run_id):
    import re
    import runpy
    from pathlib import Path

    write = runpy.run_path(str(Path(__file__).resolve().parents[2] / "packaging/write_build_info.py"))[
        "write_build_info"]
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "96e1622\n")
    project = '[project]\nversion="26.10.30"\n'
    (tmp_path / "pyproject.toml").write_text(project, encoding="utf-8")
    path = tmp_path / "build/app/build-info.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"build":"stale"}', encoding="utf-8")
    build = write(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"build": build}
    if run_id:
        assert build == "26.10.30+g96e1622.r41839780534"
    else:
        assert re.fullmatch(r"26\.10\.30\+g96e1622\.t[0-9]{8}T[0-9]{6}Z", build)
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == project
