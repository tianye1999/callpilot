"""三段式云端 ASR 收尾：commit 成功不发 clear（qwen3-asr 判非法），失败才断开会话并回落本地。"""

from agentcall.agents.local_agent import _SherpaPipeline


class FakeAsr:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.cleared = result, error, 0

    def commit(self):
        if self.error is not None:
            raise self.error
        return self.result

    def clear(self):
        self.cleared += 1


def pipeline(asr):
    p = object.__new__(_SherpaPipeline)
    p.streaming_asr = True
    p._asr_client = asr
    p._asr_segments = [b"\x00\x00" * 160]
    p._asr_failed = False
    p._fallback_transcribe = lambda segments: "本地兜底"
    return p


def test_commit_success_does_not_clear():
    asr = FakeAsr(result="你好")
    assert pipeline(asr).end_turn() == "你好"
    assert asr.cleared == 0


def test_commit_failure_discards_session_and_falls_back():
    asr = FakeAsr(error=RuntimeError("boom"))
    assert pipeline(asr).end_turn() == "本地兜底"
    assert asr.cleared == 1
