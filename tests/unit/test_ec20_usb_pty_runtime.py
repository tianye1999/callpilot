"""Runtime helper tests for the bundled EC20 USB bridge."""

from __future__ import annotations

import pytest

pytest.importorskip("fcntl", reason="EC20 PTY bridge is POSIX-only")

from scripts import ec20_usb_pty


@pytest.fixture(autouse=True)
def isolate_recovery_request(monkeypatch, tmp_path):
    """自动识别测试不能清除真实桥正在等待的恢复哨兵。"""
    from dataclasses import replace

    hardware = ec20_usb_pty.modem_hardware
    request = str(tmp_path / "recover-request")
    monkeypatch.setattr(hardware, "RECOVERY_REQUEST_PATH", request)
    monkeypatch.setattr(hardware, "PRESETS", tuple(
        replace(p, recovery_request_path=request) if p.recovery_request_path else p for p in hardware.PRESETS
    ))


def _flush_rescue_pty(fd):
    """救援口可能已被子进程关闭 master 挂断；清理不能覆盖原始测试结果。"""
    import errno
    import termios

    try:
        termios.tcflush(fd, termios.TCIOFLUSH)
    except (OSError, termios.error) as exc:
        if exc.args[0] not in (errno.EIO, errno.EBADF):
            raise


@pytest.mark.parametrize("error", [ec20_usb_pty.termios.error, OSError])
@pytest.mark.parametrize("code", [ec20_usb_pty.errno.EIO, ec20_usb_pty.errno.EBADF])
def test_rescue_flush_tolerates_closed_pty(monkeypatch, error, code):
    def flush(fd, mode):
        raise error(code, "closed PTY")

    monkeypatch.setattr(ec20_usb_pty.termios, "tcflush", flush)
    _flush_rescue_pty(123)


def test_real_pty_flush_before_close_and_linux_hangup():
    """两端仍打开时可正常 flush；Linux master 关闭后 slave 的 EIO 仅属于救援路径。"""
    import errno
    import os
    import sys
    import termios

    master, slave = os.openpty()
    try:
        termios.tcflush(master, termios.TCIOFLUSH)
        termios.tcflush(slave, termios.TCIOFLUSH)
    finally:
        os.close(master)
        try:
            if sys.platform == "linux":
                with pytest.raises(termios.error) as caught:
                    termios.tcflush(slave, termios.TCIOFLUSH)
                assert caught.value.args[0] == errno.EIO
            _flush_rescue_pty(slave)
        finally:
            os.close(slave)


@pytest.fixture
def real_pty_usb_block(monkeypatch, tmp_path):
    """仅替换 USB，用真实 PTY 和大数据块覆盖缓冲满及部分写入。"""
    import os
    import threading
    import time

    payload = bytes(range(256)) * 256
    started = threading.Event()
    completed = threading.Event()
    timings = []

    class USB:
        reads = 0

        def read(self, *args, **kwargs):
            self.reads += 1
            if self.reads == 1:
                timings.append(time.monotonic())
                started.set()
                return payload
            if self.reads == 2:
                timings.append(time.monotonic())
                completed.set()
            time.sleep(0.01)
            raise ec20_usb_pty.usb.core.USBTimeoutError("test idle")

        def write(self, *args, **kwargs):
            raise AssertionError("测试不向 slave 写数据")

    monkeypatch.setattr(ec20_usb_pty.usb.util, "claim_interface", lambda *args: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "release_interface", lambda *args: None)
    handle = ec20_usb_pty.bridge_port(
        USB(), ec20_usb_pty.UsbPort(4, 0x81, 0x01, len(payload)), str(tmp_path / "pty"),
    )
    os.set_blocking(handle.slave_fd, False)
    try:
        assert started.wait(1)
        yield handle, payload, completed, timings
    finally:
        handle.close()


def test_real_pty_delayed_reader_receives_complete_block(real_pty_usb_block, caplog):
    import os
    import select
    import time

    handle, payload, completed, timings = real_pty_usb_block
    time.sleep(0.2)
    received = bytearray()
    deadline = time.monotonic() + 1
    while len(received) < len(payload) and time.monotonic() < deadline:
        if select.select([handle.slave_fd], [], [], 0.02)[0]:
            received.extend(os.read(handle.slave_fd, 65536))
        elif completed.is_set():
            break
    assert bytes(received) == payload, f"只收到 {len(received)}/{len(payload)} 字节"
    assert completed.wait(1)
    assert timings[1] - timings[0] >= 0.18
    assert "丢弃" not in caplog.text
    assert handle.stats.snapshot()["total"]["in_bytes"] == len(payload)
    assert handle.stats.snapshot()["total"]["pty_dropped"] == 0


def test_real_pty_without_reader_drops_block_after_bounded_wait(real_pty_usb_block, caplog):
    handle, _payload, completed, timings = real_pty_usb_block
    assert completed.wait(1.5), "无读者时写线程未按时放弃本块"
    elapsed = timings[1] - timings[0]
    assert 0.45 <= elapsed < 1.0, f"应等待约 0.5 秒再丢弃，实际 {elapsed:.3f} 秒"
    assert not handle.stop.is_set()
    assert "丢弃" in caplog.text
    assert handle.stats.snapshot()["total"]["in_bytes"] == len(_payload)
    assert 0 < handle.stats.snapshot()["total"]["pty_dropped"] <= len(_payload)


def test_real_pty_stop_interrupts_backpressure_wait(real_pty_usb_block, caplog):
    import time

    handle, _payload, completed, _timings = real_pty_usb_block
    time.sleep(0.05)
    assert not completed.is_set()
    started = time.monotonic()
    handle.close()
    assert time.monotonic() - started < 0.3
    assert all(not thread.is_alive() for thread in handle.threads)
    assert "丢弃" not in caplog.text


def test_partial_writes_share_one_deadline(monkeypatch):
    import threading

    clock = [0.0]
    received = bytearray()
    writes = [0]
    payload = bytes(range(100))

    def write(fd, data):
        writes[0] += 1
        if writes[0] % 2 == 0:
            raise BlockingIOError()
        received.extend(data[:2])
        return 2

    def select(read, write, error, timeout):
        assert (read, write, error) == ([], [123], [])
        assert 0 < timeout <= 0.05
        clock[0] += timeout
        return [], [123], []

    monkeypatch.setattr(ec20_usb_pty.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ec20_usb_pty.os, "write", write)
    monkeypatch.setattr(ec20_usb_pty.select, "select", select)
    dropped = ec20_usb_pty.write_pty_bounded(123, payload, threading.Event())
    assert 0 < dropped < len(payload)
    assert received == payload[:len(received)]
    assert dropped + len(received) == len(payload)
    assert clock[0] == pytest.approx(ec20_usb_pty.USB_TO_PTY_WRITE_TIMEOUT)


def test_close_joins_then_flushes_before_closing(monkeypatch):
    import threading
    from types import SimpleNamespace

    calls = []
    thread = SimpleNamespace(join=lambda timeout: calls.append("join"), is_alive=lambda: False)
    handle = ec20_usb_pty.BridgeHandle(
        None, ec20_usb_pty.UsbPort(2, 1, 2, 512), "/nonexistent/test-link", 10, 11,
        threading.Event(), threads=[thread],
    )
    monkeypatch.setattr(ec20_usb_pty.termios, "tcflush", lambda fd, mode: calls.append(("flush", fd)))
    monkeypatch.setattr(ec20_usb_pty.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(ec20_usb_pty.usb.util, "release_interface", lambda *args: calls.append("release"))
    handle.close()
    assert calls == ["join", ("flush", 10), ("flush", 11), "release", ("close", 10), ("close", 11)]
    assert handle.stop.is_set()
    handle.close()
    assert len(calls) == 6


@pytest.mark.parametrize("code", [ec20_usb_pty.errno.EIO, ec20_usb_pty.errno.EINVAL])
def test_close_refuses_unexpected_flush_failure(monkeypatch, code):
    import threading

    handle = ec20_usb_pty.BridgeHandle(
        None, ec20_usb_pty.UsbPort(2, 1, 2, 512), "/nonexistent/test-link", 10, 11, threading.Event(),
    )

    def flush(fd, mode):
        raise ec20_usb_pty.termios.error(code, "unexpected flush failure")

    monkeypatch.setattr(ec20_usb_pty.termios, "tcflush", flush)
    monkeypatch.setattr(ec20_usb_pty.os, "close", lambda fd: pytest.fail("flush 失败时不得冒险关闭"))
    with pytest.raises(ec20_usb_pty.BridgeShutdownError):
        handle.close()
    assert not handle.closed


def test_shutdown_timeout_stops_all_bridges_and_is_fatal(monkeypatch):
    import threading
    from types import SimpleNamespace

    stops = [threading.Event(), threading.Event()]
    closed = []

    def close(index):
        assert all(stop.is_set() for stop in stops)
        closed.append(index)
        if index == 0:
            raise ec20_usb_pty.BridgeShutdownError("timeout")

    handles = iter([SimpleNamespace(stop=stops[i], degraded=False, close=lambda i=i: close(i)) for i in range(2)])
    monkeypatch.setattr(ec20_usb_pty, "discover_ports", lambda dev: {2: None, 4: None})
    monkeypatch.setattr(ec20_usb_pty, "bridge_port", lambda *args, **kwargs: next(handles))
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", lambda dev: pytest.fail("不能释放活动 USB"))
    stop = threading.Event()
    stop.set()
    with pytest.raises(ec20_usb_pty.BridgeShutdownError):
        ec20_usb_pty.run_bridges_once(None, [(2, "/a"), (4, "/b")], stop)
    assert closed == [0, 1]
    assert not issubclass(ec20_usb_pty.BridgeShutdownError, RuntimeError)


@pytest.mark.parametrize("interface", [2, 4], ids=["at", "pcm"])
def test_full_real_pty_keeps_usb_reader_running_and_closes_within_two_seconds(tmp_path, interface):
    """无人读 slave 时不能堵住 USB 线程；父进程保留救援口，防止旧实现卡死测试。"""
    import json
    import os
    import subprocess
    import sys
    import textwrap
    import time
    from pathlib import Path

    master, slave = os.openpty()
    ec20_usb_pty.make_raw(slave)
    os.set_blocking(slave, False)
    script = textwrap.dedent("""
        import json
        import sys
        import threading
        import time
        from scripts import ec20_usb_pty as bridge

        master, slave, interface = map(int, sys.argv[1:4])
        bridge.pty.openpty = lambda: (master, slave)
        bridge.usb.util.claim_interface = lambda *args: None
        bridge.usb.util.release_interface = lambda *args: None
        drained = threading.Event()

        class USB:
            reads = 0

            def read(self, *args, **kwargs):
                time.sleep(0.001)
                self.reads += 1
                # 每块大于 PTY 容量；两轮背压后仍能继续读取，而非要求无限丢帧的吞吐量。
                if self.reads >= 3:
                    drained.set()
                return b'x' * 65536

            def write(self, *args, **kwargs):
                raise AssertionError('slave has no writer')

        dev = USB()
        handle = bridge.bridge_port(dev, bridge.UsbPort(interface, 0x81, 0x01, 512), sys.argv[4])
        progressed = drained.wait(2)
        print(json.dumps({'progressed': progressed, 'reads': dev.reads}), flush=True)
        started = time.monotonic()
        handle.close()
        print(json.dumps({'close_seconds': time.monotonic() - started}), flush=True)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(master), str(slave), str(interface), str(tmp_path / "pty")],
        cwd=Path(ec20_usb_pty.__file__).resolve().parent.parent,
        pass_fds=(master, slave), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # 父进程不能持有 master，否则子进程 close 不会触发最后一次 tty 关闭。
    os.close(master)
    timed_out = False
    try:
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            timed_out = True
            # 只在被测流程超时之后救援，不能提前排空从而掩盖回归。
            deadline = time.monotonic() + 3
            while proc.poll() is None and time.monotonic() < deadline:
                _flush_rescue_pty(slave)
                try:
                    os.read(slave, 65536)
                except OSError:
                    pass
                time.sleep(0.01)
            if proc.poll() is None:
                proc.kill()
            stdout, stderr = proc.communicate(timeout=3)
    finally:
        try:
            _flush_rescue_pty(slave)
        finally:
            os.close(slave)
    observations = [json.loads(line) for line in stdout.splitlines()]
    assert not timed_out, f"PTY close blocked; rescued child: {observations}; {stderr}"
    assert proc.returncode == 0, stderr
    assert observations[0]["progressed"], f"USB reader blocked on full PTY: {observations}"
    assert observations[1]["close_seconds"] < 2, observations


def test_auto_bridge_reselects_after_unplug(monkeypatch, tmp_path):
    import threading

    from agentcall import modem_hardware as hardware

    monkeypatch.setattr(hardware, "STATE_PATH", tmp_path / "state.json")
    stop = threading.Event()
    devices = iter([(object(), hardware.PRESETS[0]), (object(), hardware.PRESETS[1])])
    monkeypatch.setattr(ec20_usb_pty, "find_auto_device", lambda: next(devices))
    seen = []
    def bridge(dev, maps, stop, **kwargs):
        kwargs["heartbeat"]()
        seen.append(hardware.read_state()["vendor"])
        if len(seen) == 2:
            assert kwargs["reset_first"] is False
            assert str(kwargs["recovery_request"]) == hardware.RECOVERY_REQUEST_PATH
            stop.set()
        return False
    monkeypatch.setattr(ec20_usb_pty, "run_bridges_once", bridge)
    assert ec20_usb_pty.run_auto(stop, poll_seconds=0) == 0
    assert seen == ["quectel", "simcom"]
    assert hardware.read_state() is None


def test_auto_waits_without_device_and_does_not_reset_normal_reconnect(monkeypatch, tmp_path):
    import threading

    from agentcall import modem_hardware as hardware

    monkeypatch.setattr(hardware, "STATE_PATH", tmp_path / "state.json")
    stop = threading.Event()
    devices = iter([None, (object(), hardware.PRESETS[1]), None, (object(), hardware.PRESETS[1])])
    monkeypatch.setattr(ec20_usb_pty, "find_auto_device", lambda: next(devices))
    clock = [100.0]
    monkeypatch.setattr(ec20_usb_pty.time, "monotonic", lambda: clock[0])
    resets = []
    def bridge(dev, maps, stop, **kwargs):
        resets.append(kwargs["reset_first"])
        clock[0] += 10
        if len(resets) == 2:
            stop.set()
        return False
    monkeypatch.setattr(ec20_usb_pty, "run_bridges_once", bridge)
    assert ec20_usb_pty.run_auto(stop, poll_seconds=0) == 0
    assert resets == [False, False]


def test_explicit_bridge_cli_keeps_maps_and_defaults(monkeypatch):
    seen = []
    monkeypatch.setattr(ec20_usb_pty.sys, "argv", ["bridge", "--vid", "0x1234", "--pid", "0x5678",
                                                "--map", "2:/tmp/test-at", "--once"])
    monkeypatch.setattr(ec20_usb_pty, "acquire_instance_lock", lambda: object())
    monkeypatch.setattr(ec20_usb_pty.signal, "signal", lambda *args: None)
    def wait(stop, **kwargs):
        assert kwargs == {"vid": 0x1234, "pid": 0x5678}
        return object()
    monkeypatch.setattr(ec20_usb_pty, "wait_for_device", wait)
    monkeypatch.setattr(ec20_usb_pty, "run_bridges_once",
                        lambda dev, maps, stop, **kw: seen.append((maps, kw)) or False)
    assert ec20_usb_pty.main() == 0
    assert seen == [([(2, "/tmp/test-at")], {"reset_first": False, "recovery_request": None})]


@pytest.mark.parametrize("script", ["ec20_usb_pty.py", "sim7600_usb_pty.py"])
def test_bridge_standalone_help(script, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    path = Path(ec20_usb_pty.__file__).with_name(script)
    result = subprocess.run([sys.executable, "-I", str(path), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--auto" in result.stdout and "--map" in result.stdout


def test_bundled_libusb_path_uses_pyinstaller_resources(tmp_path, monkeypatch):
    lib = tmp_path / "lib" / "libusb-1.0.0.dylib"
    lib.parent.mkdir()
    lib.write_bytes(b"placeholder")

    monkeypatch.setattr(ec20_usb_pty.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert ec20_usb_pty.bundled_libusb_path() == lib


def test_bundled_libusb_path_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ec20_usb_pty.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert ec20_usb_pty.bundled_libusb_path() is None


def test_sim7600_wrapper_defaults_match_phase0_probe():
    """SIM7600 桥的 VID/PID 与接口映射来自 Phase 0 真机探测，定死防漂移。"""
    from scripts import sim7600_usb_pty

    assert sim7600_usb_pty.SIM7600_VID == 0x1E0E
    assert sim7600_usb_pty.SIM7600_PID == 0x9001
    # interface 2 = AT 口，interface 4 = PCM 音频口（8kHz/16-bit/mono）。
    assert sim7600_usb_pty.DEFAULT_MAPS == ["2:/tmp/sim7600-at", "4:/tmp/sim7600-pcm"]


def test_ec20_bridge_defaults_unchanged():
    """回归护栏：Quectel 桥默认 VID/PID 不因 SIM7600 参数化而改动。"""
    assert ec20_usb_pty.VID == 0x2C7C
    assert ec20_usb_pty.PID == 0x0125


# ---- PCM 卡死自动恢复（WIL-109）----

def test_degraded_flag_defaults_off():
    """只有「设备在总线上却不排空端点」才置 degraded，真拔线不触发恢复。"""
    handle = ec20_usb_pty.BridgeHandle(
        dev=None, port=None, link="/tmp/x", master_fd=-1, slave_fd=-1,
        stop=__import__("threading").Event(),
    )
    assert handle.degraded is False


def test_usb_recovery_switches_out_and_back_then_verifies(monkeypatch):
    """恢复流程：切到备用组合 → 切回目标组合 → 回读校验通过才算成功。"""
    sent = []
    state = {"pid": "9001"}

    class _Port:
        interface = 2

    def fake_find(vid, timeout=60.0):
        return object(), _Port()

    def fake_at(dev, port, cmd, wait=1.5, rounds=15):
        sent.append(cmd)
        if cmd == "AT+CUSBPIDSWITCH?":
            return f"+CUSBPIDSWITCH: {state['pid']}\r\nOK\r\n"
        if cmd.startswith("AT+CUSBPIDSWITCH="):
            state["pid"] = cmd.split("=")[1].split(",")[0]
            return "OK\r\n"
        return "OK\r\n"

    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic", fake_find)
    monkeypatch.setattr(ec20_usb_pty, "at_on_interface", fake_at)
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", lambda d: None)

    assert ec20_usb_pty.usb_composition_recovery(0x1E0E, 0x9001, 0x9011, settle=0, confirm_grace=0) is True
    switches = [c for c in sent if c.startswith("AT+CUSBPIDSWITCH=")]
    assert switches == ["AT+CUSBPIDSWITCH=9011,1,1", "AT+CUSBPIDSWITCH=9001,1,1"]
    assert sent[-1] == "AT+CUSBPIDSWITCH?"          # 最后必须回读校验


def test_usb_recovery_retries_when_switch_back_fails(monkeypatch):
    """切回失败滞留备用组合时（真机踩过）必须重试，不能当成功返回。"""
    state = {"pid": "9001", "reject_first": True}

    class _Port:
        interface = 4

    def fake_at(dev, port, cmd, wait=1.5, rounds=15):
        if cmd == "AT+CUSBPIDSWITCH?":
            return f"+CUSBPIDSWITCH: {state['pid']}\r\nOK\r\n"
        if cmd.startswith("AT+CUSBPIDSWITCH="):
            target = cmd.split("=")[1].split(",")[0]
            if target == "9001" and state["reject_first"]:
                state["reject_first"] = False      # 模拟发太早被拒，仍停在 9011
                return "ERROR\r\n"
            state["pid"] = target
            return "OK\r\n"
        return "OK\r\n"

    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic",
                        lambda vid, timeout=60.0: (object(), _Port()))
    monkeypatch.setattr(ec20_usb_pty, "at_on_interface", fake_at)
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", lambda d: None)

    assert ec20_usb_pty.usb_composition_recovery(0x1E0E, 0x9001, 0x9011, settle=0, confirm_grace=0) is True
    assert state["pid"] == "9001"                  # 重试后确实回到目标组合


def test_usb_recovery_gives_up_and_reports_failure(monkeypatch):
    """始终切不回时返回 False（上层据此告警，提示人工断电）。"""
    class _Port:
        interface = 2

    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic",
                        lambda vid, timeout=60.0: (object(), _Port()))
    monkeypatch.setattr(ec20_usb_pty, "at_on_interface",
                        lambda d, p, cmd, wait=1.5, rounds=15:
                        "+CUSBPIDSWITCH: 9011\r\nOK\r\n" if cmd.endswith("?") else "ERROR\r\n")
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: None)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", lambda d: None)

    assert ec20_usb_pty.usb_composition_recovery(
        0x1E0E, 0x9001, 0x9011, attempts=2, settle=0, confirm_grace=0) is False


@pytest.mark.parametrize("bus_pid,reply,expected", [
    (0x9001, "+CUSBPIDSWITCH: 9011\r\nOK", True),
    (0x9011, "+CUSBPIDSWITCH: 9001\r\nOK", False),
    (None, "+CUSBPIDSWITCH: 9001\r\nOK", True),
    (None, "ERROR 9001", False),
])
def test_composition_confirmation_uses_bus_over_stale_reply(monkeypatch, bus_pid, reply, expected):
    from types import SimpleNamespace

    dev = SimpleNamespace(idProduct=bus_pid)
    released = []
    commands = []
    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic", lambda *a, **kw: (dev, object()))
    monkeypatch.setattr(ec20_usb_pty, "at_on_interface", lambda d, p, cmd: commands.append(cmd) or reply)
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", released.append)
    assert ec20_usb_pty.confirm_composition(0x1E0E, 0x9001, timeout=0)[0] is expected
    assert commands == ["AT+CUSBPIDSWITCH?"]
    assert released == [dev]


def test_last_recovery_attempt_polls_until_late_enumeration(monkeypatch):
    from types import SimpleNamespace

    clock = [0.0]
    commands = []
    released = []
    monkeypatch.setattr(ec20_usb_pty.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    def find(vid, timeout=60):
        return SimpleNamespace(idProduct=0x9001 if clock[0] >= 33 else 0x9011), SimpleNamespace(interface=4)

    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic", find)
    monkeypatch.setattr(ec20_usb_pty, "at_on_interface", lambda d, p, cmd, **kw:
                        commands.append(cmd) or "+CUSBPIDSWITCH: 9011\r\nOK")
    monkeypatch.setattr(ec20_usb_pty.usb.util, "dispose_resources", released.append)
    assert ec20_usb_pty.usb_composition_recovery(0x1E0E, 0x9001, 0x9011, attempts=1)
    assert commands.count("AT+CUSBPIDSWITCH=9001,1,1") == 1
    assert commands.count("AT+CUSBPIDSWITCH?") > 2
    assert 33 <= clock[0] <= 40
    assert len(released) == commands.count("AT+CUSBPIDSWITCH?")


def test_composition_confirmation_absent_device_is_bounded(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ec20_usb_pty.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ec20_usb_pty.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    def absent(vid, timeout):
        clock[0] += timeout
        return None, None

    monkeypatch.setattr(ec20_usb_pty, "find_at_port_dynamic", absent)
    assert not ec20_usb_pty.confirm_composition(0x1E0E, 0x9001, timeout=7)[0]
    assert clock[0] == 7
