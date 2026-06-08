from __future__ import annotations

import re
from typing import List

from .constants import TG_MSG_MAX_LENGTH


def _markdown_to_html(text: str) -> str:
    """
    将 Markdown 格式转为 Telegram 兼容的 HTML。
    
    处理策略：先保护代码块和行内代码，转义特殊字符，再做格式转换，最后还原代码。
    """
    # === Phase 0: 提取并保护代码块和行内代码（避免内部内容被错误转换） ===
    code_blocks = []
    inline_codes = []
    
    def _save_code_block(m):
        idx = len(code_blocks)
        lang = m.group(1) or ""
        code = m.group(2)
        # 代码块内部需要转义 HTML 特殊字符
        escaped_code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        if lang:
            code_blocks.append(f'<pre><code class="language-{lang}">{escaped_code}</code></pre>')
        else:
            code_blocks.append(f'<pre><code>{escaped_code}</code></pre>')
        return f'\x00CODEBLOCK{idx}\x00'
    
    def _save_inline_code(m):
        idx = len(inline_codes)
        code = m.group(1)
        escaped_code = code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        inline_codes.append(f'<code>{escaped_code}</code>')
        return f'\x00INLINECODE{idx}\x00'
    
    # 先保护代码块 (```...```)
    text = re.sub(r'```(\w*)\n(.*?)```', _save_code_block, text, flags=re.DOTALL)
    # 再保护行内代码 (`...`)
    text = re.sub(r'`([^`\n]+?)`', _save_inline_code, text)
    
    # === Phase 1: 转义 HTML 特殊字符（在非代码区域） ===
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    
    # === Phase 2: Markdown → HTML 格式转换 ===
    # 粗体+斜体: ***text***
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text)
    # 粗体: **text**
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # 粗体: __text__
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    # 斜体: *text* (不匹配乘号或路径中的星号)
    text = re.sub(r'(?<!\w)\*([^*\n]+?)\*(?!\w)', r'<i>\1</i>', text)
    # 删除线: ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    # 链接: [text](url)
    text = re.sub(r'\[([^\]]+?)\]\((https?://[^\)]+?)\)', r'<a href="\2">\1</a>', text)
    # 标题: # Title → 加粗（Telegram 不支持 h1-h6）
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # 引用: > text → blockquote
    # 匹配连续的 > 行，合并为一个 blockquote
    text = re.sub(
        r'(^&gt;\s?.+(?:\n^&gt;\s?.+)*)',
        lambda m: '<blockquote>' + re.sub(r'^&gt;\s?', '', m.group(0), flags=re.MULTILINE) + '</blockquote>',
        text, flags=re.MULTILINE
    )
    
    # === Phase 3: 还原代码块和行内代码 ===
    for idx, block in enumerate(code_blocks):
        text = text.replace(f'\x00CODEBLOCK{idx}\x00', block)
    for idx, code in enumerate(inline_codes):
        text = text.replace(f'\x00INLINECODE{idx}\x00', code)
    
    return text


def _split_long_text(text: str, max_length: int = TG_MSG_MAX_LENGTH) -> List[str]:
    """
    将超长文本按 Telegram 限制分割成多条消息。
    优先在段落边界分割，其次在行边界，最后在字符边界。
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    remaining = text
    
    while len(remaining) > max_length:
        # 在 max_length 范围内找最佳分割点
        split_at = max_length
        
        # 优先：在段落边界 (\n\n) 分割
        para_break = remaining.rfind('\n\n', 0, max_length)
        if para_break > max_length // 4:  # 至少用到 1/4 的空间
            split_at = para_break + 2  # 包含 \n\n
        else:
            # 其次：在行边界 (\n) 分割
            line_break = remaining.rfind('\n', 0, max_length)
            if line_break > max_length // 4:
                split_at = line_break + 1
            # 最后：硬切
        
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip('\n')
    
    if remaining.strip():
        chunks.append(remaining.strip())
    
    return chunks if chunks else [text]
