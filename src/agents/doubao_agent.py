"""豆包端到端实时语音 Agent（火山引擎 Realtime Dialogue）。"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import struct
import uuid
from typing import Callable

import websockets

from .base import VoiceAgent

logger = logging.getLogger(__name__)

WS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"

# 协议常量（参考火山引擎 Realtime Dialogue 二进制协议）
PROTOCOL_VERSION = 0x1
HEADER_SIZE = 0x1
MSG_TYPE_FULL_CLIENT = 0x1
MSG_TYPE_AUDIO_ONLY = 0x2
MSG_TYPE_FULL_SERVER = 0x9
MSG_TYPE_AUDIO_SERVER = 0xB
MSG_SERIAL_JSON = 0x1
MSG_SERIAL_RAW = 0x0
MSG_COMPRESS_GZIP = 0x1
MSG_COMPRESS_NONE = 0x0


def _build_header(
    msg_type: int,
    serial: int,
    compress: int,
) -> bytes:
    byte0 = (PROTOCOL_VERSION << 4) | HEADER_SIZE
    byte1 = (msg_type << 4) | serial
    byte2 = (compress << 4) | 0x0
    byte3 = 0x0
    return bytes([byte0, byte1, byte2, byte3])


def _pack_json_event(event: dict) -> bytes:
    payload = gzip.compress(json.dumps(event, ensure_ascii=False).encode("utf-8"))
    header = _build_header(MSG_TYPE_FULL_CLIENT, MSG_SERIAL_JSON, MSG_COMPRESS_GZIP)
    return header + struct.pack(">I", len(payload)) + payload


def _pack_audio_payload(pcm: bytes) -> bytes:
    header = _build_header(MSG_TYPE_AUDIO_ONLY, MSG_SERIAL_RAW, MSG_COMPRESS_NONE)
    return header + struct.pack(">I", len(pcm)) + pcm


def _unpack_server_message(data: bytes) -> tuple[str | None, bytes | None]:
    if len(data) < 8:
        return None, None
    msg_type = (data[1] >> 4) & 0xF
    serial = data[1] & 0xF
    compress = (data[2] >> 4) & 0xF
    payload_size = struct.unpack(">I", data[4:8])[0]
    payload = data[8 : 8 + payload_size]
    if compress == MSG_COMPRESS_GZIP and payload:
        payload = gzip.decompress(payload)

    if msg_type in (MSG_TYPE_FULL_SERVER,) and serial == MSG_SERIAL_JSON:
        try:
            event = json.loads(payload.decode("utf-8"))
            return event.get("event") or event.get("type"), None
        except json.JSONDecodeError:
            return None, None

    if msg_type in (MSG_TYPE_AUDIO_SERVER, MSG_TYPE_AUDIO_ONLY):
        return "audio", payload
    return None, None


class DoubaoVoiceAgent(VoiceAgent):
    input_rate = 16000
    output_rate = 24000

    def __init__(
        self,
        app_id: str,
        access_key: str,
        resource_id: str,
        app_key: str,
        model_display_name: str,
    ) -> None:
        self.app_id = app_id
        self.access_key = access_key
        self.resource_id = resource_id
        self.app_key = app_key
        self.model_display_name = model_display_name
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._on_audio_out: Callable[[bytes], None] | None = None
        self._running = False

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        if not self.app_id or not self.access_key:
            raise RuntimeError("豆包凭证未配置，请设置 DOUBAO_APP_ID 和 DOUBAO_ACCESS_KEY")

        self._on_audio_out = on_audio_out
        self._running = True

        headers = {
            "X-Api-App-ID": self.app_id,
            "X-Api-Access-Key": self.access_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-App-Key": self.app_key,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

        self._ws = await websockets.connect(WS_URL, additional_headers=headers)
        logger.info("豆包 Realtime 连接已建立")

        start_session = {
            "event": "StartSession",
            "req_params": {
                "bot_name": "AgentCall",
                "system_role": (
                    f"你叫红茶语音助手，是接入电话的语音 Agent。接通后先用中文自我介绍，"
                    f"说明你是红茶语音助手，并说明底层模型是「{self.model_display_name}」。"
                    "回答简洁，适合电话语音。"
                ),
                "speaking_style": "语速适中，口语自然。",
                "input_mod": "audio",
                "model": "O",
            },
        }
        await self._ws.send(_pack_json_event(start_session))
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def send_audio(self, pcm: bytes) -> None:
        if not self._ws or not pcm:
            return
        await self._ws.send(_pack_audio_payload(pcm))

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                finish = {"event": "FinishSession"}
                await self._ws.send(_pack_json_event(finish))
            except Exception as exc:  # noqa: BLE001
                logger.warning("结束豆包会话异常: %s", exc)
            await self._ws.close()
        if self._recv_task:
            await asyncio.gather(self._recv_task, return_exceptions=True)
        self._ws = None

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    continue
                event_name, audio = _unpack_server_message(message)
                if audio and self._on_audio_out:
                    self._on_audio_out(audio)
                elif event_name:
                    logger.debug("豆包事件: %s", event_name)
                if not self._running:
                    break
        except websockets.ConnectionClosed:
            logger.info("豆包连接已关闭")
        except Exception as exc:  # noqa: BLE001
            logger.error("豆包接收循环异常: %s", exc)
