"""通过接收侧解码和重组验证发送侧，不能在分段间补空格。"""

from dataclasses import replace

import pytest

from agentcall.sms_concat import MISSING_PART, SmsConcat
from agentcall.sms_pdu import ConcatInfo, decode_deliver, encode_submit


def as_deliver(part, *, timestamp="62010321000000"):
    # 仅替换 SUBMIT 的信封，原样保留 DCS、UDL 和全部 UD 字节。
    raw = bytes.fromhex(part.pdu)
    address_end = 5 + (raw[3] + 1) // 2
    return (b"\x00" + bytes([raw[1] & 0x40]) + raw[3:address_end]
            + raw[address_end:address_end + 2] + bytes.fromhex(timestamp)
            + raw[address_end + 3:]).hex()


@pytest.mark.parametrize("text,count", [
    ("hello", 1), ("a" * 160, 1), ("a" * 161, 2), ("a" * 307, 3),
    ("中" * 70, 1), ("中" * 71, 2), ("中" * 140, 3), ("中" * 141, 3),
    ("Hello中文" * 30, 4), ("中" * 66 + "😀" + "中" * 10, 2),
    ("😀" * 70, 3), ("a" * 152 + "€[]{}^~|\\" * 10, 3),
    ("a" * 150 + " \n" + "b" * 30, 2), (" 中\n" * 50 + "  ", 3),
    ("\t", 1), ("\x1b", 1), ("", 1),
])
def test_pdu_roundtrip_via_receiver(text, count):
    parts = encode_submit("+8613800000000", text, reference=42)
    assert len(parts) == count
    concat = SmsConcat()
    delivered = []
    for part in reversed(parts):
        message = decode_deliver(as_deliver(part))
        assert message is not None
        assert "\ufffd" not in message.body
        assert len(bytes.fromhex(part.pdu)) - 1 == part.tpdu_length
        result = concat.add(message, now=0)
        if result is not None:
            delivered.append(result)
    assert len(delivered) == 1
    assert delivered[0].message.body == text


def test_pdu_structure_and_known_gsm7_packing():
    # 独立工具 python-gsmmodem 0.9 的输出；占位号码、24 小时有效期、不请求状态报告。
    # 来源：https://github.com/faucamp/python-gsmmodem/blob/master/gsmmodem/pdu.py
    # encodeSmsSubmitPdu("+8613800000000", "hellohello", validity=timedelta(hours=24),
    #                    requestStatusReport=False)
    known = "0011000D91683108000000F00000A70AE8329BFD4697D9EC37"
    assert encode_submit("+8613800000000", "hellohello")[0].pdu == known
    part = encode_submit("10086", "hellohello")[0]
    assert part.pdu == "00110005810180F60000A70AE8329BFD4697D9EC37"
    for text, dcs, udl in [("a" * 161, 0, 160), ("中" * 71, 8, 140)]:
        raw = bytes.fromhex(encode_submit("+8613800000000", text, reference=255)[0].pdu)
        assert raw[:5] == bytes([0, 0x51, 0, 13, 0x91])
        assert raw[12:16] == bytes([0, dcs, 167, udl])
        assert raw[16:22] == bytes([5, 0, 3, 255, 2, 1])
        assert len(raw[16:]) <= 140
        if dcs == 0:
            assert raw[22] & 1 == 0  # 六字节 UDH 后有一个填充位。


def test_concat_16bit_and_unknown_iei():
    ud = bytes.fromhex("09080412340201700100") + "你好".encode("utf-16-be")
    pdu = "004005810180F6000862010321000000" + bytes([len(ud)]).hex() + ud.hex()
    message = decode_deliver(pdu)
    assert message is not None
    assert message.concat == ConcatInfo(0x1234, 2, 1)
    assert message.body == "你好"


def test_concat_out_of_order_duplicate_and_timestamp():
    parts = encode_submit("10086", "中" * 71, reference=7)
    first = decode_deliver(as_deliver(parts[0]))
    second = decode_deliver(as_deliver(parts[1], timestamp="62010322000000"))
    assert first is not None and second is not None
    concat = SmsConcat()
    assert concat.add(second, now=0) is None
    assert concat.add(second, now=1) is None
    result = concat.add(first, now=2)
    assert result is not None and result.message.body == "中" * 71
    assert result.message.timestamp == first.timestamp
    assert concat.add(first, now=3) is None
    assert concat.add(second, now=3) is None
    assert concat.flush_expired(now=700) == []


def test_concat_timeout_marks_missing_start_middle_end():
    message = decode_deliver(as_deliver(encode_submit("10086", "part")[0]))
    assert message is not None and message.concat is None
    concat = SmsConcat(timeout=10)
    assert concat.add(replace(message, concat=ConcatInfo(1, 5, 2)), now=0) is None
    assert concat.add(replace(message, concat=ConcatInfo(1, 5, 4)), now=1) is None
    assert concat.flush_expired(now=9) == []
    result = concat.flush_expired(now=10)
    assert result[0].message.body == MISSING_PART + "part" + MISSING_PART + "part" + MISSING_PART
    assert concat.flush_expired(now=11) == []


@pytest.mark.parametrize("pdu", ["", "ZZ", "00", "0000", "001100", "000405810180F6000862010321000000044F60"])
def test_pdu_rejects_truncated_or_non_deliver(pdu):
    assert decode_deliver(pdu) is None


@pytest.mark.parametrize("number", ["", "+", "10086\rAT", "abc", "1" * 21])
def test_pdu_rejects_invalid_number(number):
    with pytest.raises(ValueError):
        encode_submit(number, "hi")


def test_pdu_smsc_and_alphanumeric_sender():
    # 发件方为 GSM-7 编码的 ABC；地址长度为有用半字节数，而不是 septet 数。
    message = decode_deliver("039121F30006D041E1100000620103210000000341E110")
    assert message is not None
    assert message.sender == "ABC"
    assert message.body == "ABC"
    assert message.concat is None


@pytest.mark.parametrize("header", ["050003010200", "050003010203", "050004010201", "07080412340200"])
def test_pdu_rejects_invalid_concat_header(header):
    ud = bytes.fromhex(header) + b"\x00A"
    assert decode_deliver("004005810180F6000862010321000000" + bytes([len(ud)]).hex() + ud.hex()) is None


def test_concat_isolates_senders_and_references():
    concat = SmsConcat()
    messages = []
    for number, ref in [("10086", 1), ("10001", 1), ("10086", 2)]:
        messages.append([decode_deliver(as_deliver(part)) for part in encode_submit(number, "中" * 71, ref)])
    for first, _ in messages:
        assert concat.add(first, now=0) is None
    for _, second in reversed(messages):
        assert concat.add(second, now=1).message.body == "中" * 71


def test_pdu_whitespace_stays_at_end_of_previous_segment():
    text = "a" * 150 + " \n" + "b" * 30
    first, second = [decode_deliver(as_deliver(part)) for part in encode_submit("10086", text)]
    assert first.body == "a" * 150 + " \n"
    assert second.body == "b" * 30


def test_pdu_rejects_excessive_segment_count():
    with pytest.raises(ValueError, match="255"):
        encode_submit("10086", "中" * (255 * 67 + 1))
