"""Hardware-free, paid text API replay; never dispatches tools or starts speech.

Run: .venv/bin/python scripts/llm_turn_replay.py --runs 10
Each case is an independent conversation. Models alternate within each case/run;
one keep-alive client per target. No warmup is excluded from latency statistics.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from dotenv import dotenv_values

from agentcall.agents.local_agent import LocalPipelineAgent
from agentcall.agents.local_llm import DeepSeekChat
from agentcall.call_tools import CallTools
from agentcall.prompts import build_instructions


@dataclass(frozen=True)
class Scenario:
    name: str
    task: str
    utterance: str
    expected: tuple[tuple[str, dict[str, Any]], ...] = ()
    reply_required: bool = False


SCENARIOS = (
    Scenario("ivr_balance", "查询本月话费。", "查询话费请按2，人工服务请按0。", (("send_dtmf", {"digits": "2"}),)),
    Scenario("ivr_human", "转人工客服咨询套餐。", "套餐查询请按1，人工客服请按0。", (("send_dtmf", {"digits": "0"}),)),
    Scenario("ivr_multidigit", "查询工号103的信息。", "请按103#查询工号103的信息。",
             (("send_dtmf", {"digits": "103#"}),)),
    Scenario("human_intent", "查询本月话费。", "您好，人工客服，请问您需要办理什么业务？", reply_required=True),
    Scenario("amount", "查询话费金额，听到金额后先确认，不要挂断。", "您本月话费是38.50元，请问听清了吗？",
             reply_required=True),
    Scenario("goodbye", "咨询已经完成，对方道别时结束通话。", "没有其他事情了，再见。", (("hangup_call", {}),)),
    Scenario("identity", "查询话费；未提供身份证信息，需要身份核实时向机主确认，不要编造。",
             "请提供机主身份证后六位进行身份核实。", (("ask_owner", {}),)),
)


class NoHardware:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError("Replay must not access hardware or dispatch tools")


def context(scenario: Scenario) -> tuple[list[dict], list[dict]]:
    # Same prompt builder and outbound registry as CallSession; handlers are
    # bound but never invoked. No modem, pipeline, audio, or agent.start().
    agent = LocalPipelineAgent(llm_chat=lambda *args: {})
    agent.set_session_instructions(build_instructions(
        "outbound", "测试机主", "机主的电话助理", scenario.task, lang="zh",
    ))
    registry = CallTools(
        cast(Any, NoHardware()), hub=None, get_caller=lambda: None,
        get_record=lambda: None, schedule_hangup=lambda: None, direction="outbound",
    ).register()
    agent.set_tools(registry)
    return agent.initial_messages() + [{"role": "user", "content": scenario.utterance}], registry.specs()


@dataclass
class Result:
    correct: bool = False
    spoken_only: bool = False
    first_char_ms: float | None = None
    first_clause_ms: float | None = None
    tool_ms: float | None = None
    error: str = ""


def score(scenario: Scenario, message: dict) -> tuple[bool, bool]:
    calls = message.get("tool_calls") or []
    text = message.get("content") or ""
    actual = []
    malformed = False
    for call in calls:
        try:
            fn = call["function"]
            args = json.loads(fn["arguments"])
            if not isinstance(args, dict):
                raise ValueError("not an object")
            actual.append((fn["name"], args))
        except (KeyError, TypeError, ValueError):
            malformed = True
    correct = not malformed and len(actual) == len(scenario.expected)
    for name, required in scenario.expected:
        matching = [args for tool, args in actual if tool == name]
        correct = correct and len(matching) == 1 and all(matching[0].get(k) == v for k, v in required.items())
        if name == "hangup_call":
            correct = correct and matching == [{}]
        if name == "ask_owner":
            correct = correct and bool(matching) and bool(matching[0].get("question"))
    correct = correct and (not scenario.reply_required or bool(text.strip()))
    # Heuristic flag, not a semantic judge: imperative/claimed keypad action
    # without any send_dtmf call. Negated statements are intentionally excluded.
    claimed = bool(re.search(r"(?:已经|已|正在|现在|我来|我会|我帮您)?(?:按下|按了|按键|发送按键|发送DTMF)", text))
    claimed = claimed and not bool(re.search(r"(?:没有|未|不能|无法).{0,4}(?:按|发送)", text))
    spoken_only = claimed and not any(name == "send_dtmf" for name, _ in actual)
    return bool(correct), spoken_only


def replay(client: DeepSeekChat, scenario: Scenario, timeout: float,
           clock: Callable[[], float] = time.perf_counter) -> Result:
    messages, tools = context(scenario)
    result = Result()
    text = ""
    started = clock()

    def content(fragment: str) -> None:
        nonlocal text
        elapsed = (clock() - started) * 1000
        if fragment and result.first_char_ms is None:
            result.first_char_ms = elapsed
        text += fragment
        # First punctuation-delimited clause, without ClauseSplitter's maximum
        # length / idle flush. No punctuation means no first-clause sample.
        if result.first_clause_ms is None and re.search(r"[，。！？；、,.!?;:\n：]", text):
            result.first_clause_ms = elapsed

    def on_tools() -> None:
        if result.tool_ms is None:
            result.tool_ms = (clock() - started) * 1000

    try:
        message = client.chat(messages, tools, timeout, content, on_tools=on_tools)
        result.correct, result.spoken_only = score(scenario, message)
    except Exception as exc:
        # Never print provider exception bodies: they can reflect keys/prompts.
        result.error = type(exc).__name__
    return result


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile; empty/tool-only streams remain N/A, not zero."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def latency(results: list[Result], field: str) -> str:
    values = [value for r in results if not r.error and (value := getattr(r, field)) is not None]
    p50, p90 = percentile(values, .5), percentile(values, .9)
    return f"{p50:.0f}/{p90:.0f}({len(values)})" if p50 is not None and p90 is not None else "N/A(0)"


@contextlib.contextmanager
def settings(provider: str, model: str) -> Iterator[None]:
    updates = {"LOCAL_LLM_PROVIDER": provider, "LOCAL_LLM_MODEL": model, "LOCAL_LLM_THINKING": "disabled"}
    old = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run(targets: list[tuple[str, str]], scenarios: tuple[Scenario, ...], runs: int, timeout: float,
        client_factory: Callable[[], DeepSeekChat] = DeepSeekChat) -> dict[tuple[str, str, str], list[Result]]:
    results: dict[tuple[str, str, str], list[Result]] = {}
    clients = [client_factory() for _ in targets]
    try:
        for scenario in scenarios:
            for iteration in range(runs):
                # Rotate the first target to avoid always charging one model
                # for the earlier request in changing network conditions.
                for offset in range(len(targets)):
                    index = (iteration + offset) % len(targets)
                    provider, model = targets[index]
                    with settings(provider, model):
                        result = replay(clients[index], scenario, timeout)
                    results.setdefault((provider, model, scenario.name), []).append(result)
    finally:
        for client in clients:
            client.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--target", action="append", help="provider:model; repeat to compare")
    parser.add_argument("--scenario", action="append", choices=[s.name for s in SCENARIOS])
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.runs < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("runs and timeout must be positive")
    targets = []
    for target in args.target or ["deepseek:deepseek-flash", "dashscope:qwen3.6-flash"]:
        provider, sep, model = target.partition(":")
        if provider not in {"deepseek", "dashscope"} or not sep or not model:
            parser.error("target must be deepseek:model or dashscope:model")
        targets.append((provider, model))
    if len(set(targets)) != len(targets):
        parser.error("duplicate targets")
    # Read only API keys from the workspace .env, not unrelated runtime settings.
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env", interpolate=False)
    for provider, _ in targets:
        key = "DEEPSEEK_API_KEY" if provider == "deepseek" else "DASHSCOPE_API_KEY"
        if not env.get(key):
            parser.error(f"missing {key} in .env")
        os.environ[key] = str(env[key])
    scenarios = tuple(s for s in SCENARIOS if not args.scenario or s.name in args.scenario)
    results = run(targets, scenarios, args.runs, args.timeout)
    print("provider/model | scenario | correct/N | spoken-only | errors | char p50/p90(n) ms | "
          "clause p50/p90(n) ms | tool p50/p90(n) ms")
    for (provider, model, scenario), rows in results.items():
        print(f"{provider}/{model} | {scenario} | {sum(r.correct for r in rows)}/{len(rows)} | "
              f"{sum(r.spoken_only for r in rows)} | {sum(bool(r.error) for r in rows)} | "
              f"{latency(rows, 'first_char_ms')} | {latency(rows, 'first_clause_ms')} | {latency(rows, 'tool_ms')}")
    print("Independent turns; errors count as failures. N/A = no speech/tool event; (n) = valid latency samples.")
    print("Scores check tool names/required arguments and reply presence, not semantic answer quality. "
          "spoken-only is a heuristic.")


if __name__ == "__main__":
    main()
