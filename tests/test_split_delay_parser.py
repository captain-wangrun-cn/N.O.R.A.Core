import re


_SPLIT_MARKER_PATTERN = re.compile(r"\[SPLIT(?::([0-9.]+))?\]", re.IGNORECASE)


def _simulate_scheduler_split_messages(text: str):
    """模拟 scheduler_mixin 的 split 解析，仅返回将被发送的文本片段。"""
    parts = _SPLIT_MARKER_PATTERN.split(text)
    sent = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            t = part.strip()
            if t:
                sent.append(t)
        else:
            # delay 槽位，不应作为消息发送
            _ = part
    return sent


def test_split_delay_token_not_sent_as_message():
    text = "欢迎回家[SPLIT:1.5]第二段"
    sent = _simulate_scheduler_split_messages(text)
    assert sent == ["欢迎回家", "第二段"]


def test_multiple_split_delay_tokens_not_sent():
    text = "A[SPLIT:2]B[SPLIT:0.3]C"
    sent = _simulate_scheduler_split_messages(text)
    assert sent == ["A", "B", "C"]


def test_non_standard_split_tokens_are_not_parsed():
    text = "甲[SPILIT:1.2]乙[SPLIT：0.5]丙"
    sent = _simulate_scheduler_split_messages(text)
    assert sent == ["甲[SPILIT:1.2]乙[SPLIT：0.5]丙"]
