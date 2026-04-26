from pathlib import Path
import re


def test_followup_end_branch_has_grace_delay_after_wrapup():
    """当 followup 决策为 END 且发送结束语时，应先等待 5 秒再收尾。"""
    source = Path("core/scheduler_mixin.py").read_text(encoding="utf-8")

    pattern = re.compile(
        r'elif\s+decision\s*==\s*"END"\s*:\s*'
        r'(?:.|\n)*?if\s+count\s*>?=\s*2\s*:\s*'
        r'(?:.|\n)*?await\s+self\._send_wrapup_message\(chat_id\)\s*'
        r'(?:.|\n)*?await\s+asyncio\.sleep\(self\.FOLLOWUP_END_GRACE_SECONDS\)\s*'
        r'(?:.|\n)*?self\._transition_to_semi_online\(chat_id\)',
        re.MULTILINE,
    )

    assert pattern.search(source), "END 分支应在发送结束语后等待 FOLLOWUP_END_GRACE_SECONDS 再结束会话"
