# brain/tools.py

import os
import json
import re
import subprocess
import logging
import inspect
from typing import List, Dict, Callable, Any
from platforms.base import BaseAdapter

logger = logging.getLogger(__name__)

# --- Workspace & Security Constants ---
WORKSPACE_ROOT = os.path.abspath(os.getcwd())  # Project root (where main.py runs)
SKILLS_DIR = os.path.join(WORKSPACE_ROOT, "skills")
DOWNLOADS_DIR = os.path.join(WORKSPACE_ROOT, "downloads")

# Files that LLM should NEVER read (contain secrets)
SENSITIVE_FILES = {"config.yml", "config.yaml", ".env", ".env.local"}
# Dangerous command patterns
BLOCKED_COMMANDS = ["rm -rf /", "mkfs", "dd if=", "> /dev/", "shutdown", "reboot", "passwd"]

SKILL_MAIN_PY_TEMPLATE = """\"\"\"
{description}
\"\"\"
import argparse
import json
import sys

def run(**kwargs):
    \"\"\"Main logic for the skill. Implement your code here.\"\"\"
    # TODO: Replace this placeholder with real implementation!
    # kwargs contains the arguments passed from the command line.
    # Example: kwargs = {"keyword": "blue archive", "limit": "5"}
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
        self.register(self.read_file)
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
                f"  3. Have an 'if __name__ == \"__main__\":' entry point\n\n"
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
            return f"Error: Skill '{skill_name}' not found at '{skill_script_path}'. Available skills can be listed with get_available_skills."
        try:
            args_dict = json.loads(args_json)
            command = ["python3", skill_script_path]
            for key, value in args_dict.items():
                command.extend([f"--{key}", str(value)])
            
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            output = result.stdout.strip()
            stderr = result.stderr.strip()
            
            # Always include stderr if present
            if stderr:
                output += f"\n[STDERR]\n{stderr}"
            
            if result.returncode != 0:
                error_msg = f"Skill '{skill_name}' failed (exit code {result.returncode})."
                if output:
                    error_msg += f"\n{output}"
                if "not been implemented" in (output + stderr):
                    error_msg += (
                        f"\n\n⚠️ This skill is still using TEMPLATE CODE — it has no real implementation. "
                        f"You need to use write_file to replace '{skill_script_path}' with actual working code first, "
                        f"then try execute_skill again. Do NOT retry without fixing the code."
                    )
                return error_msg
            
            if not output:
                # Check if the script is still template code
                try:
                    with open(skill_script_path, 'r') as f:
                        code = f.read()
                    if "TODO: Replace this placeholder" in code or "Skill executed successfully" in code:
                        return (
                            f"⚠️ Skill '{skill_name}' produced no output because it still contains TEMPLATE CODE.\n"
                            f"You MUST use write_file to replace '{skill_script_path}' with a real implementation first.\n"
                            f"Do NOT call execute_skill again until you have written the actual code."
                        )
                except Exception:
                    pass
                return (
                    f"Skill '{skill_name}' executed successfully (exit code 0) but produced no stdout output.\n"
                    f"This usually means the script does not print() its results. "
                    f"Consider reading the script with read_file('{skill_script_path}') to check if it needs fixing."
                )
            
            return output
        except subprocess.TimeoutExpired:
            return f"Error: Skill '{skill_name}' timed out after 120 seconds."
        except json.JSONDecodeError as e:
            return f"Error: Invalid args_json format: {e}. Must be valid JSON string like '{{\"key\": \"value\"}}'."
        except Exception as e:
            return f"Error executing skill '{skill_name}': {e}"

    def get_available_skills(self) -> str:
        """Lists all available skills that N.O.R.A. Core can use."""
        skills_dir = "skills/"
        if not os.path.exists(skills_dir): return "Error: Skills directory not found."
        try:
            skill_folders = [d for d in os.listdir(skills_dir) if os.path.isdir(os.path.join(skills_dir, d)) and not d.startswith('__')]
            if not skill_folders: return "No skills are currently available."
            return "Available skills:\\n" + "\\n".join([f"- {s}" for s in skill_folders])
        except Exception as e: return f"Error listing skills: {e}"

    def _is_path_safe(self, path: str) -> tuple:
        """Check if a path is safe to access. Returns (is_safe, reason)."""
        abs_path = os.path.abspath(path)
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
            target = os.path.abspath(path)
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

    def read_file(self, path: str) -> str:
        """Reads the contents of a file. Cannot read sensitive config files (config.yml, .env)."""
        safe, reason = self._is_path_safe(path)
        if not safe:
            return reason
        try:
            with open(path, 'r', encoding='utf-8') as f: return f.read()
        except Exception as e: return f"Error reading file: {e}"

    def write_file(self, path: str, content: str) -> str:
        """Writes content to a file, overwriting it."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f: f.write(content)
            return f"Successfully wrote to {path}"
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
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 1. Try exact match first
            if old_code in content:
                new_content = content.replace(old_code, new_code, 1)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                return f"Successfully edited {path}."

            # 2. Fallback: normalized whitespace matching
            import re
            def normalize(s):
                return re.sub(r'\s+', ' ', s).strip()

            norm_old = normalize(old_code)
            lines = content.split('\n')

            # Sliding window over lines to find the best match
            old_line_count = max(1, old_code.count('\n') + 1)
            for window_size in range(max(1, old_line_count - 2), old_line_count + 3):
                for i in range(len(lines) - window_size + 1):
                    window = '\n'.join(lines[i:i + window_size])
                    if normalize(window) == norm_old:
                        # Found a match with normalized whitespace
                        new_lines = lines[:i] + new_code.split('\n') + lines[i + window_size:]
                        new_content = '\n'.join(new_lines)
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                        return f"Successfully edited {path} (matched with normalized whitespace)."

            return (
                f"Error: 'old_code' not found in {path} (even after whitespace normalization). "
                f"Tip: Use 'write_file' to overwrite the entire file instead of edit_file."
            )
        except Exception as e:
            return f"Error editing file: {e}"

    def exec_command(self, command: str) -> str:
        """
        (DANGEROUS) Executes a general-purpose shell command.
        WARNING: Do NOT use this for tasks that a high-level tool can do.
        Use 'list_dir' to list directories instead of 'ls'. Use 'read_file' to read files instead of 'cat'.
        NEVER use this to run skill scripts — use 'execute_skill' instead.
        """
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
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            output = result.stdout.strip()
            if result.stderr: output += f"\n[STDERR]\n{result.stderr.strip()}"
            if not output and result.returncode == 0:
                return "Command executed successfully with no output."
            return output
        except Exception as e: return f"Error executing command: {e}"

    def _generate_schema(self, func: Callable) -> Dict[str, Any]:
        """Generates a JSON schema for a function."""
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or ""
        desc = doc.strip().split("\\n")[0]
        parameters = {"type": "OBJECT", "properties": {}, "required": []}
        for name, param in sig.parameters.items():
            if name == 'self': continue
            param_type = "STRING"
            if param.annotation == int: param_type = "INTEGER"
            elif param.annotation == bool: param_type = "BOOLEAN"
            parameters["properties"][name] = {"type": param_type}
            if param.default == inspect.Parameter.empty:
                parameters["required"].append(name)
        return {"name": func.__name__, "description": desc, "parameters": parameters}

