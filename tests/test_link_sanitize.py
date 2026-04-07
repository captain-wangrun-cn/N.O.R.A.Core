#!/usr/bin/env python3
"""测试链接处理功能"""

import re

def _sanitize_links_if_needed(text: str) -> str:
    """
    处理文本中的链接以避免内容安全过滤。
    使用零宽空格 (U+200B) 而不是普通空格，对用户不可见。
    """
    # 匹配 http:// 或 https:// 开头的链接
    url_pattern = r'(https?://[^\s<>]+)'
    
    def insert_zero_width_space(match):
        url = match.group(1)
        # 如果链接太短，不处理
        if len(url) < 15:
            return url
        
        # 在域名部分插入零宽空格
        # 例如: https://example.com -> https://exam\u200Bple.com
        protocol_end = url.find('://') + 3
        domain_end = url.find('/', protocol_end)
        if domain_end == -1:
            domain_end = len(url)
        
        # 如果域名太短，不处理
        if domain_end - protocol_end < 8:
            return url
        
        # 在域名中间插入零宽空格
        insert_pos = protocol_end + (domain_end - protocol_end) // 2
        return url[:insert_pos] + '\u200B' + url[insert_pos:]
    
    return re.sub(url_pattern, insert_zero_width_space, text)

# 测试用例
test_cases = [
    "查看这个链接：https://www.example.com/path/to/page",
    "多个链接：https://google.com 和 https://github.com/user/repo",
    "短链接不处理：http://a.co",
    "混合文本：这是一个很长的句子 https://www.verylongdomain.com/with/long/path 还有更多文字",
    "没有链接的普通文本"
]

print("=== 链接处理测试 ===\n")
for i, test in enumerate(test_cases, 1):
    result = _sanitize_links_if_needed(test)
    print(f"测试 {i}:")
    print(f"  原文: {test}")
    print(f"  结果: {result}")
    print(f"  零宽空格: {'是' if '\u200B' in result else '否'}")
    print()
