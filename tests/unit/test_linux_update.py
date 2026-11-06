import asyncio
import subprocess
import threading
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentcall import linux_update, platforms
from agentcall.app_version import BUILD
from agentcall.web import server


class Runner:
    def __init__(self, ahead=2, failure=None, installed=True):
        self.calls = []
        self.ahead = ahead
        self.failure = failure
        self.installed = installed

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        assert kwargs['timeout'] > 0
        if args[:2] == ['git', 'fetch'] and self.failure:
            raise self.failure
        output = ''
        if args[:2] == ['systemctl', 'show']:
            output = ('LoadState=loaded\nActiveState=inactive\nResult=success\nExecMainStatus=0\n'
                      'InactiveEnterTimestamp=Fri 2026-11-06 12:00:00 UTC\n'
                      ) if self.installed else 'LoadState=not-found\n'
        elif args[0] == 'git':
            if '--short' in args:
                output = 'abc1234'
            elif '@{upstream}' in args:
                output = 'origin/main'
            elif 'rev-list' in args:
                output = str(self.ahead)
            elif 'log' in args:
                output = 'Latest fix'
        return subprocess.CompletedProcess(args, 0, output, '')


@pytest.mark.parametrize('ahead', [0, 2])
def test_status_and_cache(tmp_path, ahead):
    runner = Runner(ahead)
    updater = linux_update.LinuxUpdater(tmp_path, runner=runner)
    async def check():
        first = await updater.status()
        second = await updater.status()
        assert first['build'] == BUILD and first['sha'] == 'abc1234'
        assert second['ahead'] == ahead and second['latest'] == 'Latest fix'
        assert second['last_result']['Result'] == 'success'
    asyncio.run(check())
    assert len([c for c in runner.calls if c[:2] == ['git', 'fetch']]) == 1


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('git', 15),
                                     subprocess.CalledProcessError(1, 'git')])
def test_fetch_failure_is_cached(tmp_path, failure):
    runner = Runner(failure=failure)
    updater = linux_update.LinuxUpdater(tmp_path, runner=runner)
    async def check():
        assert (await updater.status())['error']
        assert (await updater.status())['ahead'] is None
    asyncio.run(check())
    assert len([c for c in runner.calls if c[:2] == ['git', 'fetch']]) == 1


@pytest.mark.parametrize('busy,queue,installed,expected', [
    (True, {}, True, 409), (False, {'pending': [1]}, True, 409),
    (False, {}, False, 503), (False, {}, True, 202),
])
def test_start(tmp_path, monkeypatch, busy, queue, installed, expected):
    monkeypatch.setattr(platforms, 'IS_LINUX', True)
    runner = Runner(installed=installed)
    service = SimpleNamespace(line_busy=lambda: busy, dial_queue_status=lambda: queue)
    app = server.build_app(hub=None, modem=None, service=service)
    app['linux_update'] = linux_update.LinuxUpdater(tmp_path, runner=runner)
    async def check():
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/linux_update/start', headers={
                'Origin': str(client.make_url('/')).rstrip('/'),
            })
            assert response.status == expected
            if expected == 503:
                assert 'install.sh' in (await response.json())['error']
    asyncio.run(check())
    starts = [c for c in runner.calls if c[0] == 'sudo']
    assert starts == ([['sudo', '-n', 'systemctl', 'start', '--no-block', 'callpilot-update.service']]
                      if expected == 202 else [])


def test_platform_and_csrf(tmp_path, monkeypatch):
    app = server.build_app(hub=None, modem=None)
    runner = Runner()
    app['linux_update'] = linux_update.LinuxUpdater(tmp_path, runner=runner)
    async def check():
        async with TestClient(TestServer(app)) as client:
            monkeypatch.setattr(platforms, 'IS_LINUX', False)
            assert (await client.get('/api/linux_update/status')).status == 503
            assert (await client.post('/api/linux_update/start')).status == 503
            monkeypatch.setattr(platforms, 'IS_LINUX', True)
            for headers in ({}, {'Origin': 'https://evil.example'},
                            {'Origin': str(client.make_url('/')).rstrip('/'), 'Sec-Fetch-Site': 'cross-site'}):
                assert (await client.post('/api/linux_update/start', headers=headers)).status == 403
    asyncio.run(check())
    assert runner.calls == []


def test_fetch_does_not_block_event_loop_and_coalesces(tmp_path):
    entered, release = threading.Event(), threading.Event()
    runner = Runner()
    def slow(args, **kwargs):
        if args[:2] == ['git', 'fetch']:
            entered.set()
            assert release.wait(5)
        return runner(args, **kwargs)
    updater = linux_update.LinuxUpdater(tmp_path, runner=slow)
    async def check():
        task = asyncio.create_task(updater.status())
        assert await asyncio.to_thread(entered.wait, 3)
        other = asyncio.create_task(updater.status())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, other)
        updater.checked -= 181
        await updater.status()
    asyncio.run(check())
    assert len([c for c in runner.calls if c[:2] == ['git', 'fetch']]) == 2


def test_missing_sudo_rule_has_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(platforms, 'IS_LINUX', True)
    runner = Runner()
    def denied(args, **kwargs):
        if args[0] == 'sudo':
            raise subprocess.CalledProcessError(1, args)
        return runner(args, **kwargs)
    app = server.build_app(hub=None, modem=None)
    app['linux_update'] = linux_update.LinuxUpdater(tmp_path, runner=denied)
    async def check():
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/linux_update/start', headers={
                'Origin': str(client.make_url('/')).rstrip('/'),
            })
            assert response.status == 503
            assert '--update-components' in (await response.json())['error']
    asyncio.run(check())


def test_login_required_for_lan_update(tmp_path, monkeypatch):
    monkeypatch.setattr(platforms, 'IS_LINUX', True)
    monkeypatch.setattr(server, '_is_local_client', lambda request: False)
    app = server.build_app(hub=None, modem=None, auth_token='test-password')
    runner = Runner()
    app['linux_update'] = linux_update.LinuxUpdater(tmp_path, runner=runner)
    async def check():
        async with TestClient(TestServer(app)) as client:
            headers = {'Origin': str(client.make_url('/')).rstrip('/')}
            assert (await client.get('/api/linux_update/status')).status == 401
            assert (await client.post('/api/linux_update/start', headers=headers)).status == 401
            headers['Authorization'] = 'Bearer test-password'
            assert (await client.post('/api/linux_update/start', headers=headers)).status == 202
    asyncio.run(check())
