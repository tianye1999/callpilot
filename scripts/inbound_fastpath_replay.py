"""Replay clock-local observations, never infer an unmeasured carrier-audio arrival.

Usage: python3 scripts/inbound_fastpath_replay.py [directory] [--json]
The Edge DAG preserves task durations. --ice-ms LOW HIGH adds a direct-WebRTC
sensitivity scenario, not a measurement or an audible-phone-playout prediction.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "edge_claim_received", "edge_endpoint_connect_start", "edge_endpoint_connect_return",
    "edge_media_ready", "edge_ata_start", "edge_pcm_ready", "edge_first_modem_to_phone",
)


def load_marks(path: Path) -> dict[str, float]:
    marks: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") != "latency":
            continue
        stage, value = row.get("stage"), row.get("ms")
        if not isinstance(stage, str) or not stage.startswith(("edge_", "client_")):
            continue
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Invalid numeric timing mark")
        if stage in marks and marks[stage] != value:
            raise ValueError("Conflicting duplicate timing mark")
        marks[stage] = float(value)
    return marks


def span(marks: dict[str, float], start: str, end: str) -> float | None:
    if start.split("_", 1)[0] != end.split("_", 1)[0]:
        raise ValueError("Intervals must use the same clock")
    if start not in marks or end not in marks:
        return None
    return marks[end] - marks[start]


def finish_times(graph: dict[str, tuple[float, tuple[str, ...]]]) -> dict[str, float]:
    """Evaluate a DAG with fixed nonnegative costs and unlimited independent workers."""
    finished: dict[str, float] = {}
    pending = dict(graph)
    while pending:
        ready = [name for name, (_, dependencies) in pending.items()
                 if all(dependency in finished for dependency in dependencies)]
        if not ready:
            raise ValueError("Cycle or missing DAG dependency")
        for name in ready:
            cost, dependencies = pending.pop(name)
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("Negative or nonfinite task duration")
            finished[name] = cost + max((finished[dependency] for dependency in dependencies), default=0.0)
    return finished


def analyze(marks: dict[str, float], call_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "call": call_id,
        "phone_track_first_frame_ms": span(marks, "client_answer_action", "client_downlink_first_frame"),
        "claim_http_ms": span(marks, "client_claim_start", "client_claim_response"),
        "edge_observed_ms": span(marks, "edge_claim_received", "edge_first_modem_to_phone"),
        "edge_parallel_model_ms": None,
        "answer_to_carrier_audio_ms": None,
        "answer_to_carrier_audio_model_ms": None,
        "legacy_cross_clock_value_ms": None,
        "missing": [stage for stage in REQUIRED if stage not in marks],
        "invalid": [],
    }
    # Reproduce the old script ONLY to expose its clock-domain error, never as evidence.
    if "client_answer_action" in marks and "edge_first_modem_to_phone" in marks:
        result["legacy_cross_clock_value_ms"] = (
            marks["edge_first_modem_to_phone"] - marks["client_answer_action"]
        )
    if result["missing"]:
        return result
    graph = {
        "endpoint": (marks["edge_endpoint_connect_return"] - marks["edge_endpoint_connect_start"], ()),
        "subscription": (marks["edge_media_ready"] - marks["edge_endpoint_connect_return"], ("endpoint",)),
        "ata_pcm": (marks["edge_pcm_ready"] - marks["edge_ata_start"], ()),
        "first_frame": (
            marks["edge_first_modem_to_phone"] - max(marks["edge_pcm_ready"], marks["edge_media_ready"]),
            ("subscription", "ata_pcm"),
        ),
    }
    if min(marks["edge_endpoint_connect_start"], marks["edge_ata_start"]) < marks["edge_claim_received"]:
        result["invalid"].append("Measured branch starts before model origin")
        return result
    try:
        result["edge_parallel_model_ms"] = finish_times(graph)["first_frame"]
    except ValueError as error:
        result["invalid"].append(str(error))
    return result


def direct_model(marks: dict[str, float], ice_range: tuple[float, float] | None) -> dict[str, Any] | None:
    """Readiness sensitivity: max(historical HTTP + assumed ICE, forward + PCM).

    The one-way claim delivery is unmeasured, so retain [0, historical HTTP].
    Historical HTTP also lacks the new Edge SDP gathering cost: callers must
    not interpret this interval as a latency guarantee for the new protocol.
    """
    if ice_range is None:
        return None
    low, high = ice_range
    if not all(math.isfinite(value) for value in ice_range) or not 0 <= low <= high:
        raise ValueError("ICE assumptions must be finite, nonnegative and ordered")
    claim = span(marks, "client_claim_start", "client_claim_response")
    pcm = span(marks, "edge_ata_start", "edge_pcm_ready")
    if claim is None or pcm is None or min(claim, pcm) < 0:
        return None
    return {
        "lower_ms": max(claim + low, pcm),
        "upper_ms": max(claim + high, claim + pcm),
        "ice_assumption_ms": [low, high],
        "claim_forward_assumption_ms": [0.0, claim],
    }


def number(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}"


def markdown(results: list[dict[str, Any]], ice_range: tuple[float, float] | None = None) -> str:
    lines = [
        "# Inbound Fastpath V2 离线回放",
        "",
        "单位 ms。按输入文件名排序匿名编号；只输出阶段耗时，不输出号码、姓名、令牌或音频内容。",
        "",
        "## 证据边界",
        "",
        "`client_*` 以手机 `claim_start` 为零，`edge_*` 以 Edge 收到 claim 为零。",
        "Swift `CallTiming.startClaim()` 重置 origin；Python `CallTiming.receive_client()` 原样保存客户端 ms。",
        "现有 `calltiming.py` 跨端相减缺少时钟偏移，故下表的旧脚本值只用于纠错，不是实测接听延迟。",
        "手机首音轨帧也可能是静音：通 01 手机在 claim 后约 7407 ms 已收到首帧，",
        "Edge 在收到 claim 后约 10090 ms 才 PCM 就绪，因此两者不是同一份运营商音频的端点。",
        "这 14 通没有带序号的 carrier frame 在手机播放回调中的对应观测，不能还原接听到运营商首声。",
        "",
        "## 固定耗时 DAG",
        "",
        "模型只改 Edge 子图，以 Edge claim 接收为起点，连接与 ATA+PCM 同时开始：",
        "",
        "```text",
        "claim ─┬─ endpoint ─ subscription ─┐",
        "       └─ ATA + PCM ───────────────┴─ first-frame tail",
        "```",
        "",
        "每个耗时由同一通、同一端的两个实测标记相减得到，不凭空消除订阅、开麦或 PCM 耗时。",
        "`new_edge = max(media_ready - endpoint_start, pcm_ready - ata_start)`",
        "`           + first_modem_to_phone - max(media_ready, pcm_ready)`。",
        "没有使用 `min(old,new)`，没有假设管理接口、HTTP、D1 或音频传输耗时为零。",
        "这些外部阶段不在此子图内，也不能将子图节省直接视为端到端节省。",
        "",
        "非劣定理仅限固定任务耗时、无新资源竞争的 DAG：删除依赖或提前根节点，最长路径不会增加。",
        "真实预连会改变网络、无线电及 SDK 状态，不满足固定耗时前提时，本回放不证明逐网络非劣。",
        "模型保留旧订阅尾部，因此不声称覆盖 V2 完整优化收益。",
        "",
        "## 直连 WebRTC 敏感性模型",
        "",
        "新模型为 `max(C + I, F + P)`；C 使用每通历史手机 HTTP RTT 作为代理，",
        "I 为显式输入的 ICE/DTLS 建连假设，P 为该通 ATA→PCM 同钟耗时，F 为未知 claim 单程送达。",
        "保留 `F ∈ [0,C]`，而不是假装 Edge 能在手机接听瞬间收到 claim。",
        "当 `I ∈ [Imin,Imax]`，区间为 `[max(C+Imin,P), max(C+Imax,C+P)]`。",
        "该值仅是媒体条件就绪的敏感性估算，不含后续编码/网络/播放缓冲；",
        "历史 C 也不含新协议的 Edge SDP gather 成本，因此绝非新路径上界或性能保证。",
        "",
        "方案 A 的 deferred SDP 先认领再补 SDP：以接听为零，C 为认领返回时刻，G 为同一手机 peer 的",
        "剩余 TURN/gather 完成时刻，S 为授权后补 SDP 请求至 answer 的耗时，I 为随后 ICE/DTLS。",
        "其就绪模型为 `max(max(C,G)+S+I,F+P)`，而不是把补 SDP 往返当作零。",
        "ATA/PCM 分支不再依赖 G，但媒体分支仍依赖真实 gather、SDP relay 与 ICE。",
        "历史 14 通均未测方案 A 的 G/S/I，故 **deferred SDP 逐通理论值全部 unknown**；",
        "下表的直连区间仅保留先前 inline/已就绪 SDP 的敏感性场景，不能冒充方案 A 的延迟预测。",
        (f"本次假设 I=[{ice_range[0]:.1f},{ice_range[1]:.1f}] ms，仅用于敏感性分析，不是历史测量。"
         if ice_range is not None else "本次未提供 ICE 假设，直连理论列保持 unknown。"),
        "",
        "## 逐通结果",
        "",
        "| 通 | 旧脚本跨钟值（无效） | 手机接听→首音轨帧 | Edge claim→首 modem 帧旧实测 "
        "| Edge 并行理论 | inline SDP 敏感性区间 | 接听→运营商首声旧/新 |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for result in results:
        direct = result.get("direct_model")
        direct_text = f"{direct['lower_ms']:.1f}–{direct['upper_ms']:.1f}" if direct else "unknown"
        lines.append(
            f"| {result['call']} | {number(result['legacy_cross_clock_value_ms'])} | "
            f"{number(result['phone_track_first_frame_ms'])} | {number(result['edge_observed_ms'])} | "
            f"{number(result['edge_parallel_model_ms'])} | {direct_text} | unknown / unknown |"
        )
    valid = [result for result in results if result["edge_parallel_model_ms"] is not None]
    nonworse = sum(result["edge_parallel_model_ms"] <= result["edge_observed_ms"] + 1e-6 for result in valid)
    lines.extend([
        "",
        f"Edge 子图可算 {len(valid)}/{len(results)} 通；固定耗时理论非劣 {nonworse}/{len(valid)} 通。",
        "完整接听→运营商首声逐通非劣：**未证明**，不是 14/14 通过。",
        "",
        "## 缺失与复现",
        "",
    ])
    for result in results:
        if result["missing"]:
            lines.append(f"- {result['call']}: 缺失 {', '.join(result['missing'])}。不补零。")
        if result["invalid"]:
            lines.append(f"- {result['call']}: {'; '.join(result['invalid'])}。拒绝计算。")
    lines.extend([
        "",
        "```sh",
        "python3 scripts/inbound_fastpath_replay.py",
        "python3 scripts/inbound_fastpath_replay.py --json",
        "# Explicit hypothetical range, not a measured ICE latency:",
        "python3 scripts/inbound_fastpath_replay.py --ice-ms 200 1000",
        ".venv/bin/pytest -q tests/unit/test_inbound_fastpath_replay.py",
        "```",
        "",
        "后续必须补充：手机 answer 单调时钟、认领请求/响应映射、Edge 带序号首 carrier frame、",
        "手机收到及播放同一序号帧的回调。跨端汇总以相关 ID 配对，禁止直接混用两个零点。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=ROOT / "local-docs" / "timing-2026-11-02")
    parser.add_argument("--json", action="store_true", help="Emit content-free structured observations")
    parser.add_argument("--report", type=Path, help="Generate a Markdown validation artifact")
    parser.add_argument("--ice-ms", nargs=2, type=float, metavar=("LOW", "HIGH"),
                        help="Explicit hypothetical ICE/DTLS range; unknown when omitted")
    args = parser.parse_args()
    paths = sorted(args.directory.glob("*.jsonl"))
    if not paths:
        parser.error("No JSONL observations found")
    ice_range = tuple(args.ice_ms) if args.ice_ms else None
    results = []
    for index, path in enumerate(paths, start=1):
        marks = load_marks(path)
        result = analyze(marks, f"call-{index:02d}")
        result["direct_model"] = direct_model(marks, ice_range)
        results.append(result)
    report = markdown(results, ice_range)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False, allow_nan=False) if args.json else report, end="\n")


if __name__ == "__main__":
    main()
