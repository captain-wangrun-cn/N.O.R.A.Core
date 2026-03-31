# brain/tools.py

import os
import json
import re
import glob
import subprocess
import logging
import inspect
import uuid
import time
import time
from cryptography.fernet import Fernet
from typing import List, Dict, Callable, Any, Optional
from adapters.base import BaseAdapter
from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

# 工具一句话简介（用于注入前脑/后脑提示词）
TOOL_INTROS = {
    "create_new_skill": "在 workspace/skills 下创建新的技能脚手架目录与基础文件；调用前先阅读 workspace/skills/skill_creator/ 下的文档并遵循其规范。",
    "execute_skill": "运行 workspace/skills 下指定技能并返回输出结果，适合调用已实现能力。",
    "execute_tool_plan": "按计划顺序批量执行多个工具步骤，适合多步流水线。",
    "read_file": "读取文件内容，支持按行范围读取，适合查看源码/文档。",
    "search": "在代码库中搜索关键词或模式，定位实现或配置。",
    "write_file": "创建或覆盖写入文件内容，适合新增文件或重写。",
    "edit_file": "基于旧内容对文件做精确修改，适合小范围替换。",
    "list_dir": "列出目录内容，查看文件和子目录，适合确认路径。",
    "get_available_skills": "列出 workspace/skills 下当前可用技能清单，便于选择技能。",
    "exec_command": "执行受限的系统命令，作为无法用高层工具时的兜底；默认工作目录为 workspace 根目录。",
    "view_image": "仅用于检索用户之前发送给 AI 的历史图片（图片记忆库），支持标签语义与 OCR 文本模糊搜索，不用于搜索或生成新图。",
    "crop_image_for_llm": "裁剪图片以便模型分析，用户要求仔细观察图片某个部分时可调用。",
    "report_progress": "向用户发送阶段性进度汇报，适合长任务中途更新。",
    "set_alarm": "设置提醒或倒计时闹钟，适合提醒/日程。",
    "list_alarms": "查看所有未触发的闹钟，适合确认已有提醒。",
    "cancel_alarm": "取消指定闹钟，适合调整已设置提醒。",
    "read_secret_vault": "读取加密的 SECRET.md 私人记事本（仅 AI 自用，不向用户披露）。",
    "write_secret_vault": "写入/追加加密的 SECRET.md 私人记事本，首次调用自动生成密钥并创建文件。",
    "store_progress_note": "（后脑）记录当前步骤与讲解，供前脑/用户查询进度。",
    "get_progress_note": "（前脑/后脑）获取最近记录的步骤与讲解，便于汇报。",
}

# --- Workspace & Security Constants ---
# 获取工作区管理器，会自动初始化工作区
workspace_manager = get_workspace_manager()
WORKSPACE_ROOT = workspace_manager.root
SKILLS_DIR = workspace_manager.skills_dir
DOWNLOADS_DIR = workspace_manager.downloads_dir
DATA_DIR = workspace_manager.data_dir

# 代码仓库根目录（brain/ 的上一级）
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRET_FILE = os.path.join(WORKSPACE_ROOT, "SECRET.md")
SECRET_KEY_FILE = os.path.join(DATA_DIR, "secret.key")
CUSTOM_FILE = os.path.join(WORKSPACE_ROOT, "CUSTOM.md")

# 进度共享存储：chat_id → {"step": str, "explanation": str, "updated_at": float}
_PROGRESS_STORE: Dict[str, Dict[str, Any]] = {}

# Files that LLM should NEVER read (contain secrets)
SENSITIVE_FILES = {"config.yml", "config.yaml", ".env", ".env.local", "SECRET.md", "CUSTOM.md", "secret.key"}
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
    def __init__(self, adapter: BaseAdapter, image_store=None, scheduler=None):
        self._tools: Dict[str, Callable] = {}
        self._schemas: List[Dict[str, Any]] = []
        self.adapter = adapter
        self.image_store = image_store  # memory.image_store.ImageStore 实例
        self.scheduler = scheduler  # core.scheduler.ProactiveScheduler 实例
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
        self.register(self.view_image)
        self.register(self.crop_image_for_llm)
        self.register(self.report_progress)
        self.register(self.read_secret_vault)
        self.register(self.write_secret_vault)
        # 动态闹钟工具（仅在 scheduler 可用时注册）
        if self.scheduler is not None:
            self.register(self.set_alarm)
            self.register(self.list_alarms)
            self.register(self.cancel_alarm)
        # 进度共享工具（后脑写入，前脑可读取）
        self.register(self.store_progress_note)
        self.register(self.get_progress_note)

    def register(self, func: Callable):
        self._tools[func.__name__] = func
        self._schemas.append(self._generate_schema(func))

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return self._schemas

    # ------------------------------------------------------------------
    # 进度共享工具
    # ------------------------------------------------------------------

    def store_progress_note(self, chat_id: str, step: str, explanation: str) -> str:
        """
        记录当前步骤与讲解，供前脑/用户查询。

        :param chat_id: 会话唯一标识（通常是 chat_id 或 user_id）
        :param step: 当前进行到的步骤概述
        :param explanation: 该步骤的详细说明或补充
        :return: 确认信息

        典型用法（后脑调用）：
        - 在长流程中，随时写入最新的步骤描述，便于前脑汇报或用户询问时提供进度。
        - 如果 step/explanation 为空字符串，会使用上一次的值；但建议显式填写。
        """
        now = time.time()
        _PROGRESS_STORE[chat_id] = {
            "step": (step or ""),
            "explanation": (explanation or ""),
            "updated_at": now,
        }
        logger.info(f"[progress] chat={chat_id} step='{step[:80]}'")
        return "Progress stored"

    def get_progress_note(self, chat_id: str) -> str:
        """
        获取最近记录的步骤与讲解。

        :param chat_id: 会话唯一标识（通常是 chat_id 或 user_id）
        :return: JSON 字符串 {"step": str, "explanation": str, "updated_at": timestamp}，若不存在返回说明。

        典型用法（前脑/后脑调用）：
        - 前脑在回复用户时获取最新进度，用于回答“做到哪了/现在在干嘛”。
        - 后脑需要自查最近记录的步骤时调用。
        """
        data = _PROGRESS_STORE.get(chat_id)
        if not data:
            return "No progress recorded yet."
        try:
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return f"Step: {data.get('step', '')}\nExplanation: {data.get('explanation', '')}\nUpdatedAt: {data.get('updated_at', 0)}"

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
        skill_dir = os.path.join(SKILLS_DIR, skill_name)
        if os.path.exists(skill_dir):
            return f"Error: Skill '{skill_name}' already exists. Please choose a different name."
        try:
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), 'w', encoding='utf-8') as f:
                f.write(
                    f"---\n"
                    f"name: {skill_name}\n"
                    f"description: {description}\n"
                    f"version: 1.0.0\n"
                    f"---\n\n"
                    f"# {skill_name}\n\n{description}\n"
                )
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
                f"  5. Use os.getenv('SKILLS_DIR') for skill root path (currently: {SKILLS_DIR})\n"
                f"  6. Use os.getenv('DOWNLOADS_DIR') for download directory (currently: {DOWNLOADS_DIR})\n"
                f"  7. NEVER hardcode paths like '/root/xxx' — always use the environment variables above!\n\n"
                f"After writing the code, also update '{os.path.join(skill_dir, 'SKILL.md')}' with the correct usage docs.\n"
                f"Do NOT call execute_skill until the real code is written."
            )
            return feedback
        except Exception as e: return f"An unexpected error occurred: {e}"

    async def execute_skill(self, skill_name: str, args_json: str = "{}") -> str:
        """
        Executes a specific, pre-existing skill. This is the ONLY safe way to run skill scripts.
        :param skill_name: The name of the skill directory (e.g., 'pixiv_manager').
        :param args_json: A JSON string of arguments for the skill (e.g., '{"keyword": "blue archive"}').
        """
        skill_script_path = os.path.join(SKILLS_DIR, skill_name, "main.py")
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
        env["CODE_ROOT"] = str(CODE_ROOT)
        env["NORA_CONFIG_PATH"] = str(os.path.join(CODE_ROOT, "config.yml"))
        config_path = os.path.join(SKILLS_DIR, skill_name, "config.json")
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
            
            # 使用 asyncio subprocess 避免阻塞事件循环
            import asyncio
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(WORKSPACE_ROOT),
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: Skill '{skill_name}' timed out after 120 seconds."
            
            output = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
            stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
            
            if stderr:
                output += f"\n[STDERR]\n{stderr}"
            
            if proc.returncode != 0:
                return f"Skill '{skill_name}' failed (exit code {proc.returncode}).\n{output}" if output else f"Skill '{skill_name}' failed with no output."
            
            return output if output else f"Skill '{skill_name}' ran OK but produced no output. Check if it prints results."
        except asyncio.TimeoutError:
            return f"Error: Skill '{skill_name}' timed out after 120 seconds."
        except json.JSONDecodeError as e:
            return f"Error: Invalid args_json: {e}. Format: '{{\"key\": \"value\"}}'."
        except Exception as e:
            return f"Error executing '{skill_name}': {e}"

    def get_available_skills(self) -> str:
        """Lists all available skills that N.O.R.A. Core can use."""
        if not os.path.exists(SKILLS_DIR): return f"Error: Skills directory not found at {SKILLS_DIR}."
        try:
            skill_folders = [d for d in os.listdir(SKILLS_DIR) if os.path.isdir(os.path.join(SKILLS_DIR, d)) and not d.startswith('__')]
            if not skill_folders: return "No skills are currently available."
            return "Available skills:\\n" + "\\n".join([f"- {s}" for s in skill_folders])
        except Exception as e: return f"Error listing skills: {e}"

    def _is_path_safe(self, path: str) -> tuple:
        """Check if a path is safe to access. Returns (is_safe, reason)."""
        abs_path = os.path.abspath(path)
        basename = os.path.basename(abs_path)
        if basename in {"SECRET.md", "secret.key"}:
            return False, "Access denied: SECRET vault is encrypted. Use read_secret_vault/write_secret_vault instead."
        if basename == "CUSTOM.md":
            return False, "Access denied: CUSTOM.md 由用户管理，禁止通过文件工具读取/修改。"
        if basename in SENSITIVE_FILES:
            return False, f"Access denied: '{basename}' contains sensitive data (API keys, tokens). You should not read this file."
        return True, ""

    def _resolve_workspace_path(self, path: str) -> str:
        """
        Normalize file-tool path inputs.

        Supported forms:
        - Absolute paths (kept as-is)
        - workspace/xxx (mapped to real WORKSPACE_ROOT)
        - workspace      (mapped to WORKSPACE_ROOT)
        - Relative paths (resolved from WORKSPACE_ROOT)
        """
        raw = str(path or "").strip().strip('"').strip("'")
        if not raw:
            return raw

        normalized = raw.replace("\\", "/")

        # Absolute path
        if os.path.isabs(raw):
            return os.path.abspath(raw)

        # workspace prefix mapping
        if normalized == "workspace":
            return os.path.abspath(str(WORKSPACE_ROOT))
        if normalized.startswith("workspace/"):
            rel = normalized[len("workspace/"):]
            return os.path.abspath(os.path.join(str(WORKSPACE_ROOT), rel))

        # Default relative path behavior: resolve from WORKSPACE_ROOT
        return os.path.abspath(os.path.join(str(WORKSPACE_ROOT), normalized))

    def _get_secret_key(self) -> bytes:
        """Load or generate the symmetric key for SECRET.md encryption."""
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(SECRET_KEY_FILE):
            try:
                with open(SECRET_KEY_FILE, 'rb') as f:
                    key = f.read().strip()
                if key:
                    return key
            except Exception:
                logger.warning("读取 secret.key 失败，将生成新密钥", exc_info=True)

        key = Fernet.generate_key()
        with open(SECRET_KEY_FILE, 'wb') as f:
            f.write(key)
        return key

    def _ensure_secret_file(self, key: bytes):
        """Ensure SECRET.md exists as encrypted content (even if empty)."""
        os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
        if not os.path.exists(SECRET_FILE):
            token = Fernet(key).encrypt(b"")
            with open(SECRET_FILE, 'wb') as f:
                f.write(token)
        elif os.path.isdir(SECRET_FILE):
            raise ValueError("SECRET.md 路径是目录，请删除后重试。")

    def list_dir(self, path: str = ".") -> str:
        """
        Lists the contents of a directory. Use this instead of 'exec_command ls'.
        Returns files and subdirectories with type indicators (📁 for dirs, 📄 for files).
        :param path: The directory path to list. Defaults to workspace root '.'.
        """
        try:
            target = self._resolve_workspace_path(path)
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

    async def report_progress(self, message: str) -> str:
        """
        Send a progress message to the user during long-running tasks. Use this to keep the user informed about what you're doing. The message will be sent immediately. Do NOT overuse — only call when there's meaningful progress to share (e.g. major step completed, important finding, or task taking longer than expected).

        :param message: A short, natural-language progress update to send to the user. Keep it concise and informative.
        """
        # 实际发送逻辑由 controller 拦截处理，此处仅作为 fallback
        return f"Progress reported: {message}"

    def read_secret_vault(self) -> str:
        """Decrypt and read SECRET.md (private to AI)."""
        try:
            key = self._get_secret_key()
            self._ensure_secret_file(key)
            cipher = Fernet(key)
            with open(SECRET_FILE, 'rb') as f:
                token = f.read().strip()
            if not token:
                return "(secret vault is empty)"
            try:
                plaintext = cipher.decrypt(token).decode('utf-8')
            except Exception:
                return "Error: SECRET.md 无法解密（密钥不匹配或文件损坏）。"
            return plaintext if plaintext else "(secret vault is empty)"
        except Exception as e:
            return f"Error reading SECRET.md: {e}"

    def write_secret_vault(self, content: str, append: bool = True, add_timestamp: bool = True) -> str:
        """Encrypt and write notes into SECRET.md (AI-only)."""
        if not isinstance(content, str) or not content.strip():
            return "Error: content is required."
        try:
            key = self._get_secret_key()
            self._ensure_secret_file(key)
            cipher = Fernet(key)

            existing = ""
            if append:
                try:
                    with open(SECRET_FILE, 'rb') as f:
                        token = f.read().strip()
                    if token:
                        existing = cipher.decrypt(token).decode('utf-8')
                except Exception:
                    logger.warning("SECRET.md 解密失败，将重置为新内容。", exc_info=True)
                    existing = ""

            entry = content.strip()
            if add_timestamp:
                from datetime import datetime, timezone

                entry = f"[{datetime.now(timezone.utc).isoformat()}] {entry}"

            new_plain = entry if not existing or not append else existing + "\n\n" + entry
            encrypted = cipher.encrypt(new_plain.encode('utf-8'))
            with open(SECRET_FILE, 'wb') as f:
                f.write(encrypted)
            return f"Secret note saved (append={append}, length={len(entry)})."
        except Exception as e:
            return f"Error writing SECRET.md: {e}"

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
        resolved_path = self._resolve_workspace_path(path)
        safe, reason = self._is_path_safe(resolved_path)
        if not safe:
            return reason
        try:
            # LLM 可能传入字符串类型的行号，需要强制转换
            if start_line is not None:
                start_line = int(start_line)
            if end_line is not None:
                end_line = int(end_line)
            
            with open(resolved_path, 'r', encoding='utf-8') as f:
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
                    header = f"# Lines {start_idx + 1}-{end_idx} of {total_lines} (File: {resolved_path})\n"
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
            target_root = self._resolve_workspace_path(path)
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
            resolved_path = self._resolve_workspace_path(path)
            safe, reason = self._is_path_safe(resolved_path)
            if not safe:
                return reason
            os.makedirs(os.path.dirname(resolved_path), exist_ok=True)
            with open(resolved_path, 'w', encoding='utf-8') as f: f.write(content)
            return f"Successfully wrote to {resolved_path}"
        except Exception as e: return f"Error writing file: {e}"

    def edit_file(self, path: str, old_code: str, new_code: str) -> str:
        """
        Performs a find-and-replace on a file. Tries exact match first, then falls back to
        whitespace-normalized matching. If editing is difficult, consider using 'write_file' to
        overwrite the entire file content instead.
        :param path: Path to the file to edit.
        :param old_code: The code snippet to find (approximate whitespace is OK).
        :param new_code: The replacement code snippet.
        """
        resolved_path = self._resolve_workspace_path(path)
        safe, reason = self._is_path_safe(resolved_path)
        if not safe:
            return reason
        try:
            with open(resolved_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 1. Try exact match first
            if old_code in content:
                new_content = content.replace(old_code, new_code, 1)
                with open(resolved_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                return f"Successfully edited {resolved_path}."

            # 2. Fallback: normalized whitespace matching
            import re
            def normalize(s):
                return re.sub(r'\s+', ' ', s).strip()

            norm_old = normalize(old_code)
            lines = content.split('\n')

            # Sliding window over lines to find the best match
            old_line_count = max(1, old_code.count('\n') + 1)
            # Widen the window search range to handle more varying empty lines/formatting
            for window_size in range(max(1, old_line_count - 5), old_line_count + 6):
                for i in range(len(lines) - window_size + 1):
                    window = '\n'.join(lines[i:i + window_size])
                    if normalize(window) == norm_old:
                        # Found a match with normalized whitespace
                        new_lines = lines[:i] + new_code.split('\n') + lines[i + window_size:]
                        new_content = '\n'.join(new_lines)
                        with open(resolved_path, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                        return f"Successfully edited {resolved_path} (matched with normalized whitespace)."

            return (
                f"Error: 'old_code' not found in {resolved_path} (even after whitespace normalization). "
                f"Tip: Use 'write_file' to overwrite the entire file instead of edit_file."
            )
        except Exception as e:
            return f"Error editing file: {e}"

    async def exec_command(self, command: str, timeout: int = 60) -> str:
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
            # 使用 asyncio subprocess 避免阻塞事件循环
            import asyncio
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE_ROOT),
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: Command timed out after {timeout} seconds. For long-running commands (apt install, pip install, etc.), use a higher timeout value, e.g. exec_command(command, timeout=300)."
            
            output_str = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
            stderr_str = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
            
            # Build structured output with exit code
            parts = []
            if output_str:
                parts.append(output_str)
            if stderr_str:
                parts.append(f"[STDERR]\n{stderr_str}")
            
            output = "\n".join(parts) if parts else "(no output)"
            
            # Always include exit code for LLM decision-making
            status = "✅ SUCCESS" if proc.returncode == 0 else f"❌ FAILED"
            return f"[Exit Code: {proc.returncode}] {status}\n{output}"
        except asyncio.TimeoutError:
            return f"Error: Command timed out after {timeout} seconds. For long-running commands (apt install, pip install, etc.), use a higher timeout value, e.g. exec_command(command, timeout=300)."
        except Exception as e: return f"Error executing command: {e}"

    def view_image(
        self,
        image_id: str = "",
        keyword: str = "",
        text_query: str = "",
        start_time: str = "",
        end_time: str = "",
        user_id: str = "",
        limit: int = 10,
        return_image: bool = True,
        use_image_model: bool = False,
        local_path: str = "",
    ) -> str:
        """
        Retrieves images from the image memory database. Supports multiple search modes:
        1. By image_id: exact lookup (e.g. 'img_a1b2c3d4')
        2. By keyword: semantic/text search on image tags (e.g. '猫咪', 'sunset beach')
        3. By text_query: fuzzy search on OCR text extracted from images (e.g. 'Hello World', '购物清单'). Supports search-engine-like fuzzy matching — partial matches, case-insensitive, multi-keyword AND logic.
        4. By time range: filter by Unix timestamp (start_time/end_time)
        5. If no parameters given: returns the most recent images.
        text_query and keyword can be used together for combined results.
        :param image_id: Exact image ID to look up (e.g. 'img_a1b2c3d4').
        :param keyword: Keyword or description for semantic/text search on image tags.
        :param text_query: Search text content (OCR) extracted from images. Supports fuzzy matching, case-insensitive, multi-keyword AND logic (space-separated). E.g. 'error message', '购物 清单'.
        :param start_time: Start of time range as Unix timestamp string (e.g. '1709856000').
        :param end_time: End of time range as Unix timestamp string (e.g. '1709942400').
        :param user_id: Filter by user ID. If empty, searches all users.
        :param limit: Maximum number of results (1-50, default 10).
        :param return_image: When true, the tool returns image references via `[image: abs_path]`
            MediaTags，供多模态管线自动读取图片内容。Default: True.
        :param use_image_model: When true, downstream should switch to the image model
            for subsequent reasoning until a crop_image_for_llm round finishes.
        :param local_path: Optional local file path to view directly (workspace-relative or absolute).
            When provided, bypasses DB search and returns the file with MediaTag if exists.
        """
        if not self.image_store:
            return "Error: Image memory system is not available (ImageStore not initialized)."
        if not self.image_store.enabled:
            return "Error: Image memory system is offline (MongoDB/Qdrant/Embedding not ready)."

        try:
            limit = max(1, min(int(limit), 50))
            s_time = float(start_time) if start_time else None
            e_time = float(end_time) if end_time else None
        except (ValueError, TypeError) as e:
            return f"Error: Invalid parameter value: {e}"

        # Local file direct view (bypass DB)
        if local_path:
            resolved_local = self._resolve_workspace_path(local_path)
            if not os.path.isfile(resolved_local):
                return f"Error: local file not found: {local_path}"
            results = [{
                "image_id": image_id or os.path.basename(resolved_local),
                "file_path": resolved_local,
                "tags": keyword or "(local file)",
                "user_id": user_id or "",
                "timestamp": time.time(),
            }]
        else:
            results = self.image_store.search_images(
                image_id=image_id,
                keyword=keyword,
                text_query=text_query,
                start_time=s_time,
                end_time=e_time,
                user_id=user_id,
                limit=limit,
            )

        if not results:
            return "No images found matching the criteria."

        # 如果开启 use_image_model，强制 return_image 为 True 以便注入 MediaTag
        if use_image_model:
            return_image = True

        # Format results for LLM consumption
        output_lines = [f"Found {len(results)} image(s):\n"]
        if return_image:
            output_lines.append("[Mode] return_image=true: output includes MediaTag lines [image: abs_path] for downstream multimodal ingestion.\n")
        if use_image_model:
            output_lines.append("[Hint] use_image_model=true: downstream should switch to the image model until crop_image_for_llm completes.\n")
        for i, img in enumerate(results, 1):
            output_lines.append(f"--- Image {i} ---")
            output_lines.append(f"  ID: {img.get('image_id', 'N/A')}")
            output_lines.append(f"  File: {img.get('file_path', 'N/A')}")
            output_lines.append(f"  Tags: {img.get('tags', img.get('text', 'N/A'))}")
            ocr_text = img.get('ocr_text', '')
            if ocr_text:
                # 截断过长的 OCR 文字，避免输出过大
                preview = ocr_text[:200] + ('...' if len(ocr_text) > 200 else '')
                output_lines.append(f"  OCR Text: {preview}")
            ts = img.get("timestamp")
            if ts:
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                output_lines.append(f"  Time: {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            output_lines.append(f"  User: {img.get('user_id', 'N/A')}")
            score = img.get("score")
            if score is not None:
                output_lines.append(f"  Relevance: {score:.3f}")
            if return_image:
                file_path = str(img.get('file_path', '')).strip()
                if file_path:
                    abs_path = file_path if os.path.isabs(file_path) else os.path.abspath(file_path)
                    output_lines.append(f"  MediaTag: [image: {abs_path}]")
            output_lines.append("")

        return "\n".join(output_lines)

    def crop_image_for_llm(
        self,
        image_path: str,
        preset: str = "",
        left: Optional[int] = None,
        top: Optional[int] = None,
        right: Optional[int] = None,
        bottom: Optional[int] = None,
        padding: int = 0,
        output_path: str = "",
        return_image: bool = True,
    ) -> str:
        """
        Crops an image to highlight important regions for LLM image understanding.

        IMPORTANT: `preset` and pixel range (`left/top/right/bottom`) are mutually exclusive input modes.
        Prefer using presets!!
        If both are provided, preset takes priority automatically.

        Built-in presets (ratio-based):
        - top_half, bottom_half, left_half, right_half
        - center, top_center, bottom_center

        :param image_path: Path to source image (absolute path or workspace-relative path).
        :param preset: Optional preset crop area name. If provided, it overrides pixel range.
        :param left: Left pixel of crop range (used only when preset is empty).
        :param top: Top pixel of crop range (used only when preset is empty).
        :param right: Right pixel of crop range (used only when preset is empty).
        :param bottom: Bottom pixel of crop range (used only when preset is empty).
        :param padding: Optional padding (pixels) around selected area. Default 0.
        :param output_path: Optional output path. Default writes to workspace downloads/crops.
        :param return_image: When true, include `[image: ...]` MediaTag for next-round multimodal use.
        """
        try:
            from PIL import Image
        except ImportError:
            return "Error: Pillow is not installed. Please install Pillow to enable image cropping."

        resolved_input = self._resolve_workspace_path(image_path)
        if not os.path.isfile(resolved_input):
            return f"Error: image file not found: {image_path}"

        preset_key = str(preset or "").strip().lower()
        use_preset = bool(preset_key)

        presets: Dict[str, tuple] = {
            "top_half": (0.0, 0.0, 1.0, 0.5),
            "bottom_half": (0.0, 0.5, 1.0, 1.0),
            "left_half": (0.0, 0.0, 0.5, 1.0),
            "right_half": (0.5, 0.0, 1.0, 1.0),
            "center": (0.15, 0.15, 0.85, 0.85),
            "top_center": (0.1, 0.0, 0.9, 0.55),
            "bottom_center": (0.1, 0.45, 0.9, 1.0),
        }

        try:
            padding = max(0, int(padding or 0))
        except Exception:
            return "Error: padding must be an integer >= 0."

        try:
            with Image.open(resolved_input) as img:
                width, height = img.size

                if use_preset:
                    if preset_key not in presets:
                        available = ", ".join(sorted(presets.keys()))
                        return f"Error: unknown preset '{preset}'. Available presets: {available}"
                    rx1, ry1, rx2, ry2 = presets[preset_key]
                    x1 = int(width * rx1)
                    y1 = int(height * ry1)
                    x2 = int(width * rx2)
                    y2 = int(height * ry2)
                    mode = f"preset:{preset_key}"
                else:
                    missing = [
                        name
                        for name, value in {
                            "left": left,
                            "top": top,
                            "right": right,
                            "bottom": bottom,
                        }.items()
                        if value is None
                    ]
                    if missing:
                        return (
                            "Error: pixel crop range is incomplete. "
                            "Provide all of left/top/right/bottom, or use preset."
                        )

                    try:
                        assert left is not None and top is not None and right is not None and bottom is not None
                        x1, y1, x2, y2 = int(left), int(top), int(right), int(bottom)
                    except Exception:
                        return "Error: left/top/right/bottom must be valid integers."

                    mode = "pixel_range"

                x1 = max(0, x1 - padding)
                y1 = max(0, y1 - padding)
                x2 = min(width, x2 + padding)
                y2 = min(height, y2 + padding)

                if x1 >= x2 or y1 >= y2:
                    return (
                        f"Error: invalid crop box after clamp: ({x1}, {y1}, {x2}, {y2}). "
                        f"Image size is {width}x{height}."
                    )

                cropped = img.crop((x1, y1, x2, y2))

                if output_path:
                    resolved_output = self._resolve_workspace_path(output_path)
                    output_dir = os.path.dirname(resolved_output)
                    if output_dir:
                        os.makedirs(output_dir, exist_ok=True)
                else:
                    crop_dir = os.path.join(str(DOWNLOADS_DIR), "crops")
                    os.makedirs(crop_dir, exist_ok=True)
                    src_stem = os.path.splitext(os.path.basename(resolved_input))[0]
                    resolved_output = os.path.join(
                        crop_dir,
                        f"{src_stem}_crop_{uuid.uuid4().hex[:8]}.png",
                    )

                cropped.save(resolved_output)
                abs_output = os.path.abspath(resolved_output)

                lines = [
                    "Image crop completed.",
                    f"Mode: {mode}",
                    f"Source: {resolved_input}",
                    f"Output: {abs_output}",
                    f"Original size: {width}x{height}",
                    f"Crop box: ({x1}, {y1}, {x2}, {y2})",
                    f"Cropped size: {x2 - x1}x{y2 - y1}",
                ]
                if use_preset and any(v is not None for v in (left, top, right, bottom)):
                    lines.append("Note: preset is provided, so pixel range was ignored.")
                if return_image:
                    lines.append(f"MediaTag: [image: {abs_output}]")
                return "\n".join(lines)
        except Exception as e:
            return f"Error cropping image: {e}"

    # ------------------------------------------------------------------
    # 动态闹钟工具 (Alarm Tools)
    # ------------------------------------------------------------------

    def set_alarm(
        self,
        trigger_time: str = "",
        reason: str = "",
        countdown_minutes: int = 0,
    ) -> str:
        """
        Set a dynamic alarm/reminder that triggers at a specific time or after a countdown. Supports both absolute time and countdown mode. Reason is optional.

        :param trigger_time: Absolute trigger time. Format: "HH:MM" (today), "YYYY-MM-DD HH:MM", or "MM-DD HH:MM". Leave empty if using countdown mode.
        :param reason: The reason/purpose for the alarm (optional). E.g. "提醒主人喝水", "下班提醒", "会议开始".
        :param countdown_minutes: Countdown in minutes from now. Set to > 0 to use countdown mode (trigger_time will be ignored). E.g. 30 means "30 minutes from now".
        """
        if not self.scheduler:
            return "Error: Scheduler not available."

        is_countdown = countdown_minutes > 0
        result = self.scheduler.add_alarm(
            trigger_time_str=trigger_time,
            reason=reason,
            is_countdown=is_countdown,
            countdown_minutes=countdown_minutes,
        )
        if result["success"]:
            return json.dumps(result, ensure_ascii=False)
        else:
            return f"Error: {result['message']}"

    def list_alarms(self) -> str:
        """
        List all active (unfired) alarms and reminders. Returns a JSON array of alarm objects with trigger_time, reason, and alarm_id.
        """
        if not self.scheduler:
            return "Error: Scheduler not available."

        alarms = self.scheduler.list_alarms()
        if not alarms:
            return "当前没有活跃的闹钟。"
        return json.dumps(alarms, ensure_ascii=False, indent=2)

    def cancel_alarm(self, alarm_id: str) -> str:
        """
        Cancel an active alarm by its ID. Use list_alarms to get the alarm_id first.

        :param alarm_id: The unique ID of the alarm to cancel.
        """
        if not self.scheduler:
            return "Error: Scheduler not available."

        result = self.scheduler.cancel_alarm(alarm_id)
        return json.dumps(result, ensure_ascii=False)

    # ------------------------------------------------------------------

    def _generate_schema(self, func: Callable) -> Dict[str, Any]:
        """Generates a JSON schema for a function."""
        import typing
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or ""
        desc = doc.strip().split("\\n")[0]
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
            
            parameters["properties"][name] = {"type": param_type}
            if param.default == inspect.Parameter.empty:
                parameters["required"].append(name)
        return {"name": func.__name__, "description": desc, "parameters": parameters}

