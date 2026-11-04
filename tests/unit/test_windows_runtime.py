from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentcall import windows_runtime as runtime


def test_seed_never_overwrites_user_env(tmp_path):
    template = tmp_path / 'template'
    template.write_text('EXAMPLE=1\n', encoding='utf-8')
    support = tmp_path / 'support'
    runtime.seed_env(support, template)
    assert (support / '.env').read_text(encoding='utf-8') == 'EXAMPLE=1\n'
    (support / '.env').write_text('USER=2', encoding='utf-8')
    runtime.seed_env(support, template)
    assert (support / '.env').read_text(encoding='utf-8') == 'USER=2'


def test_windows_seed_removes_mac_hardware_overrides(tmp_path):
    from dotenv import dotenv_values

    root = Path(__file__).resolve().parents[2]
    runtime.seed_env(tmp_path, root / '.env.example')
    values = dotenv_values(tmp_path / '.env')
    assert not {'MODEM_PORT', 'MODEM_AUDIO_MODE', 'MODEM_PCM_PORT'} & values.keys()
    assert values['MODEM_VENDOR'] == 'auto'
    assert values['AGENT_PROVIDER'] == 'local'


def test_restart_budget_and_backoff():
    policy = runtime.RestartBudget()
    assert [policy.next_delay() for _ in range(9)] == [0, 1, 2, 4, 8, 16, 60, 60, 60]


@pytest.fixture
def tray_harness(monkeypatch, tmp_path):
    import sys

    state = SimpleNamespace(now=0.0, launches=[], children=[], titles=[], tick=lambda: None,
                            icon=None, fail_launches=False)

    class Event:
        def __init__(self):
            self.value = False

        def set(self):
            self.value = True

        def clear(self):
            self.value = False

        def is_set(self):
            return self.value

        def wait(self, delay):
            if not self.value:
                state.now += delay
                assert state.now < 2000, 'Supervisor did not stop'
                state.tick()
            return self.value

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self):
            assert (tmp_path / 'service.stop').exists()
            self.returncode = 0

    def popen(command, **kwargs):
        assert command == [sys.executable, '--service']
        assert kwargs == {'cwd': tmp_path}
        state.launches.append(state.now)
        if state.fail_launches:
            raise OSError('Temporary startup failure')
        child = Child()
        state.children.append(child)
        return child

    class Icon:
        def __init__(self, *args, menu):
            self.menu = {item.text: item.action for item in menu()}
            state.icon = self

        @property
        def title(self):
            return state.titles[-1]

        @title.setter
        def title(self, value):
            state.titles.append(value)

        def run(self, setup):
            setup(self)

        def stop(self):
            state.stopped = True

    monkeypatch.setattr(runtime, '_require_frozen', lambda: None)
    monkeypatch.setattr(runtime.threading, 'Event', Event)
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: state.now)
    monkeypatch.setattr(runtime.subprocess, 'Popen', popen)
    monkeypatch.setattr(sys, 'argv', ['CallPilot', '--login'])
    monkeypatch.setitem(sys.modules, 'pystray', SimpleNamespace(
        Icon=Icon, Menu=lambda builder: builder,
        MenuItem=lambda text, action, **kwargs: SimpleNamespace(text=text, action=action),
    ))
    monkeypatch.setitem(sys.modules, 'PIL', SimpleNamespace(Image=SimpleNamespace(open=lambda path: None)))
    monkeypatch.setitem(sys.modules, 'tray_app', SimpleNamespace(
        icon_path=lambda online: 'fake.ico', web_url=lambda: 'fake://backend',
        probe_online=lambda *args, **kwargs: False,
    ))
    from agentcall import windows_autostart

    monkeypatch.setattr(windows_autostart, 'stale_run_target', lambda: None)
    state.run = lambda: runtime.run_tray(tmp_path)
    state.stop = lambda: (tmp_path / 'tray.stop').touch()
    return state


@pytest.mark.parametrize('fail_launches', [False, True])
def test_tray_fast_then_repeated_slow_retries(tray_harness, caplog, fail_launches):
    state = tray_harness
    state.fail_launches = fail_launches

    def tick():
        if state.children:
            state.children[-1].returncode = 1
        if len(state.launches) == 9:
            state.stop()

    state.tick = tick
    with caplog.at_level('INFO', logger=runtime.__name__):
        state.run()
    gaps = [b - a for a, b in zip(state.launches, state.launches[1:])]
    assert gaps == [1.25, 2.25, 4.25, 8.25, 16.25, 60.25, 60.25, 60.25]
    assert 'CallPilot: service error, retrying' in state.titles
    assert 'attempt=9 delay=60 mode=slow' in caplog.text
    assert state.stopped


@pytest.mark.parametrize('crash_after', [599.75, 600.25, 601])
def test_tray_stable_runtime_resets_budget_once(tray_harness, caplog, crash_after):
    state = tray_harness

    def tick():
        if len(state.launches) == 1:
            state.children[-1].returncode = 1
        elif len(state.launches) == 2 and state.now - state.launches[-1] >= crash_after:
            state.children[-1].returncode = 1
        elif len(state.launches) == 3:
            state.stop()

    state.tick = tick
    with caplog.at_level('INFO', logger=runtime.__name__):
        state.run()
    stable = crash_after >= 600
    assert state.launches[2] - state.launches[1] == crash_after + (0 if stable else 2)
    assert caplog.text.count('Stable backend runtime; resetting restart budget') == int(stable)
    assert caplog.text.count('attempt=1 delay=0 mode=fast') == 1 + int(stable)


def test_tray_manual_restart_resets_slow_budget(tray_harness, tmp_path):
    state = tray_harness
    requested_at = []

    def tick():
        if state.children:
            state.children[-1].returncode = 1
        if len(state.launches) == 6 and state.now - state.launches[-1] >= 10 and not requested_at:
            requested_at.append(state.now)
            state.icon.menu['Restart service']()
        elif len(state.launches) == 7:
            state.stop()

    state.tick = tick
    state.run()
    assert state.launches[-1] == requested_at[0]
    assert (tmp_path / 'service.stop').exists()


def test_tray_manual_restart_waits_for_running_child(tray_harness, tmp_path, caplog):
    state = tray_harness
    requested_at = []

    def tick():
        if len(state.launches) == 1 and not requested_at:
            requested_at.append(state.now)
            state.icon.menu['Restart service']()
        elif len(state.launches) == 1 and state.now - requested_at[0] >= 2:
            assert (tmp_path / 'service.stop').exists()
            state.children[-1].returncode = 0
        elif len(state.launches) == 2:
            state.stop()

    state.tick = tick
    with caplog.at_level('INFO', logger=runtime.__name__):
        state.run()
    assert state.launches[1] == requested_at[0] + 2
    assert caplog.text.count('attempt=1 delay=0 mode=fast') == 2


@pytest.mark.parametrize('quit_mode', ['stop_file', 'menu'])
def test_tray_does_not_launch_during_exit(tray_harness, quit_mode):
    state = tray_harness

    def tick():
        if state.children:
            state.children[-1].returncode = 1
        if state.now >= 40:
            # Both requests in the same iteration: exit must win over restart.
            state.icon.menu['Restart service']()
            if quit_mode == 'stop_file':
                state.stop()
            else:
                state.icon.menu['Quit CallPilot']()

    state.tick = tick
    state.run()
    assert len(state.launches) == 6
    assert state.stopped


def test_role_lock_and_bounded_shutdown(tmp_path):
    lock = runtime.acquire_lock(tmp_path, 'service')
    assert lock is not None
    assert runtime.acquire_lock(tmp_path, 'service') is None
    assert runtime.shutdown(tmp_path, timeout=0) == 1
    assert (tmp_path / 'service.stop').exists()
    lock.close()
    assert runtime.shutdown(tmp_path, timeout=0) == 0


def test_safe_backend_uses_real_meta_without_hardware_or_listener(monkeypatch):
    import asyncio
    import json

    from aiohttp.test_utils import make_mocked_request

    from agentcall import windows_smoke
    from agentcall.app_version import VERSION
    from agentcall.web import server

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import app

    def forbidden(*args, **kwargs):
        raise AssertionError('Hardware or real backend construction is forbidden')

    monkeypatch.setattr(app, 'CallAgentService', forbidden)
    monkeypatch.setattr('serial.Serial', forbidden)
    monkeypatch.setattr('serial.serial_for_url', forbidden)
    monkeypatch.setattr('serial.tools.list_ports.comports', forbidden)
    monkeypatch.setattr('usb.core.find', forbidden)
    monkeypatch.setattr(server, '_scan_usb', forbidden)
    responses = []

    class Runner:
        def __init__(self, application):
            self.application = application

        async def setup(self):
            pass

        async def cleanup(self):
            pass

    class Site:
        def __init__(self, runner, host, port):
            assert (host, port) == ('127.0.0.1', 47100)
            self.application = runner.application

        async def start(self):
            response = await server._meta(make_mocked_request('GET', '/api/meta', app=self.application))
            responses.append(json.loads(response.text))

    monkeypatch.setattr(windows_smoke.web, 'AppRunner', Runner)
    monkeypatch.setattr(windows_smoke.web, 'TCPSite', Site)
    app.main(stop_requested=lambda: True, hardware_selftest=True)
    assert responses[0]['version'] == VERSION
    assert responses[0]['hardware']['usb_online'] is False
    assert not asyncio._get_running_loop()


def test_windows_dispatch_and_mac_selftest_unchanged(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import app
    import tray_app

    monkeypatch.setattr(tray_app, '_prepend_runtime_paths', lambda: None)
    monkeypatch.setattr('sys.argv', ['CallPilot', '--selftest'])
    monkeypatch.setattr('sys.platform', 'win32')
    monkeypatch.setattr(tray_app, '_is_frozen', lambda: True)
    windows = Mock(return_value=0)
    monkeypatch.setattr(runtime, 'main', windows)
    tray_app.main()
    windows.assert_called_once()
    monkeypatch.setattr('sys.platform', 'darwin')
    mac = Mock(return_value=0)
    monkeypatch.setattr(app, '_selftest', mac)
    with pytest.raises(SystemExit) as exc:
        tray_app.main()
    assert exc.value.code == 0
    mac.assert_called_once()


def test_win32_source_keeps_original_entry_without_windows_dispatch(monkeypatch):
    import app
    import tray_app

    monkeypatch.setattr('sys.platform', 'win32')
    monkeypatch.setattr(tray_app, '_is_frozen', lambda: False)
    monkeypatch.setattr(tray_app, '_prepend_runtime_paths', lambda: None)
    monkeypatch.setattr('sys.argv', ['tray_app.py', '--selftest'])
    windows = Mock(side_effect=AssertionError('Source must not enter Windows runtime'))
    monkeypatch.setattr(runtime, 'main', windows)
    original = Mock(return_value=0)
    monkeypatch.setattr(app, '_selftest', original)
    with pytest.raises(SystemExit) as exc:
        tray_app.main()
    assert exc.value.code == 0
    original.assert_called_once()
    windows.assert_not_called()


@pytest.mark.parametrize('entry', ['main', 'run_tray'])
def test_windows_runtime_rejects_source_before_side_effects(monkeypatch, tmp_path, entry):
    monkeypatch.setattr(runtime.config, '_is_frozen', lambda: False)
    monkeypatch.setattr(runtime.config, 'app_support_dir', Mock(side_effect=AssertionError('No data writes')))
    monkeypatch.setattr(runtime.subprocess, 'Popen', Mock(side_effect=AssertionError('No real child')))
    with pytest.raises(RuntimeError, match='frozen'):
        getattr(runtime, entry)(*([tmp_path] if entry == 'run_tray' else []))


def test_packaging_contract():
    root = Path(__file__).resolve().parents[2]
    spec = (root / 'packaging/agentcall.spec').read_text(encoding='utf-8')
    assert '"tray_app.py" if IS_MACOS or IS_WINDOWS' in spec
    assert '".env.example"' in spec
    assert not (root / 'packaging/windows/CallPilot.iss').exists(), '绿色版不应再发安装器'
    assert not (root / 'scripts/windows/test_installer.ps1').exists(), '安装器冒烟脚本应已移除'
    assert (root / 'scripts/windows/test_portable.ps1').is_file()
    workflow = (root / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    assert 'ISCC' not in workflow
    assert 'setup.exe' not in workflow
    assert 'test_portable.ps1' in workflow


def test_portable_readonly_runtime_exits_without_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.config, '_is_frozen', lambda: True)
    monkeypatch.setattr(runtime.config, 'is_portable_mode', lambda: True)
    monkeypatch.setattr(runtime.config, 'app_support_dir', lambda: tmp_path / 'data-root')
    monkeypatch.setattr(runtime.config, 'ensure_dir_writable',
                        Mock(side_effect=runtime.config.DataRootNotWritable('read-only')))
    monkeypatch.setattr(runtime, '_report_portable_error', Mock())
    monkeypatch.setattr('sys.argv', ['CallPilot', '--service'])

    with pytest.raises(SystemExit) as exc:
        runtime.main()
    assert exc.value.code == 1
    assert not (tmp_path / 'data-root').exists()
