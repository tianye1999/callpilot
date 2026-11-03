from pathlib import Path
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
    assert values['MODEM_VENDOR'] == 'quectel'
    assert values['AGENT_PROVIDER'] == 'local'


def test_restart_budget_and_backoff():
    policy = runtime.RestartBudget()
    assert [policy.next_delay() for _ in range(7)] == [0, 1, 2, 4, 8, 16, None]


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


def test_packaging_contract():
    root = Path(__file__).resolve().parents[2]
    spec = (root / 'packaging/agentcall.spec').read_text(encoding='utf-8')
    assert '"tray_app.py" if IS_MACOS or IS_WINDOWS' in spec
    assert '".env.example"' in spec
    installer = (root / 'packaging/windows/CallPilot.iss').read_text(encoding='utf-8')
    assert 'PrivilegesRequired=lowest' in installer
    assert '--shutdown' in installer
    assert '[UninstallDelete]' not in installer
