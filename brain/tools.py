# brain/tools.py

import os
import json
import re
import glob
import subprocess
import logging
import inspect
from typing import List, Dict, Callable, Any, Optional
from adapters.base import BaseAdapter
from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

# --- Workspace & Security Constants ---
# 获取工作区管理器，会自动初始化工作区
workspace_manager = get_workspace_manager()
WORKSPACE_ROOT = workspace_manager.root
SKILLS_DIR = workspace_manager.skills_dir
DOWNLOADS_DIR = workspace_manager.downloads_dir

# Files that LLM should NEVER read (contain secrets)
SENSITIVE_FILES = {"config.yml", "config.yaml", ".env", ".env.local"}
# Dangerous command patterns
BLOCKED_COMMANDS = ["rm -rf /", "mkfs", "dd if=", "> /dev/", "shutdown", "reboot", "passwd"]

SKILL_MAIN_PY_TEMPLATE = """\"\"\"
{description}
\"\"\"
import argparse
import json
import os
import sys

# 工作区路径（由 execute_skill 自动注入，无需手动设置）
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT", ".")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", os.path.join(WORKSPACE_ROOT, "downloads"))

def run(**kwargs):
    \"\"\"Main logic for the skill. Implement your code here.\"\"\"
    # TODO: Replace this placeholder with real implementation!
    # kwargs contains the arguments passed from the command line.
    # Example: kwargs = {"keyword": "blue archive", "limit": "5"}
    #
    # 文件下载示例:
    #   save_path = os.path.join(DOWNLOADS_DIR, "my_file.jpg")
    #   os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    #
    print(json.dumps({"status": "error", "message": "This skill has not been implemented yet. Please write the real code."}))
    return 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="{description}")
    # TODO: Define your arguments here. Example:
    # parser.add_argument("--keyword", required=True, help="Search keyword")
    # parser.add_argument("--limit", type=int, default=3, help="Max results")
    
    args, unknown = parser.parse_known_args()
    result = run(**vars(args))
    sys.exit(result if isinstance(result, int) else 0)
"""

class ToolManager:
    """
    Manages and executes all available tools using a dual-layer system.
    """
    def __init__(self, adapter: BaseAdapter):
        self._tools: Dict[str, Callable] = {}
        self._schemas: List[Dict[str, Any]] = []
        self.adapter = adapter
        self._register_tools()

    def _register_tools(self):
        self.register(self.create_new_skill)
        self.register(self.execute_skill)
        self.register(self.execute_tool_plan)
        self.register(self.read_file)
        self.register(self.search)
        self.register(self.write_file)
        self.register(self.edit_file)
        self.register(self.list_dir)
        self.register(self.get_available_skills)
        self.register(self.exec_command)

    def register(self, func: Callable):
        self._tools[func.__name__] = func
        self._schemas.append(self._generate_schema(func))

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return self._schemas

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name not in self._tools: return f"Error: Tool '{name}' not found."
        try:
            logger.info(f"Executing tool: {name} with args: {args}")
            # Filter out 'kwargs' if it exists in args to prevent common LLM mapping errors
            filtered_args = {k: v for k, v in args.items() if k != 'kwargs'}
            func = self._tools[name]
            result = await func(**filtered_args) if inspect.iscoroutinefunction(func) else func(**filtered_args)
            return str(result)
        except Exception as e:
            logger.error(f"Tool execution error for {name}: {e}", exc_info=True)
            return f"Error executing {name}: {str(e)}"

    async def execute_tool_plan(
        self,
        plan_json: str,
        stop_on_error: bool = True,
        dedupe_successful: bool = True,
        max_steps: int = 20,
    ) -> str:
        """
        Executes multiple tools sequentially in ONE call, with detailed success/failure/skip logs.

        :param plan_json: JSON array of steps. Example: '[{"name":"search","args":{"query":"ToolManager"}}, {"name":"read_file","args":{"path":"brain/tools.py","start_line":1,"end_line":60}}]'
        :param stop_on_error: If true, stop immediately when one step fails and return failure detail.
        :param dedupe_successful: If true, skip duplicated calls that already succeeded in this plan.
        :param max_steps: Safety limit for plan length (1-50).
        """
        try:
            max_steps = max(1, min(int(max_steps), 50))
            steps = json.loads(plan_json)
        except json.JSONDecodeError as e:
            return f"Error: invalid plan_json: {e}"
        except Exception as e:
            return f"Error: invalid execute_tool_plan parameters: {e}"

        if not isinstance(steps, list):
            return "Error: plan_json must be a JSON array of tool steps."
        if not steps:
            return "Error: empty tool plan."

        if len(steps) > max_steps:
            steps = steps[:max_steps]

        success_signatures = set()
        details = []
        success_count = 0
        failed_count = 0
        skipped_count = 0
        aborted = False

        def _preview(text: str, limit: int = 500) -> str:
            if len(text) <= limit:
                return text
            return text[:limit] + f" ... [truncated {len(text) - limit} chars]"

        for i, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                failed_count += 1
                details.append({
                    "step": i,
                    "status": "failed",
                    "tool": "<invalid>",
                    "error": "Step must be an object with fields: name, args",
                })
                if stop_on_error:
                    aborted = True
                    break
                continue

            tool_name = step.get("name")
            tool_args = step.get("args", {})
            if not isinstance(tool_name, str) or not tool_name:
                failed_count += 1
                details.append({
                    "step": i,
                    "status": "failed",
                    "tool": "<invalid>",
                    "error": "Missing or invalid tool name.",
                })
                if stop_on_error:
                    aborted = True
                    break
                continue

            if tool_name == "execute_tool_plan":
                failed_count += 1
                details.append({
                    "step": i,
                    "status": "failed",
                    "tool": tool_name,
                    "error": "Nested execute_tool_plan is not allowed.",
                })
                if stop_on_error:
                    aborted = True
                    break
                continue

            if not isinstance(tool_args, dict):
                failed_count += 1
                details.append({
                    "step": i,
                    "status": "failed",
                    "tool": tool_name,
                    "error": "Tool args must be an object (dict).",
                })
                if stop_on_error:
                    aborted = True
                    break
                continue

            signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, ensure_ascii=False)}"
            if dedupe_successful and signature in success_signatures:
                skipped_count += 1
                details.append({
                    "step": i,
                    "status": "skipped_duplicate_success",
                    "tool": tool_name,
                    "args": tool_args,
                    "reason": "Same tool+args already succeeded earlier in this plan.",
                })
                continue

            result = await self.execute(tool_name, tool_args)
            result_lower = result.lower().strip()
            is_failed = (
                result_lower.startswith("error")
                or result_lower.startswith("refused")
                or " failed" in result_lower
            )

            if is_failed:
                failed_count += 1
                details.append({
                    "step": i,
                    "status": "failed",
                    "tool": tool_name,
                    "args": tool_args,
                    "result_preview": _preview(result),
                })
                if stop_on_error:
                    aborted = True
                    break
            else:
                success_count += 1
                success_signatures.add(signature)
                details.append({
                    "step": i,
                    "status": "success",
                    "tool": tool_name,
                    "args": tool_args,
                    "result_preview": _preview(result),
                })

        payload = {
            "type": "tool_plan_result",
            "summary": {
                "total_steps": len(steps),
                "executed_steps": success_count + failed_count,
                "success": success_count,
                "failed": failed_count,
                "skipped": skipped_count,
                "aborted": aborted,
                "stop_on_error": bool(stop_on_error),
                "dedupe_successful": bool(dedupe_successful),
            },
            "steps": details,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def create_new_skill(self, skill_name: str, description: str) -> str:
        """
        Creates a complete boilerplate for a new skill. This is the preferred way to create skills.
        :param skill_name: The name of the new skill (e.g., 'web_search').
        :param description: A one-sentence description of what the new skill does.
        """
        logger.info(f"Request to create new skill: {skill_name}")
        skill_dir = os.path.join("skills", skill_name)
        if os.path.exists(skill_dir):
            return f"Error: Skill '{skill_name}' already exists. Please choose a different name."
        try:
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), 'w', encoding='utf-8') as f:
                f.write(f"---\nname: {skill_name}\ndescription: {description}\n---\n\n# {skill_name}\n\n{description}\n")
            with open(os.path.join(skill_dir, "__init__.py"), 'w', encoding='utf-8') as f: pass
            with open(os.path.join(skill_dir, "main.py"), 'w', encoding='utf-8') as f:
                # Use a simple placeholder replacement to avoid str.format interpreting template braces
                rendered_template = SKILL_MAIN_PY_TEMPLATE.replace("{description}", description)
                f.write(rendered_template)
            feedback = (
                f"Successfully created skill boilerplate for '{skill_name}' at '{skill_dir}'.\n\n"
                f"⚠️ IMPORTANT: The skill is NOT functional yet! The main.py contains only TEMPLATE code.\n"
                f"You MUST now use write_file('{os.path.join(skill_dir, 'main.py')}', '<real code>') to write the actual implementation.\n"
                f"The script must:\n"
                f"  1. Use argparse to parse command-line arguments (--key value format)\n"
                f"  2. Print results to stdout (so execute_skill can capture them)\n"
                f"  3. Have an 'if __name__ == \"__main__\":' entry point\n"
                f"  4. Use os.getenv('WORKSPACE_ROOT') for workspace root path (currently: {WORKSPACE_ROOT})\n"
                f"  5. Use os.getenv('DOWNLOADS_DIR') for download directory (currently: {DOWNLOADS_DIR})\n"
                f"  6. NEVER hardcode paths like '/root/xxx' — always use the environment variables above!\n\n"
                f"After writing the code, also update '{os.path.join(skill_dir, 'SKILL.md')}' with the correct usage docs.\n"
                f"Do NOT call execute_skill until the real code is written."
            )
            return feedback
        except Exception as e: return f"An unexpected error occurred: {e}"

    def execute_skill(self, skill_name: str, args_json: str = "{}") -> str:
        """
        Executes a specific, pre-existing skill. This is the ONLY safe way to run skill scripts.
        :param skill_name: The name of the skill directory (e.g., 'pixiv_manager').
        :param args_json: A JSON string of arguments for the skill (e.g., '{"keyword": "blue archive"}').
        """
        skill_script_path = os.path.join("skills", skill_name, "main.py")
        if not os.path.exists(skill_script_path):
            return f"Error: Skill '{skill_name}' not found. Use get_available_skills to see what's available."
        
        # Pre-flight: refuse to execute template/placeholder code
        try:
            with open(skill_script_path, 'r', encoding='utf-8') as f:
                code_content = f.read()
            _template_markers = ["TODO: Replace this placeholder", "has not been implemented yet", "Skill executed successfully"]
            if any(marker in code_content for marker in _template_markers):
                return (
                    f"REFUSED: '{skill_name}' still contains template code and cannot be executed.\n"
                    f"Action required: use write_file('{skill_script_path}', '<real code>') to write the implementation first."
                )
        except Exception:
            pass
        
        # Load skill-specific config if it exists
        env = os.environ.copy()
        # 始终注入工作区路径，让技能代码可以正确定位文件
        env["WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
        env["DOWNLOADS_DIR"] = str(DOWNLOADS_DIR)
        env["SKILLS_DIR"] = str(SKILLS_DIR)
        config_path = os.path.join("skills", skill_name, "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    skill_config = json.load(f)
                    if isinstance(skill_config, dict):
                        # Inject config values as environment variables
                        for k, v in skill_config.items():
                            env[str(k)] = str(v)
                    else:
                        return f"Error: {config_path} must be a valid JSON object."
            except json.JSONDecodeError as e:
                return f"Error: Invalid JSON in {config_path}: {e}"
            except Exception as e:
                return f"Error reading config for '{skill_name}': {e}"

        try:
            args_dict = json.loads(args_json)
            command = ["python3", skill_script_path]
            for key, value in args_dict.items():
                command.extend([f"--{key}", str(value)])
            
            result = subprocess.run(command, capture_output=True, text=True, timeout=120, env=env)
            output = result.stdout.strip()
            stderr = result.stderr.strip()
            
            if stderr:
                output += f"\n[STDERR]\n{stderr}"
            
            if result.returncode != 0:
                return f"Skill '{skill_name}' failed (exit code {result.returncode}).\n{output}" if output else f"Skill '{skill_name}' failed with no output."
            
            return output if output else f"Skill '{skill_name}' ran OK but produced no output. Check if it prints results."
        except subprocess.TimeoutExpired:
            return f"Error: Skill '{skill_name}' timed out after 120 seconds."
        except json.JSONDecodeError as e:
            return f"Error: Invalid args_json: {e}. Format: '{{\"key\": \"value\"}}'."
        except Exception as e:
            return f"Error executing '{skill_name}': {e}"

    def get_available_skills(self) -> str:
        """Lists all available skills that N.O.R.A. Core can use."""
        skills_dir = "skills/"
        if not os.path.exists(skills_dir): return "Error: Skills directory not found."
        try:
            skill_folders = [d for d in os.listdir(skills_dir) if os.path.isdir(os.path.join(skills_dir, d)) and not d.startswith('__')]
            if not skill_folders: return "No skills are currently available."
            return "Available skills:\\n" + "\\n".join([f"- {s}" for s in skill_folders])
        except Exception as e: return f"Error listing skills: {e}"

    def _resolve_path(self, path: str) -> str:
        """
        统一路径解析：相对路径基于 WORKSPACE_ROOT，绝对路径保持不变。
        这确保 LLM 传入 'USER.md' 时解析到 workspace/USER.md 而非仓库根目录。
        """
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(WORKSPACE_ROOT, path))

    def _is_path_safe(self, path: str) -> tuple:
        """Check if a path is safe to access. Returns (is_safe, reason)."""
        abs_path = self._resolve_path(path)
        basename = os.path.basename(abs_path)
        if basename in SENSITIVE_FILES:
            return False, f"Access denied: '{basename}' contains sensitive data (API keys, tokens). You should not read this file."
        return True, ""

    def list_dir(self, path: str = ".") -> str:
        """
        Lists the contents of a directory. Use this instead of 'exec_command ls'.
        Returns files and subdirectories with type indicators (📁 for dirs, 📄 for files).
        :param path: The directory path to list. Defaults to workspace root '.'.
        """
        try:
            target = self._resolve_path(path)
            if not os.path.isdir(target):
                return f"Error: '{path}' is not a directory."
            entries = sorted(os.listdir(target))
            if not entries:
                return f"Directory '{path}' is empty."
            result_lines = [f"📂 Contents of '{path}':"]
            for entry in entries:
                full = os.path.join(target, entry)
                if entry.startswith('.') or entry.startswith('__'):
                    continue  # Skip hidden and __pycache__ dirs
                if os.path.isdir(full):
                    result_lines.append(f"  📁 {entry}/")
                else:
                    size = os.path.getsize(full)
                    size_str = f"{size}" if size < 1024 else f"{size/1024:.1f}KB" if size < 1048576 else f"{size/1048576:.1f}MB"
                    result_lines.append(f"  📄 {entry} ({size_str})")
            return "\n".join(result_lines)
        except Exception as e:
            return f"Error listing directory: {e}"

    def read_file(self, path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
        """
        Reads the contents of a file. Cannot read sensitive config files (config.yml, .env).
        
        :param path: Path to the file to read.
        :param start_line: Optional. Starting line number (1-indexed, inclusive). If not provided, starts from beginning.
        :param end_line: Optional. Ending line number (1-indexed, inclusive). If not provided, reads to end.
        :return: File contents or specific line range.
        
        Examples:
          read_file("app.py")              → Read entire file
          read_file("app.py", 10, 20)      → Read lines 10-20 (inclusive)
          read_file("app.py", 50)          → Read from line 50 to end
        """
        safe, reason = self._is_path_safe(path)
        if not safe:
            return reason
        try:
            # LLM 可能传入字符串类型的行号，需要强制转换
            if start_line is not None:
                start_line = int(start_line)
            if end_line is not None:
                end_line = int(end_line)
            
            abs_path = self._resolve_path(path)
            with open(abs_path, 'r', encoding='utf-8') as f:
                if start_line is None and end_line is None:
                    # 读取全文
                    return f.read()
                else:
                    # 按行读取
                    lines = f.readlines()
                    total_lines = len(lines)
                    
                    # 处理行号（1-indexed → 0-indexed）
                    start_idx = (start_line - 1) if start_line else 0
                    end_idx = end_line if end_line else total_lines
                    
                    # 边界检查
                    if start_idx < 0:
                        start_idx = 0
                    if end_idx > total_lines:
                        end_idx = total_lines
                    if start_idx >= total_lines:
                        return f"Error: start_line {start_line} exceeds file length ({total_lines} lines)"
                    
                    # 提取指定行范围
                    selected_lines = lines[start_idx:end_idx]
                    result = ''.join(selected_lines)
                    
                    # 添加行号信息帮助 LLM 理解上下文
                    header = f"# Lines {start_idx + 1}-{end_idx} of {total_lines} (File: {path})\n"
                    return header + result
        except Exception as e:
            return f"Error reading file: {e}"

    def search(
        self,
        query: str,
        path: str = ".",
        include_pattern: str = "**/*",
        is_regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 50,
    ) -> str:
        """
        Searches text in files (similar to IDE/Copilot search) and returns matched file/line snippets.

        :param query: Search keyword or regex pattern.
        :param path: Root directory to search in. Defaults to workspace root '.'.
        :param include_pattern: Glob pattern for files, e.g. '**/*.py', 'docs/**/*.md'.
        :param is_regex: Whether query should be treated as a regular expression.
        :param case_sensitive: Whether search is case-sensitive.
        :param max_results: Maximum number of matched lines to return (1-200).
        """
        if not query:
            return "Error: query cannot be empty."

        try:
            max_results = max(1, min(int(max_results), 200))
            target_root = self._resolve_path(path)
            if not os.path.exists(target_root):
                return f"Error: path '{path}' does not exist."

            if os.path.isfile(target_root):
                candidate_files = [target_root]
            else:
                candidate_files = []
                search_pattern = os.path.join(target_root, include_pattern.replace("/", os.sep))
                for abs_file in glob.glob(search_pattern, recursive=True):
                    if os.path.isfile(abs_file):
                        name = os.path.basename(abs_file)
                        rel_path = os.path.relpath(abs_file, target_root).replace("\\", "/")
                        if (
                            name.startswith('.')
                            or name.startswith('__')
                            or rel_path.startswith('.')
                            or "/__" in f"/{rel_path}"
                        ):
                            continue
                        candidate_files.append(abs_file)

                # 兼容 "**/*.py" 在部分平台不匹配根目录文件的情况
                if include_pattern.startswith("**/"):
                    fallback_pattern = include_pattern[3:]
                    fallback_search_pattern = os.path.join(target_root, fallback_pattern.replace("/", os.sep))
                    for abs_file in glob.glob(fallback_search_pattern, recursive=True):
                        if os.path.isfile(abs_file) and abs_file not in candidate_files:
                            candidate_files.append(abs_file)

            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(query, flags) if is_regex else None

            results = []
            scanned_files = 0

            for file_path in candidate_files:
                safe, reason = self._is_path_safe(file_path)
                if not safe:
                    logger.debug(f"Skip sensitive file in search: {file_path} ({reason})")
                    continue

                scanned_files += 1
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        for line_no, line in enumerate(f, start=1):
                            text = line.rstrip("\n")
                            matched = False
                            if is_regex:
                                matched = bool(pattern.search(text)) if pattern else False
                            else:
                                if case_sensitive:
                                    matched = query in text
                                else:
                                    matched = query.lower() in text.lower()

                            if matched:
                                rel = os.path.relpath(file_path, target_root).replace("\\", "/")
                                preview = text.strip()
                                if len(preview) > 220:
                                    preview = preview[:220] + "..."
                                results.append(f"{rel}:{line_no}: {preview}")
                                if len(results) >= max_results:
                                    break
                except UnicodeDecodeError:
                    continue
                except Exception as e:
                    logger.debug(f"Search skipped file '{file_path}': {e}")
                    continue

                if len(results) >= max_results:
                    break

            header = (
                f"Search results for '{query}' in '{path}' "
                f"(pattern='{include_pattern}', regex={is_regex}, case_sensitive={case_sensitive})."
            )

            if not results:
                return f"{header}\nNo matches found. Scanned {scanned_files} file(s)."

            suffix = ""
            if len(results) >= max_results:
                suffix = f"\nReached max_results={max_results}. Narrow query/pattern for more precise results."

            return f"{header}\n" + "\n".join(results) + suffix
        except re.error as e:
            return f"Error: invalid regex pattern: {e}"
        except Exception as e:
            return f"Error searching files: {e}"

    def write_file(self, path: str, content: str) -> str:
        """Writes content to a file, overwriting it."""
        try:
            abs_path = self._resolve_path(path)
            logger.debug(f"write_file called: path={path} -> abs={abs_path}, content_len={len(content)}")
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, 'w', encoding='utf-8') as f: f.write(content)
            logger.info(f"write_file: wrote {len(content)} chars to {abs_path}")
            return f"Successfully wrote to {path}"
        except Exception as e: return f"Error writing file: {e}"

    def edit_file(self, path: str, old_code: str, new_code: str, match_index: Optional[int] = None) -> str:
        """
        Performs a precise find-and-replace on a file.
        Tries exact match first, then falls back to whitespace-normalized matching.
        If old_code matches multiple locations, provide match_index (1-based) to select the exact target.
        :param path: Path to the file to edit.
        :param old_code: The code snippet to find (approximate whitespace is OK).
        :param new_code: The replacement code snippet.
        :param match_index: Optional 1-based index of which matched location to replace when multiple matches exist.
        """
        safe, reason = self._is_path_safe(path)
        if not safe:
            return reason
        try:
            if match_index is not None:
                try:
                    match_index = int(match_index)
                except Exception:
                    return "Error: match_index must be an integer (1-based)."
                if match_index < 1:
                    return "Error: match_index must be >= 1 (1-based)."

            abs_path = self._resolve_path(path)
            logger.debug(f"edit_file called: path={path} -> abs={abs_path}")
            logger.debug(f"edit_file old_code({len(old_code)} chars): {old_code[:200]}{'...' if len(old_code) > 200 else ''}")
            logger.debug(f"edit_file new_code({len(new_code)} chars): {new_code[:200]}{'...' if len(new_code) > 200 else ''}")
            
            with open(abs_path, 'r', encoding='utf-8') as f:
                content = f.read()

            def _format_matches_with_context(matches: list, all_lines: list, max_items: int = 10) -> str:
                """格式化匹配列表，返回带 index/line/context 的可读文本。"""
                lines_out = []
                for item in matches[:max_items]:
                    line_no = item["line_no"]
                    context_start = max(1, line_no - 2)
                    context_end = min(len(all_lines), item["end_line"] + 2)
                    context = "\n".join(all_lines[context_start - 1:context_end])
                    if len(context) > 260:
                        context = context[:260] + "..."
                    lines_out.append(
                        f"  [{item['index']}] line {line_no}-{item['end_line']}:\n"
                        f"{context}"
                    )
                if len(matches) > max_items:
                    lines_out.append(f"  ... and {len(matches) - max_items} more matches")
                return "\n\n".join(lines_out)

            # 1. Try exact match first
            if old_code in content:
                lines = content.split('\n')
                exact_matches = []
                search_start = 0
                exact_idx = 0
                old_line_span = max(1, old_code.count('\n') + 1)
                while True:
                    idx = content.find(old_code, search_start)
                    if idx == -1:
                        break
                    exact_idx += 1
                    line_no = content.count('\n', 0, idx) + 1
                    exact_matches.append({
                        "index": exact_idx,
                        "offset": idx,
                        "line_no": line_no,
                        "end_line": line_no + old_line_span - 1,
                    })
                    search_start = idx + len(old_code)

                if len(exact_matches) > 1 and match_index is None:
                    logger.warning(f"edit_file: old_code matches {len(exact_matches)} exact locations in {abs_path}")
                    return (
                        f"Error: old_code matches {len(exact_matches)} exact locations in {path}. "
                        f"Please call edit_file again with match_index (1-based) to select one target.\n\n"
                        f"Available matches:\n{_format_matches_with_context(exact_matches, lines)}\n\n"
                        f"Example: edit_file(path=\"{path}\", old_code=..., new_code=..., match_index=2)"
                    )

                if match_index is not None:
                    if match_index > len(exact_matches):
                        return (
                            f"Error: match_index {match_index} is out of range. "
                            f"This old_code has {len(exact_matches)} exact match(es) in {path}."
                        )
                    target = exact_matches[match_index - 1]
                else:
                    target = exact_matches[0]

                start = target["offset"]
                end = start + len(old_code)
                new_content = content[:start] + new_code + content[end:]
                if new_content == content:
                    logger.warning(f"edit_file: old_code == new_code (no actual change) for {abs_path}")
                    return f"Warning: edit_file matched old_code in {path}, but new_code is identical — no changes made."
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                logger.info(
                    f"edit_file: exact match succeeded for {abs_path} "
                    f"(selected index={target['index']}, line={target['line_no']})"
                )
                return f"Successfully edited {path}."

            # 2. Fallback: normalized whitespace matching
            import re
            def normalize(s):
                return re.sub(r'\s+', ' ', s).strip()

            norm_old = normalize(old_code)
            lines = content.split('\n')

            # Sliding window over lines to find ALL matches first
            old_line_count = max(1, old_code.count('\n') + 1)
            all_norm_matches = []  # list of (window_size, line_index)
            for window_size in range(max(1, old_line_count - 5), old_line_count + 6):
                for i in range(len(lines) - window_size + 1):
                    window = '\n'.join(lines[i:i + window_size])
                    if normalize(window) == norm_old:
                        all_norm_matches.append((window_size, i))
            
            if all_norm_matches:
                norm_matches = []
                for idx_num, (window_size, i) in enumerate(all_norm_matches, start=1):
                    line_no = i + 1
                    norm_matches.append({
                        "index": idx_num,
                        "line_no": line_no,
                        "end_line": line_no + window_size - 1,
                        "window_size": window_size,
                        "line_index": i,
                    })

                if len(norm_matches) > 1 and match_index is None:
                    logger.warning(f"edit_file: old_code matches {len(norm_matches)} normalized locations in {abs_path}")
                    return (
                        f"Error: old_code matches {len(norm_matches)} locations in {path} (normalized whitespace). "
                        f"Please call edit_file again with match_index (1-based) to select one target.\n\n"
                        f"Available matches:\n{_format_matches_with_context(norm_matches, lines)}\n\n"
                        f"Example: edit_file(path=\"{path}\", old_code=..., new_code=..., match_index=2)"
                    )

                if match_index is not None:
                    if match_index > len(norm_matches):
                        return (
                            f"Error: match_index {match_index} is out of range. "
                            f"This old_code has {len(norm_matches)} normalized match(es) in {path}."
                        )
                    target = norm_matches[match_index - 1]
                else:
                    target = norm_matches[0]

                window_size = target["window_size"]
                i = target["line_index"]
                # Found a unique match with normalized whitespace
                new_lines = lines[:i] + new_code.split('\n') + lines[i + window_size:]
                new_content = '\n'.join(new_lines)
                if new_content == content:
                    logger.warning(f"edit_file: normalized match but no actual change for {abs_path}")
                    return f"Warning: edit_file matched old_code (normalized) in {path}, but result is identical — no changes made."
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                logger.info(
                    f"edit_file: normalized match at lines {i+1}-{i+window_size} for {abs_path} "
                    f"(selected index={target['index']})"
                )
                return f"Successfully edited {path} (matched with normalized whitespace)."

            logger.warning(f"edit_file: old_code not found in {abs_path} (file size: {len(content)} chars)")
            return (
                f"Error: 'old_code' not found in {path} (even after whitespace normalization). "
                f"Tip: Use read_file to get more surrounding context, then retry edit_file with a more specific old_code."
            )
        except Exception as e:
            return f"Error editing file: {e}"

    def exec_command(self, command: str, timeout: int = 60) -> str:
        """
        (DANGEROUS) Executes a general-purpose shell command.
        WARNING: Do NOT use this for tasks that a high-level tool can do.
        Use 'list_dir' to list directories instead of 'ls'. Use 'read_file' to read files instead of 'cat'.
        NEVER use this to run skill scripts — use 'execute_skill' instead.
        For long-running commands (e.g., apt install, pip install), set timeout to a higher value (e.g., 300).
        :param command: The shell command to execute.
        :param timeout: Maximum execution time in seconds (default: 60, max: 600). Use higher values for package installs.
        """
        # Clamp timeout
        timeout = max(10, min(int(timeout), 600))
        
        # Security: block dangerous commands
        cmd_lower = command.lower().strip()
        for blocked in BLOCKED_COMMANDS:
            if blocked in cmd_lower:
                return f"Error: Command blocked for safety reasons."
        # Security: block reading sensitive files via cat/grep/head/tail
        for sensitive in SENSITIVE_FILES:
            if sensitive in command and any(reader in cmd_lower for reader in ["cat ", "head ", "tail ", "less ", "more ", "grep "]):
                return f"Error: Cannot read '{sensitive}' — it contains sensitive data."
        # Redirect: intercept skill script execution and guide to execute_skill
        if re.search(r'python[3]?\s+skills/', cmd_lower):
            # Extract skill name from the command
            skill_match = re.search(r'skills/([^/\s]+)/', command)
            skill_name = skill_match.group(1) if skill_match else "unknown"
            return (
                f"Error: Do NOT use exec_command to run skill scripts. "
                f"Use the 'execute_skill' tool instead.\n"
                f"Example: execute_skill(\"{skill_name}\", '{{\"arg\": \"value\"}}')\n"
                f"The execute_skill tool handles argument parsing, timeout, and error reporting automatically."
            )
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
            
            # Build structured output with exit code
            parts = []
            if result.stdout.strip():
                parts.append(result.stdout.strip())
            if result.stderr.strip():
                parts.append(f"[STDERR]\n{result.stderr.strip()}")
            
            output = "\n".join(parts) if parts else "(no output)"
            
            # Always include exit code for LLM decision-making
            status = "✅ SUCCESS" if result.returncode == 0 else f"❌ FAILED"
            return f"[Exit Code: {result.returncode}] {status}\n{output}"
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout} seconds. For long-running commands (apt install, pip install, etc.), use a higher timeout value, e.g. exec_command(command, timeout=300)."
        except Exception as e: return f"Error executing command: {e}"

    def _generate_schema(self, func: Callable) -> Dict[str, Any]:
        """Generates a JSON schema for a function, including parameter descriptions from docstrings."""
        import typing
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or ""
        
        # 提取函数描述（docstring 第一段，直到空行或 :param）
        desc_lines = []
        for line in doc.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith(":param") or stripped.startswith(":return") or stripped.startswith("Examples:"):
                break
            if not stripped and desc_lines:  # 空行分隔描述和参数
                break
            desc_lines.append(stripped)
        desc = " ".join(desc_lines).strip() or doc.strip().split("\n")[0]
        
        # 提取 :param 注释，构建参数描述映射
        param_descriptions = {}
        for match in re.finditer(r':param\s+(\w+):\s*(.+?)(?=\n\s*:|$)', doc, re.DOTALL):
            param_name = match.group(1)
            param_desc = ' '.join(match.group(2).strip().split())  # 规范化空白
            param_descriptions[param_name] = param_desc
        
        parameters = {"type": "OBJECT", "properties": {}, "required": []}
        for name, param in sig.parameters.items():
            if name == 'self': continue
            
            # 解析类型注解，支持 Optional[X] (即 Union[X, None])
            annotation = param.annotation
            param_type = "STRING"  # 默认
            
            # 处理 Optional[X] → 提取 X
            origin = getattr(annotation, '__origin__', None)
            if origin is typing.Union:
                # Optional[int] == Union[int, None]
                args = [a for a in annotation.__args__ if a is not type(None)]
                if args:
                    annotation = args[0]
            
            if annotation == int:
                param_type = "INTEGER"
            elif annotation == bool:
                param_type = "BOOLEAN"
            elif annotation == float:
                param_type = "NUMBER"
            
            prop = {"type": param_type}
            # 添加参数描述（如果存在）
            if name in param_descriptions:
                prop["description"] = param_descriptions[name]
            
            parameters["properties"][name] = prop
            if param.default == inspect.Parameter.empty:
                parameters["required"].append(name)
        return {"name": func.__name__, "description": desc, "parameters": parameters}

