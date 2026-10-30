"""3GPP TS 23.038 / 23.040 短信编码与解码；纯函数，不访问模组。"""

from dataclasses import dataclass

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = {10: "\f", 20: "^", 40: "{", 41: "}", 47: "\\",
                  60: "[", 61: "~", 62: "]", 64: "|", 101: "€"}
_ENCODE = {char: [index] for index, char in enumerate(GSM7_BASIC) if index != 27}
_ENCODE.update({char: [27, index] for index, char in GSM7_EXTENSION.items()})


@dataclass(frozen=True)
class ConcatInfo:
    ref: int
    total: int
    seq: int


@dataclass(frozen=True)
class SmsDeliver:
    sender: str | None
    timestamp: str
    body: str
    concat: ConcatInfo | None = None


@dataclass(frozen=True)
class SubmitPdu:
    pdu: str
    tpdu_length: int


def _split(text: str, costs: list[int], limit: int) -> list[str]:
    parts = []
    start = 0
    while start < len(text):
        end = start
        used = 0
        whitespace_end = start
        while end < len(text) and used + costs[end] <= limit:
            used += costs[end]
            end += 1
            if text[end - 1].isspace():
                whitespace_end = end
        if end < len(text) and whitespace_end > start:
            end = whitespace_end
        parts.append(text[start:end])
        start = end
    return parts or [""]


def encode_submit(number: str, text: str, reference: int = 0) -> list[SubmitPdu]:
    """编码 SMS-SUBMIT，默认短信中心、相对有效期 167（24 小时）。"""
    digits = number.removeprefix("+")
    if not digits or len(digits) > 20 or any(char not in "0123456789" for char in digits):
        raise ValueError("短信号码必须为 1 至 20 位数字，可带国际前缀 +")
    if not 0 <= reference <= 255:
        raise ValueError("连接参考号必须在 0..255")
    padded = digits + ("F" if len(digits) % 2 else "")
    address = bytes.fromhex("".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2)))
    gsm = all(char in _ENCODE for char in text)
    costs = [len(_ENCODE[char]) if gsm else len(char.encode("utf-16-be")) // 2 for char in text]
    single_limit, multi_limit = (160, 153) if gsm else (70, 67)
    parts = [text] if sum(costs) <= single_limit else _split(text, costs, multi_limit)
    if len(parts) > 255:
        raise ValueError("长短信不能超过 255 段")
    result = []
    for seq, part in enumerate(parts, 1):
        header = bytes([5, 0, 3, reference, len(parts), seq]) if len(parts) > 1 else b""
        if gsm:
            septets = [code for char in part for code in _ENCODE[char]]
            header_septets = (len(header) * 8 + 6) // 7
            udl = header_septets + len(septets)
            # UDH 补齐到 septet 边界，六字节连接头后必须空出一位。
            packed = int.from_bytes(header, "little")
            for index, code in enumerate(septets, header_septets):
                packed |= code << (index * 7)
            ud = packed.to_bytes((udl * 7 + 7) // 8, "little")
        else:
            ud = header + part.encode("utf-16-be")
            udl = len(ud)
        tpdu = (bytes([0x51 if header else 0x11, 0, len(digits), 0x91 if number.startswith("+") else 0x81])
                + address + bytes([0, 0 if gsm else 8, 167, udl]) + ud)
        result.append(SubmitPdu((b"\x00" + tpdu).hex().upper(), len(tpdu)))
    return result


def _gsm_text(data: bytes, count: int, offset: int = 0) -> str:
    packed = int.from_bytes(data, "little")
    chars = []
    escaped = False
    for index in range(count):
        code = (packed >> (offset + index * 7)) & 127
        if escaped:
            chars.append(GSM7_EXTENSION.get(code, GSM7_BASIC[code]))
            escaped = False
        elif code == 27:
            escaped = True
        else:
            chars.append(GSM7_BASIC[code])
    if escaped:
        raise ValueError("不完整 GSM 扩展转义")
    return "".join(chars)


def decode_deliver(pdu: str) -> SmsDeliver | None:
    """解码 SMS-DELIVER，畸形或不支持的压缩编码返回 None，不吞掉 UDH。"""
    try:
        raw = bytes.fromhex(pdu)
        pos = 0

        def take(size: int) -> bytes:
            nonlocal pos
            value = raw[pos:pos + size]
            if len(value) != size:
                raise ValueError("PDU 截断")
            pos += size
            return value

        take(take(1)[0])
        first = take(1)[0]
        if first & 3:
            return None
        count, ton = take(2)
        address = take((count + 1) // 2)
        if ton & 0x70 == 0x50:
            sender = _gsm_text(address, count * 4 // 7)
        else:
            sender = "".join(f"{byte & 15:X}{byte >> 4:X}" for byte in address)[:count]
            if ton & 0x70 == 0x10:
                sender = "+" + sender
        _, dcs = take(2)
        scts = take(7)
        date = [f"{byte & 15}{byte >> 4}" for byte in scts[:6]]
        timestamp = f"{date[0]}/{date[1]}/{date[2]},{date[3]}:{date[4]}:{date[5]}"
        if dcs & 0xC0 == 0 and dcs & 0x20:
            return None  # 不支持压缩，保留 SIM 副本供后续处理。
        coding = dcs & 12
        if dcs & 0xF0 in (0xC0, 0xD0, 0xE0):
            coding = 8 if dcs & 0xF0 == 0xE0 else 0
        elif dcs & 0xF0 == 0xF0:
            coding = 4 if dcs & 4 else 0
        if coding == 12:
            return None
        udl = take(1)[0]
        ud = take((udl * 7 + 7) // 8 if coding == 0 else udl)
        if len(ud) > 140:
            return None
        header_size = 0
        concat = None
        if first & 0x40:
            header_size = ud[0] + 1
            if header_size > len(ud):
                return None
            cursor = 1
            while cursor < header_size:
                if cursor + 2 > header_size:
                    return None
                iei, size = ud[cursor:cursor + 2]
                cursor += 2
                end = cursor + size
                if end > header_size:
                    return None
                value = ud[cursor:end]
                if iei in (0, 8):
                    if size != (3 if iei == 0 else 4):
                        return None
                    concat = ConcatInfo(int.from_bytes(value[:-2], "big"), value[-2], value[-1])
                    if not 1 <= concat.seq <= concat.total:
                        return None
                cursor = end
        if coding == 0:
            header_septets = (header_size * 8 + 6) // 7
            if header_septets > udl:
                return None
            body = _gsm_text(ud, udl - header_septets, header_septets * 7)
        else:
            body = ud[header_size:].decode("utf-16-be" if coding == 8 else "latin-1")
        return SmsDeliver(sender or None, timestamp, body, concat)
    except (ValueError, IndexError, UnicodeError):
        return None
