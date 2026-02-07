# brain/tools.py

import os
import json
import subprocess
import logging
import inspect
from typing import List, Dict, Callable, Any
from platforms.base import BaseAdapter

logger = logging.getLogger(__name__)

SKILL_MAIN_PY_TEMPLATE = """\"\"\"
{description}
\"\"\"

def run(**kwargs):
    # Main logic for the skill goes here
    # You can access arguments passed from the controller via kwargs
    print(f"Executing {{__name__}} with arguments: {kwargs}")
    return "Skill executed successfully."

\"\"\"
This dictionary is used to define the tool's schema for the LLM.
'name' should be the function name to be called.
'description' should be a brief explanation of what the tool does.
'parameters' should be a dictionary defining the arguments the tool accepts.
\"\"\"
SCHEMA = {
    "name": "run",
    "description": "{description}",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "example_arg": {
                "type": "STRING",
                "description": "An example argument."
            }
        },
        "required": ["example_arg"]
    }
}
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
                f"Successfully created new skill '{skill_name}'.\n"
                f"Step 1/3 Complete: Skill boilerplate generated at '{skill_dir}'.\n"
                f"Next step is to use 'write_file' to replace the content of '{os.path.join(skill_dir, 'main.py')}' with real implementation code."
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
            return f"Error: Skill '{skill_name}' not found."
        try:
            args_dict = json.loads(args_json)
            command = ["python3", skill_script_path]
            for key, value in args_dict.items():
                command.extend([f"--{key}", str(value)])
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            output = result.stdout.strip()
            stderr = result.stderr.strip()
            # Always include stderr if present (may contain useful info even on success)
            if stderr:
                output += f"\n[STDERR]\n{stderr}"
            if result.returncode != 0:
                return f"Skill '{skill_name}' exited with code {result.returncode}.\n{output}" if output else f"Skill '{skill_name}' exited with code {result.returncode} and no output."
            return output or f"Skill '{skill_name}' executed successfully (exit code 0, no stdout output). The script may need to print results to stdout."
        except subprocess.TimeoutExpired:
            return f"Error: Skill '{skill_name}' timed out after 120 seconds."
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

    def read_file(self, path: str) -> str:
        """Reads the contents of a file."""
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
        WARNING: Do NOT use this for tasks that a high-level tool can do. For simple, one-off tasks ONLY.
        """
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            output = result.stdout.strip()
            if result.stderr: output += f"\\n[STDERR]\\n{result.stderr.strip()}"
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

