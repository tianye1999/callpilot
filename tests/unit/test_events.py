"""EventHub 单测：历史、持久化与 PDU 修复。"""

from __future__ import annotations

import asyncio
import json

from agentcall.events import EventHub


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
