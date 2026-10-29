"""菜单栏状态表：纯数据输入，不接触真实硬件或 Cocoa。"""

import pytest

from agentcall import macos_shell


def ready_meta():
    return {
        "hardware": {"usb_online": True, "modem_connected": True, "signal": {"level": "excellent"}},
        "sim": {"present": True, "network_attached": True, "registered": True, "carrier": "Example"},
        "credentials": {"ok": True}, "version": "0.6.0",
    }


@pytest.mark.parametrize("section,changes,elapsed,state,zh,en", [
    ("hardware", {"usb_online": False}, 0, "attention", "检查 USB", "Check the USB"),
    ("hardware", {"modem_connected": False}, 60, "transient", "正在连接模组", "Connecting to modem"),
    ("hardware", {"modem_connected": False}, 60.001, "attention", "未能连接模组", "Cannot connect to modem"),
    ("hardware", {"modem_resetting": True}, 0, "transient", "模组恢复中", "Modem recovering"),
    ("sim", {"present": False}, 0, "attention", "重新读卡", "Read SIM again"),
    ("sim", {"network_attached": False, "reg_status": "注册被拒", "eps_status": "注册被拒"},
     0, "attention", "卡被网络拒绝", "SIM rejected"),
    ("sim", {"network_attached": False, "reg_status": "注册被拒", "eps_status": "搜网中"},
     0, "transient", "正在搜网", "Searching for network"),
    ("credentials", {"ok": False}, 0, "attention", "模型 Key", "model key"),
    ("sim", {"network_attached": False}, 0, "transient", "正在搜网", "Searching for network"),
    ("sim", {}, 0, "ready", "可以接打电话", "Ready to make and receive calls"),
])
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_tray_status_branches(section, changes, elapsed, state, zh, en, lang):
    meta = ready_meta()
    meta[section].update(changes)
    result = macos_shell.tray_status(meta, uptime=100, connecting_for=elapsed, now=1000, lang=lang)
    assert result.state == state
    assert (zh if lang == "zh" else en) in result.title


@pytest.mark.parametrize("uptime,state", [(0, "transient"), (44.999, "transient"),
                                         (45, "transient"), (45.001, "attention")])
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_tray_status_unreachable_grace(uptime, state, lang):
    result = macos_shell.tray_status(None, uptime=uptime, now=1000, lang=lang)
    assert result.state == state
    assert result.detail == ""
    assert ("启动" if lang == "zh" else "Starting") in result.title if state == "transient" else (
        ("查看原因" if lang == "zh" else "check why") in result.title
    )


def test_tray_status_priorities_and_unknown_sim_during_connection():
    meta = ready_meta()
    meta["hardware"].update(modem_connected=False, modem_resetting=True)
    meta["sim"]["present"] = False
    assert macos_shell.tray_status(meta, uptime=100, now=1000).state == "transient"
    meta["credentials"]["ok"] = False
    assert macos_shell.tray_status(meta, uptime=100, now=1000).state == "attention"
    meta["call_active"] = True
    meta["call"] = {"number_suffix": "1234", "started_at": 917, "direction": "inbound"}
    for lang, expected in [("zh", "通话中 · 尾号 1234 · 01:23"), ("en", "In call · ending 1234 · 01:23")]:
        result = macos_shell.tray_status(meta, uptime=100, now=1000, lang=lang)
        assert result.state == "in_call" and result.title == expected
    meta["call"]["number_suffix"] = None
    assert macos_shell.tray_status(meta, uptime=100, now=1000).title == "通话中 · 01:23"


@pytest.mark.parametrize("section,changes", [
    ("hardware", {"usb_online": False}), ("sim", {"present": False}),
    ("sim", {"network_attached": False, "reg_status": "注册被拒", "eps_status": "注册被拒"}),
    ("credentials", {"ok": False}),
])
def test_known_attention_overrides_resetting_and_call_overrides_attention(section, changes):
    meta = ready_meta()
    meta["hardware"]["modem_resetting"] = True
    meta[section].update(changes)
    assert macos_shell.tray_status(meta, uptime=100, now=1000).state == "attention"
    meta["call_active"] = True
    assert macos_shell.tray_status(meta, uptime=100, now=1000).state == "in_call"


@pytest.mark.parametrize("level,zh,en", [("excellent", "很好", "Excellent"), ("good", "良好", "Good"),
                                     ("fair", "一般", "Fair"), ("weak", "弱", "Weak"),
                                     ("unknown", "未知", "Unknown")])
def test_tray_detail_signal_and_lte(level, zh, en):
    meta = ready_meta()
    meta["hardware"]["signal"]["level"] = level
    meta["sim"].update(registered=False, eps_registered=True)
    for lang, expected in [("zh", f"Example · 仅 LTE 已注册 · {zh} · 0.6.0"),
                           ("en", f"Example · LTE registered only · {en} · 0.6.0")]:
        assert macos_shell.tray_status(meta, uptime=100, now=1000, lang=lang).detail == expected


def test_tray_unknown_usb_is_transient_and_missing_details_are_omitted():
    meta = {"hardware": {"usb_online": None, "modem_connected": False}}
    result = macos_shell.tray_status(meta, uptime=100, now=1000)
    assert result.state == "transient" and result.detail == ""


@pytest.mark.parametrize("state,color", [("ready", (0.15, 0.75, 0.35)), ("in_call", (0.1, 0.55, 1.0)),
                                       ("transient", (1.0, 0.75, 0.1)), ("attention", (0.95, 0.2, 0.2))])
def test_tray_icon_color(state, color):
    assert macos_shell.tray_icon_color(state) == color


def test_bundled_version_uses_packaged_plist(tmp_path):
    import plistlib

    contents = tmp_path / "CallPilot.app" / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.2.3"}))
    assert macos_shell.bundled_version(str(contents / "MacOS" / "CallPilot")) == "1.2.3"
    assert macos_shell.bundled_version(str(tmp_path / "python")) == ""


def test_call_clock_resets_without_recording(monkeypatch):
    import threading

    from agentcall import call_agent

    session = object.__new__(call_agent.CallSession)
    session._active_lock = threading.Lock()
    session._active = False
    session.call_started_at = None
    now = [100.0]
    monkeypatch.setattr(call_agent.time, "time", lambda: now[0])
    session._set_active(True)
    assert session.call_started_at == 100
    now[0] = 110
    session._set_active(True)
    assert session.call_started_at == 100
    session._set_active(False)
    session._set_active(True)
    assert session.call_started_at == 110
