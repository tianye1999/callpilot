"""EventHub 单测：历史、持久化与 PDU 修复。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from agentcall.events import EventHub


def test_wait_for_event_unblocks_on_matching_publish():
    hub = EventHub(asyncio.new_event_loop())
    result: list[dict | None] = []

    thread = threading.Thread(
        target=lambda: result.append(
            hub.wait_for_event(
                lambda event: event.get("type") == "sms_in"
                and event.get("sender") == "10086",
                timeout=1.0,
            )
        )
    )
    thread.start()
    time.sleep(0.02)
    hub.publish({"type": "sms_in", "sender": "10010", "text": "ignore"})
    hub.publish({"type": "sms_in", "sender": "10086", "text": "official"})
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert result and result[0] is not None
    assert result[0]["text"] == "official"


def test_wait_for_event_can_match_existing_history_and_times_out():
    hub = EventHub(asyncio.new_event_loop())
    hub.publish({"type": "sms_in", "sender": "10086", "text": "existing", "ts": 10.0})

    found = hub.wait_for_event(
        lambda event: event.get("type") == "sms_in" and event.get("ts", 0) >= 10.0,
        timeout=0.01,
    )
    missing = hub.wait_for_event(
        lambda event: event.get("sender") == "10010",
        timeout=0.01,
    )

    assert found is not None and found["text"] == "existing"
    assert missing is None


def make_loop() -> asyncio.AbstractEventLoop:
    return asyncio.new_event_loop()


def test_publish_appends_history_with_timestamp():
    hub = EventHub(make_loop())
    hub.publish({"type": "system", "text": "hi"})

    events = hub.history()
    assert len(events) == 1
    assert events[0]["type"] == "system"
    assert "ts" in events[0]


def test_sms_events_persisted_and_reloaded(tmp_path):
    store = tmp_path / "messages.json"
    hub = EventHub(make_loop(), store_path=store)
    hub.publish({"type": "sms_in", "sender": "10086", "text": "hello"})
    hub.publish({"type": "system", "text": "not persisted"})

    data = json.loads(store.read_text(encoding="utf-8"))
    assert [e["type"] for e in data] == ["sms_in"]

    reloaded = EventHub(make_loop(), store_path=store)
    assert [e["type"] for e in reloaded.history()] == ["sms_in"]


def test_persist_write_does_not_hold_history_lock(tmp_path, monkeypatch):
    store = tmp_path / "messages.json"
    hub = EventHub(make_loop(), store_path=store)
    write_started = threading.Event()
    release_write = threading.Event()
    second_publish_done = threading.Event()
    original_write_text = Path.write_text

    def blocked_write(path, data, *args, **kwargs):
        if path == store:
            write_started.set()
            release_write.wait(timeout=2)
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", blocked_write)
    persist_thread = threading.Thread(
        target=hub.publish,
        args=({"type": "sms_in", "sender": "10086", "text": "hello"},),
        daemon=True,
    )
    persist_thread.start()
    assert write_started.wait(timeout=1)

    publish_thread = threading.Thread(
        target=lambda: (
            hub.publish({"type": "system", "text": "still responsive"}),
            second_publish_done.set(),
        ),
        daemon=True,
    )
    publish_thread.start()
    completed_without_disk = second_publish_done.wait(timeout=0.2)
    release_write.set()
    persist_thread.join(timeout=1)
    publish_thread.join(timeout=1)

    assert completed_without_disk, "磁盘写入期间 publish 热路径被 history lock 阻塞"
    assert [event["type"] for event in hub.history()] == ["sms_in", "system"]


def test_broadcast_tasks_referenced_until_done():
    loop = make_loop()
    try:
        hub = EventHub(loop)

        async def scenario():
            gate = asyncio.Event()

            class SlowWS:
                async def send_json(self, event):
                    await gate.wait()

            hub.register(SlowWS())
            hub.publish({"type": "system", "text": "hi"})
            # publish 经 call_soon_threadsafe 调度 _broadcast，让出一轮使其执行
            await asyncio.sleep(0)

            pending = list(hub._tasks)
            assert len(pending) == 1
            assert not pending[0].done()

            gate.set()
            await asyncio.gather(*pending)
            await asyncio.sleep(0)  # 等 done_callback 把 task 从集合中清掉
            assert not hub._tasks

        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_broadcast_audio_fans_out_only_to_audio_clients():
    """音频通道：无监听端零成本；有监听端时二进制帧原样送达，采样率可设。"""
    loop = make_loop()
    try:
        hub = EventHub(loop)

        async def scenario():
            received: list[bytes] = []

            class AudioWS:
                async def send_bytes(self, data):
                    received.append(bytes(data))

            # 无监听端：broadcast_audio 不调度、零成本
            hub.broadcast_audio(b"\x01\x02")
            await asyncio.sleep(0)
            assert received == []

            hub.set_audio_rate(16000)
            assert hub.audio_rate == 16000
            hub.register_audio(AudioWS())
            hub.broadcast_audio(b"\x01\x00\x02\x00")      # 默认 kind=0（下行）
            hub.broadcast_audio(b"\x05\x00", kind=1)      # kind=1（上行）
            await asyncio.sleep(0)               # 让 _broadcast_audio 执行
            await asyncio.gather(*list(hub._audio_tasks))
            # 每帧前置 1 字节方向标记：0x00=下行、0x01=上行
            assert received == [b"\x00\x01\x00\x02\x00", b"\x01\x05\x00"]

        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_reload_repairs_legacy_pdu_sms(tmp_path):
    # 迁移前遗留的未解码 PDU 短信（sender 为空、正文是 PDU hex）
    pdu = "00040D91683108000000F0000862707021030023044F60597D"
    store = tmp_path / "messages.json"
    store.write_text(
        json.dumps([{"type": "sms_in", "sender": None, "text": pdu, "ts": 1.0}]),
        encoding="utf-8",
    )

    hub = EventHub(make_loop(), store_path=store)
    event = hub.history()[0]
    assert event["sender"] == "+8613800000000"
    assert event["text"] == "你好"


def test_sms_dedup_within_session():
    """同一短信（sender+sms_ts+text）重复 publish：只入库一次，publish 返回 False。"""
    hub = EventHub(make_loop())
    sms = {"type": "sms_in", "sender": "10086", "text": "余额100", "sms_ts": "26/07/10,14:00:00"}
    assert hub.publish(dict(sms)) is True          # 首次入库
    assert hub.publish(dict(sms)) is False         # 重复 → 跳过
    assert len([e for e in hub.history() if e.get("type") == "sms_in"]) == 1


def test_sms_dedup_distinguishes_by_timestamp():
    """同发件方同正文但时间戳不同 = 两条不同短信，都入库（不误去重）。"""
    hub = EventHub(make_loop())
    base = {"type": "sms_in", "sender": "10001", "text": "剩余1.00GB"}
    assert hub.publish({**base, "sms_ts": "26/07/09,14:00:00"}) is True
    assert hub.publish({**base, "sms_ts": "26/07/10,14:00:00"}) is True
    assert len([e for e in hub.history() if e.get("type") == "sms_in"]) == 2


def test_sms_dedup_survives_restart(tmp_path):
    """重启补收：已持久化的短信再 publish（模拟启动补收 SIM 已存）不重复入库。"""
    store = tmp_path / "messages.json"
    hub = EventHub(make_loop(), store_path=store)
    sms = {"type": "sms_in", "sender": "10086", "text": "hi", "sms_ts": "26/07/10,09:00:00"}
    assert hub.publish(dict(sms)) is True

    reloaded = EventHub(make_loop(), store_path=store)  # 重启：从 messages.json 预填指纹
    assert reloaded.publish(dict(sms)) is False         # 补收同一条 → 去重
    assert len([e for e in reloaded.history() if e.get("type") == "sms_in"]) == 1
