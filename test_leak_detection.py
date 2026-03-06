"""Test tool call leak detection and cleaning functions."""
import re

# === Copy the leak detection code directly for standalone testing ===
_TOOL_NAMES = r'(?:execute_skill|execute_tool_plan|execute_tool|write_file|read_file|edit_file|exec_command|list_dir|create_new_skill|search|get_available_skills|delegate_to_coder)'

_TOOL_LEAK_PATTERNS = [
    re.compile(_TOOL_NAMES + r"""\s*\(""", re.IGNORECASE),
    re.compile(r'</?(?:execute_skill|execute_tool|tool_call|function_call)\b', re.IGNORECASE),
    re.compile(r"""execute_tool\s*\(\s*['"]""", re.IGNORECASE),
    re.compile(r"""['"](?:old_code|new_code|file_path|args_json)['"]\s*:\s*['"]""", re.IGNORECASE),
]


def _is_tool_call_leak(text: str) -> bool:
    for pattern in _TOOL_LEAK_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _clean_tool_call_leaks(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r'<execute_skill>[\s\S]*?</execute_skill>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<execute_tool>[\s\S]*?</execute_tool>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'</?(?:execute_skill|execute_tool|tool_call|function_call)\b[^>]*>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"""execute_tool\s*\(\s*['"][^'"]*['"]\s*,\s*\{[^}]*\}\s*\)""", '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(_TOOL_NAMES + r"""\s*\([^)]*\)""", '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'返回结果[：:]\s*```[\s\S]*?```', '', cleaned)
    cleaned = re.sub(r"""\{\s*['"](?:file_path|old_code|new_code|path|args_json)['"][\s\S]*?\}""", '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned

# Test cases: (text, expected_is_leak)
test_cases = [
    # Should be detected as leaks
    ('execute_tool("edit_file", {"path": "SOUL.md", "old_code": "hello", "new_code": "world"})', True),
    ('<execute_skill>\n["edit_file", {"file_path": "SOUL.md", "old_code": "## 核心准则", "new_code": "## 核心准则\\n\\n成为主人的依靠"}]\n</execute_skill>', True),
    ('edit_file(path="test.py", old_code="x", new_code="y")', True),
    ('execute_skill("web_search", \'{"query": "test"}\')', True),
    ('write_file("SOUL.md", "content here")', True),
    ('read_file("config.py")', True),
    ('execute_tool(\'edit_file\', {\'path\': \'SOUL.md\', \'old_code\': \'1. **可靠\', \'new_code\': \'1. **可靠与依赖\'})', True),
    
    # Should NOT be detected as leaks (normal text)
    ('你好主人，我来帮你处理~', False),
    ('让我看看文件内容', False),
    ('已经完成修改了，主人 ✨', False),
    ('我使用了 edit_file 工具来修改文件', False),  # Mention tool name but not actual call syntax
    ('文件 SOUL.md 已经更新完毕', False),
]

print("=== Testing _is_tool_call_leak() ===")
all_pass = True
for text, expected in test_cases:
    result = _is_tool_call_leak(text)
    status = "✅" if result == expected else "❌"
    if result != expected:
        all_pass = False
    print(f"{status} Expected={expected}, Got={result} | {text[:80]}")

print()
print("=== Testing _clean_tool_call_leaks() ===")

# Test cleaning
leak_text1 = """好的，主人。

我这就将您的期望，铭刻到灵魂深处。

execute_tool('edit_file', {'path': 'SOUL.md', 'old_code': '1. **可靠 (Reliable):** 能力至上，主动预判，稳重冷静。', 'new_code': '1. **可靠与依靠 (Reliable & Dependable):** 能力至上，主动预判，稳重冷静。永远是主人最坚实、最值得信赖的后盾与依靠。'})

已经完成了，主人。"""

cleaned1 = _clean_tool_call_leaks(leak_text1)
print(f"Input length: {len(leak_text1)}, Cleaned length: {len(cleaned1)}")
print(f"Cleaned:\n{cleaned1}")
print()

leak_text2 = """<execute_skill>
[
    "edit_file",
    {
        "file_path": "SOUL.md",
        "old_code": "## 核心准则",
        "new_code": "## 核心准则\\n\\n成为主人的依靠 (Be the master's support)。"
    }
]
</execute_skill>"""

cleaned2 = _clean_tool_call_leaks(leak_text2)
print(f"Input length: {len(leak_text2)}, Cleaned length: {len(cleaned2)}")
print(f"Cleaned: '{cleaned2}'")
print()

if all_pass:
    print("🎉 All leak detection tests passed!")
else:
    print("⚠️ Some tests failed!")
