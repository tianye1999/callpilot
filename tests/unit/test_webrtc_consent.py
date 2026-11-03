"""Consent contracts with fake protocols and clocks; no sockets or peers."""

import asyncio
import hashlib
import hmac
import struct
from types import SimpleNamespace

import pytest
from aioice import Connection, stun

from agentcall import webrtc_ice


def test_consent_single_send_accepts_delayed_authenticated_response(monkeypatch):
    async def scenario():
        connection = Connection(ice_controlling=False)
        connection.remote_username, connection.remote_password = "peer", "secret"
        assert webrtc_ice.configure_connection(connection)
        sent, timers = [], []
        loop = asyncio.get_running_loop()

        class Timer:
            cancelled = False

            def cancel(self):
                self.cancelled = True

        def later(delay, callback, *args, **kwargs):
            timer = Timer()
            timers.append((delay, callback, timer))
            return timer

        protocol = SimpleNamespace(transactions={}, send_stun=lambda request, addr: sent.append((request, addr)),
                                   datagram_received=lambda *_: None)
        pair = SimpleNamespace(protocol=protocol, remote_addr=("203.0.113.1", 1234), component=1)
        with monkeypatch.context() as patch:
            patch.setattr(loop, "call_later", later)
            task = asyncio.create_task(webrtc_ice._consent_request(connection, pair))
            await asyncio.sleep(0)
            request, addr = sent[0]
            assert len(sent) == 1 and timers[0][0] == 4

            def reply(key=b"secret", address=addr, signed=True, unknown=False):
                message = stun.Message(stun.Method.BINDING, stun.Class.RESPONSE,
                                       transaction_id=request.transaction_id)
                data = bytes(message)
                if unknown:
                    # A valid optional extension before MESSAGE-INTEGRITY must
                    # not be lost by parse -> serialize before HMAC validation.
                    data += struct.pack("!HH4s", 0xAAAA, 4, b"test")
                    data = stun.set_body_length(data, len(data) - 20)
                if signed:
                    data = stun.set_body_length(data, len(data) - 20 + 24)
                    digest = hmac.new(key, data, hashlib.sha1).digest()
                    data += struct.pack("!HH", 0x0008, 20) + digest
                protocol.datagram_received(data, address)

            reply(address=("203.0.113.2", 1234))
            reply(key=b"wrong")
            reply(signed=False)
            await asyncio.sleep(0)
            assert not task.done()
            # No 500 ms timer: a response in the retained 4 s window still wins.
            reply(unknown=True)
            await task
            assert len(sent) == 1 and timers[0][2].cancelled
            assert not protocol.transactions
        await connection.close()

    asyncio.run(scenario())


def test_authenticated_forbidden_revokes_without_waiting_for_expiry(monkeypatch):
    async def scenario():
        clock = SimpleNamespace(now=0.0)
        pair = SimpleNamespace(local_addr=("192.0.2.1", 10), remote_addr=("203.0.113.1", 20))
        connection = SimpleNamespace(_nominated={1: pair})

        async def sleep(delay):
            clock.now += delay

        async def request(*_):
            response = stun.Message(stun.Method.BINDING, stun.Class.ERROR)
            response.attributes["ERROR-CODE"] = (403, "Forbidden")
            raise stun.TransactionFailed(response)

        with monkeypatch.context() as patch:
            patch.setattr(webrtc_ice.time, "monotonic", lambda: clock.now)
            patch.setattr(webrtc_ice.asyncio, "sleep", sleep)
            patch.setattr(webrtc_ice.random, "random", lambda: 0.5)
            patch.setattr(webrtc_ice, "_consent_request", request)
            await webrtc_ice._consent_component(connection, 1)
        assert clock.now == 5

    asyncio.run(scenario())


def test_response_processed_at_expiry_cannot_restore_consent(monkeypatch, caplog):
    async def scenario():
        clock = SimpleNamespace(now=0.0)
        pair = SimpleNamespace(local_addr=("192.0.2.1", 10), remote_addr=("203.0.113.1", 20))
        connection = SimpleNamespace(_nominated={1: pair})

        async def sleep(delay):
            clock.now += delay

        async def request(*_):
            clock.now = 30  # Simulate the loop processing a reply after a stall.

        with monkeypatch.context() as patch:
            patch.setattr(webrtc_ice.time, "monotonic", lambda: clock.now)
            patch.setattr(webrtc_ice.asyncio, "sleep", sleep)
            patch.setattr(webrtc_ice.random, "random", lambda: 0.5)
            patch.setattr(webrtc_ice, "_consent_request", request)
            await webrtc_ice._consent_component(connection, 1)
        assert clock.now == 30
        assert "consent expired" in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_consent_workers_drained_before_close_or_cancellation(monkeypatch, cancel):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        drained, closed = [], []

        async def worker(_, component):
            entered.set()
            try:
                if component == 1:
                    await release.wait()
                else:
                    await asyncio.Event().wait()
            finally:
                drained.append(component)

        async def close():
            assert sorted(drained) == [1, 2]
            closed.append(True)

        connection = SimpleNamespace(_nominated={1: None, 2: None}, close=close, _query_consent_task=None)
        monkeypatch.setattr(webrtc_ice, "_consent_component", worker)
        task = asyncio.create_task(webrtc_ice._query_consent(connection))
        connection._query_consent_task = task
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await task
            assert connection._query_consent_task is None
        assert sorted(drained) == [1, 2]
        assert closed == ([] if cancel else [True])

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_consent_timeout_or_cancel_removes_transaction_and_timer(monkeypatch, cancel):
    async def scenario():
        connection = Connection(ice_controlling=False)
        connection.remote_username, connection.remote_password = "peer", "secret"
        callbacks = []
        loop = asyncio.get_running_loop()
        handles = []
        original = loop.call_later

        def later(delay, callback, *args, **kwargs):
            callbacks.append(callback)
            handle = original(3600, callback, *args, **kwargs)
            handles.append(handle)
            return handle

        protocol = SimpleNamespace(transactions={}, send_stun=lambda *_: None, datagram_received=lambda *_: None)
        pair = SimpleNamespace(protocol=protocol, remote_addr=("203.0.113.1", 1234), component=1)
        with monkeypatch.context() as patch:
            patch.setattr(loop, "call_later", later)
            task = asyncio.create_task(webrtc_ice._consent_request(connection, pair))
            await asyncio.sleep(0)
            if cancel:
                task.cancel()
            else:
                callbacks[0]()
            with pytest.raises(asyncio.CancelledError if cancel else stun.TransactionTimeout):
                await task
            assert not protocol.transactions and all(h.cancelled() for h in handles)
        await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("interval_random", [0.0, 0.5, 1.0])
def test_consent_expires_at_30_seconds_not_six_failures(monkeypatch, interval_random):
    async def scenario():
        clock = SimpleNamespace(now=0.0)
        requests = []
        pair = SimpleNamespace(local_addr=("192.0.2.1", 10), remote_addr=("203.0.113.1", 20))
        connection = SimpleNamespace(_nominated={1: pair})

        async def sleep(delay):
            clock.now += delay

        async def request(*_):
            requests.append(clock.now)
            raise stun.TransactionTimeout()

        with monkeypatch.context() as patch:
            patch.setattr(webrtc_ice.time, "monotonic", lambda: clock.now)
            patch.setattr(webrtc_ice.asyncio, "sleep", sleep)
            patch.setattr(webrtc_ice.random, "random", lambda: interval_random)
            patch.setattr(webrtc_ice, "_consent_request", request)
            await webrtc_ice._consent_component(connection, 1)
        assert clock.now == 30
        assert requests == pytest.approx(list(range(int(4 + 2 * interval_random), 30, int(4 + 2 * interval_random))))

    asyncio.run(scenario())


@pytest.mark.parametrize("switch", [False, True])
def test_consent_response_refresh_and_nominated_pair_switch(monkeypatch, switch):
    async def scenario():
        clock = SimpleNamespace(now=0.0)
        old = SimpleNamespace(local_addr=("192.0.2.1", 10), remote_addr=("203.0.113.1", 20))
        new = SimpleNamespace(local_addr=("192.0.2.2", 11), remote_addr=("203.0.113.2", 21))
        connection = SimpleNamespace(_nominated={1: old})
        requests = []

        async def sleep(delay):
            clock.now += delay
            if switch and clock.now >= 15:
                connection._nominated[1] = new

        async def request(_, pair):
            requests.append((clock.now, pair))
            if clock.now == 5:
                return  # A valid reply resets the deadline to 35.
            raise stun.TransactionTimeout()

        with monkeypatch.context() as patch:
            patch.setattr(webrtc_ice.time, "monotonic", lambda: clock.now)
            patch.setattr(webrtc_ice.asyncio, "sleep", sleep)
            patch.setattr(webrtc_ice.random, "random", lambda: 0.5)
            patch.setattr(webrtc_ice, "_consent_request", request)
            await webrtc_ice._consent_component(connection, 1)
        assert clock.now == (45 if switch else 35)
        assert all(pair is (new if switch and timestamp >= 15 else old) for timestamp, pair in requests)

    asyncio.run(scenario())
