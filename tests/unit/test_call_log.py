"""CallLogger/CallRecord 单测：产物、录音开关、幂等、排序、清理、并发。"""

from __future__ import annotations

import json
import re
import threading
import time
import wave

import numpy as np
import pytest

from agentcall.call_log import CallLogger


def read_events(record_path):
    lines = (record_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def make_call_dir(base_dir, call_id, meta=..., summary=None):
    """直接铺一个通话目录，用于测试 list_calls/purge 的读取逻辑。"""
    path = base_dir / call_id
    path.mkdir(parents=True)
    if meta is ...:
        meta = {
            "id": call_id,
            "direction": "outbound",
            "number": "10000",
            "started_at": 1.0,
            "ended_at": 2.0,
            "status": "completed",
        }
    if meta is not None:
        text = meta if isinstance(meta, str) else json.dumps(meta)
        (path / "meta.json").write_text(text, encoding="utf-8")
    if summary is not None:
        (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return path


# ---- 完整生命周期与产物 ----


def test_full_lifecycle_produces_all_artifacts(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=True)
    record = clog.begin_call("outbound", "10000")

    assert re.fullmatch(r"\d{8}-\d{6}-outbound-10000", record.id)
    assert record.path == tmp_path / record.id

    record.log_event("latency", stage="asr_first_byte", ms=123.4)
    record.write_uplink(b"\x01\x02" * 160)
    record.write_downlink(b"\x03\x04" * 320)
    record.set_summary({"text": "对方确认了订单"})
    record.finish("completed")

    for name in ("events.jsonl", "meta.json", "uplink.wav", "downlink.wav", "summary.json"):
        assert (record.path / name).exists(), name

    # wav 格式：8kHz 16bit mono，帧数与写入字节一致
    with wave.open(str(record.path / "downlink.wav"), "rb") as wf:
        assert wf.getframerate() == 8000
        assert wf.getsampwidth() == 2
        assert wf.getnchannels() == 1
        assert wf.getnframes() == 320

    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["id"] == record.id
    assert meta["direction"] == "outbound"
    assert meta["number"] == "10000"
    assert meta["status"] == "completed"
    assert meta["answered"] is False
    assert re.fullmatch(r"call_[A-Za-z0-9_-]{12,80}", meta["public_id"])
    assert meta["public_id"] == record.public_id
    assert meta["content_updated_at"] >= meta["started_at"]
    assert meta["summary_state"] == "FAILED"
    assert meta["ended_at"] >= meta["started_at"]
    assert meta["duration"] == pytest.approx(meta["ended_at"] - meta["started_at"], abs=0.01)

    # 事件：call_started + latency + summary + call_finished，且都带 ts
    events = read_events(record.path)
    assert [e["type"] for e in events] == ["call_started", "latency", "summary", "call_finished"]
    assert all("ts" in e for e in events)
    assert meta["events"] == len(events)

    summary = json.loads((record.path / "summary.json").read_text(encoding="utf-8"))
    assert summary == {"text": "对方确认了订单"}


# ---- 合成对话录音 mixed.wav（立体声 左=AI / 右=对方，按时间轴对齐）----


def _read_stereo(path):
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getframerate() == 8000
        assert wf.getsampwidth() == 2
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    return data.reshape(-1, 2)  # 列0=左(AI), 列1=右(对方)


def test_mixed_recording_aligns_ai_to_uplink_timeline(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=True)
    record = clog.begin_call("outbound", "10000")

    # 对方(上行)连续录制：先 100 样本静音；此时 AI 说话 50 样本；再 100 样本对方声音。
    record.write_uplink(b"\x00\x00" * 100)   # uplink_bytes -> 200（= 样本100）
    record.write_downlink(b"\x22\x22" * 50)  # 打点在上行样本 100 处
    record.write_uplink(b"\x11\x11" * 100)   # 对方继续说
    record.finish("completed")

    stereo = _read_stereo(record.path / "mixed.wav")
    assert stereo.shape[0] == 200  # n = max(上行200, AI结束150)
    left, right = stereo[:, 0], stereo[:, 1]
    # 右声道 = 对方上行：前100静音，后100为 0x1111
    assert np.all(right[:100] == 0)
    assert np.all(right[100:200] == 0x1111)
    # 左声道 = AI：仅落在样本 [100,150) 处，其余静音（证明按时间轴对齐，非从头拼接）
    assert np.all(left[:100] == 0)
    assert np.all(left[100:150] == 0x2222)
    assert np.all(left[150:200] == 0)


def test_mixed_recording_downlink_only_ai_left_silence_right(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=True)
    record = clog.begin_call("outbound", "10000")
    record.write_downlink(b"\x22\x22" * 30)  # 只有 AI 说话，对方无上行
    record.finish("completed")

    stereo = _read_stereo(record.path / "mixed.wav")
    assert stereo.shape[0] == 30
    assert np.all(stereo[:, 0] == 0x2222)  # 左 = AI
    assert np.all(stereo[:, 1] == 0)       # 右 = 对方静音


def test_mixed_recording_skipped_when_no_pcm(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=True)
    record = clog.begin_call("outbound", "10000")
    record.finish("completed")  # 录音开但无音频
    assert not (record.path / "mixed.wav").exists()


def test_mixed_recording_absent_when_recording_disabled(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=False)
    record = clog.begin_call("outbound", "10000")
    record.write_uplink(b"\x11\x11" * 100)
    record.write_downlink(b"\x22\x22" * 50)
    record.finish("completed")
    assert not (record.path / "mixed.wav").exists()


def test_begin_call_rejects_bad_direction_and_handles_none_number(tmp_path):
    clog = CallLogger(tmp_path)
    with pytest.raises(ValueError):
        clog.begin_call("sideways", "10000")

    record = clog.begin_call("inbound", None)
    assert record.id.endswith("-inbound-unknown")

    # 同秒同号码再次拨打：id 加序号后缀，不冲突
    another = clog.begin_call("inbound", None)
    assert another.id != record.id
    assert another.path.is_dir()


# ---- 录音开关 ----


def test_recording_defaults_are_disabled(tmp_path):
    logger = CallLogger(tmp_path)
    record = logger.begin_call("inbound", "13800000000")

    assert logger.recording_enabled is False
    assert record.recording_enabled is False


def test_recording_disabled_writes_no_wav(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=False)
    record = clog.begin_call("inbound", "13800000000")
    record.write_uplink(b"\x00\x01" * 100)
    record.write_downlink(b"\x00\x01" * 100)
    record.finish("completed")

    assert not (record.path / "uplink.wav").exists()
    assert not (record.path / "downlink.wav").exists()
    assert (record.path / "events.jsonl").exists()
    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["recording_enabled"] is False
    assert meta["uplink_bytes"] == 0


def test_per_call_recording_override_keeps_events_but_no_audio(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=True)
    record = clog.begin_call(
        "outbound",
        "10000",
        source="remote_web_dialer",
        recording_enabled=False,
    )
    record.write_uplink(b"\x00\x01" * 100)
    record.finish("completed")

    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "remote_web_dialer"
    assert meta["recording_enabled"] is False
    assert not (record.path / "uplink.wav").exists()


# ---- finish 幂等 ----


def test_finish_is_idempotent(tmp_path):
    clog = CallLogger(tmp_path)
    record = clog.begin_call("outbound", "10086")
    record.write_uplink(b"\x00\x01" * 10)
    record.finish("completed")

    events_before = read_events(record.path)
    record.finish("failed")  # 第二次调用应为 no-op

    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert read_events(record.path) == events_before

    # finish 后写录音被丢弃、log_event 直接追加到磁盘
    record.write_uplink(b"\x00\x01" * 10)
    record.log_event("post_hangup", note="补记")
    events = read_events(record.path)
    assert events[-1]["type"] == "post_hangup"


def test_late_summary_advances_content_updated_at_without_changing_public_id(
    tmp_path, monkeypatch
):
    timestamps = iter([100.0, 110.0, 120.0, 130.0, 140.0])
    monkeypatch.setattr("agentcall.call_log.time.time", lambda: next(timestamps))
    record = CallLogger(tmp_path).begin_call("inbound", "13800000000")
    record.finish("completed")
    before = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))

    record.set_summary({"ok": True, "summary": "ready"})
    after = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))

    assert after["public_id"] == before["public_id"]
    assert after["content_updated_at"] > before["content_updated_at"]
    assert before["summary_state"] == "UNAVAILABLE"
    assert after["summary_state"] == "READY"
    assert after["events"] == before["events"]


def test_mark_summary_pending_is_explicit_and_advances_content_timestamp(
    tmp_path, monkeypatch
):
    timestamps = iter([100.0, 101.0, 102.0, 103.0])
    monkeypatch.setattr("agentcall.call_log.time.time", lambda: next(timestamps))
    record = CallLogger(tmp_path).begin_call("inbound", "10086")
    record.finish("completed")
    before = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))

    record.mark_summary_pending()
    after = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))

    assert before["summary_state"] == "UNAVAILABLE"
    assert after["summary_state"] == "PENDING"
    assert after["content_updated_at"] > before["content_updated_at"]


# ---- list_calls ----


def test_list_calls_sorted_new_to_old_with_limit_and_broken_dirs(tmp_path):
    clog = CallLogger(tmp_path)
    make_call_dir(tmp_path, "20260101-100000-inbound-111")
    make_call_dir(tmp_path, "20260102-100000-outbound-222")
    make_call_dir(
        tmp_path,
        "20260103-100000-outbound-333",
        summary={"text": "最新一通"},
    )
    make_call_dir(tmp_path, "20260104-100000-inbound-bad", meta="{not json")  # 损坏
    make_call_dir(tmp_path, "20260105-100000-inbound-nometa", meta=None)  # 无 meta

    calls = clog.list_calls()
    assert [c["id"] for c in calls] == [
        "20260103-100000-outbound-333",
        "20260102-100000-outbound-222",
        "20260101-100000-inbound-111",
    ]
    assert calls[0]["summary"] == {"text": "最新一通"}
    assert "summary" not in calls[1]
    assert set(calls[1]) >= {"id", "direction", "number", "started_at", "ended_at", "status"}

    assert [c["id"] for c in clog.list_calls(limit=1)] == ["20260103-100000-outbound-333"]


def test_list_calls_empty_base_dir(tmp_path):
    assert CallLogger(tmp_path / "fresh").list_calls() == []


# ---- purge_expired ----


def test_purge_expired_removes_only_old_calls(tmp_path):
    clog = CallLogger(tmp_path, retention_days=30)
    now = time.time()
    old = make_call_dir(
        tmp_path,
        "20250101-100000-inbound-old",
        meta={"id": "old", "started_at": now - 40 * 86400},
    )
    fresh = make_call_dir(
        tmp_path,
        "20260707-100000-inbound-new",
        meta={"id": "new", "started_at": now - 86400},
    )
    # meta 损坏但目录名可解析出旧时间戳 → 也应被清理
    old_by_name = make_call_dir(
        tmp_path, "20240101-100000-outbound-legacy", meta="{broken"
    )

    assert clog.purge_expired() == 2
    assert not old.exists()
    assert not old_by_name.exists()
    assert fresh.exists()
    assert clog.purge_expired() == 0  # 再跑一次没有可删的


def test_purge_disabled_when_retention_nonpositive(tmp_path):
    clog = CallLogger(tmp_path, retention_days=0)
    make_call_dir(
        tmp_path,
        "20200101-100000-inbound-ancient",
        meta={"id": "ancient", "started_at": 0.0},
    )
    assert clog.purge_expired() == 0
    assert (tmp_path / "20200101-100000-inbound-ancient").exists()


# ---- 环境变量工厂 ----


def test_from_env_reads_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_LOG_DIR", str(tmp_path / "calls"))
    monkeypatch.setenv("RECORDING_ENABLED", "false")
    monkeypatch.setenv("RECORDING_RETENTION_DAYS", "7")
    clog = CallLogger.from_env()
    assert clog.base_dir == tmp_path / "calls"
    assert clog.base_dir.is_dir()
    assert clog.recording_enabled is False
    assert clog.retention_days == 7


def test_from_env_bool_matches_config_panel(tmp_path, monkeypatch):
    """布尔判定统一走 config.get_bool：``on`` 不在真值集合内，与设置面板一致。"""
    monkeypatch.setenv("CALL_LOG_DIR", str(tmp_path / "calls"))
    monkeypatch.setenv("RECORDING_ENABLED", "on")
    assert CallLogger.from_env().recording_enabled is False
    monkeypatch.setenv("RECORDING_ENABLED", "yes")
    assert CallLogger.from_env().recording_enabled is True


# ---- 多线程冒烟 ----


def test_log_event_concurrent_smoke(tmp_path):
    clog = CallLogger(tmp_path, recording_enabled=True)
    record = clog.begin_call("outbound", "10000")
    threads_n, per_thread = 8, 200

    def worker(idx: int) -> None:
        for i in range(per_thread):
            record.log_event("latency", stage=f"t{idx}", ms=float(i))
            record.write_uplink(b"\x00\x01")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    record.finish("completed")

    events = read_events(record.path)
    # call_started + 并发事件 + call_finished，每行都是合法 JSON
    assert len(events) == threads_n * per_thread + 2
    with wave.open(str(record.path / "uplink.wav"), "rb") as wf:
        assert wf.getnframes() == threads_n * per_thread


def test_inbound_numbers_collects_only_inbound(tmp_path):
    """inbound_numbers 只收来电方号码:外呼不算、空号码跳过、去重。"""
    clog = CallLogger(base_dir=tmp_path / "calls")
    clog.begin_call("inbound", "13800000000").finish("completed")
    clog.begin_call("inbound", "10086").finish("completed")
    clog.begin_call("inbound", "10086").finish("failed")      # 重复来电 → 去重
    clog.begin_call("outbound", "13900000000").finish("completed")  # 外呼不算
    clog.begin_call("inbound", None).finish("completed")      # 空号码跳过
    assert clog.inbound_numbers() == {"13800000000", "10086"}


def test_inbound_numbers_empty_when_no_calls(tmp_path):
    assert CallLogger(base_dir=tmp_path / "calls").inbound_numbers() == set()


def test_finish_writes_answered_true_after_answered_event(tmp_path):
    clog = CallLogger(tmp_path)
    record = clog.begin_call("outbound", "10000")
    record.log_event("answered")
    record.finish("completed")

    meta = json.loads((record.path / "meta.json").read_text(encoding="utf-8"))
    assert meta["answered"] is True


def test_answered_outbound_numbers_collects_only_answered_outbound(tmp_path):
    clog = CallLogger(base_dir=tmp_path / "calls")
    answered = clog.begin_call("outbound", "10000")
    answered.log_event("answered")
    answered.finish("completed")
    not_connected = clog.begin_call("outbound", "10086")
    not_connected.finish("not_connected")
    inbound = clog.begin_call("inbound", "13800000000")
    inbound.log_event("answered")
    inbound.finish("completed")

    assert clog.answered_outbound_numbers() == {"10000"}


def test_answered_outbound_numbers_falls_back_to_legacy_events(tmp_path):
    clog = CallLogger(base_dir=tmp_path / "calls")
    legacy = make_call_dir(
        clog.base_dir,
        "20260101-100000-outbound-10000",
        meta={"id": "legacy", "direction": "outbound", "number": "10000"},
    )
    (legacy / "events.jsonl").write_text(
        json.dumps({"type": "call_started"}) + "\n" + json.dumps({"type": "answered"}) + "\n",
        encoding="utf-8",
    )
    missed = make_call_dir(
        clog.base_dir,
        "20260101-100001-outbound-10086",
        meta={"id": "missed", "direction": "outbound", "number": "10086"},
    )
    (missed / "events.jsonl").write_text(json.dumps({"type": "call_started"}) + "\n", encoding="utf-8")

    assert clog.answered_outbound_numbers() == {"10000"}
