# brain/tools.py

import os
import subprocess
import logging
import inspect
from typing import List, Dict, Callable, Any
from platforms.base import BaseAdapter

logger = logging.getLogger(__name__)

class ToolManager:
    """
    Manages and executes all available tools.
    """
    def __init__(self, adapter: BaseAdapter):
        self._tools: Dict[str, Callable] = {}
        self._schemas: List[Dict[str, Any]] = []
        self.adapter = adapter
        self._register_primitive_tools()

    def _register_primitive_tools(self):
        self.register(self.read_file)
        self.register(self.write_file)
        # self.register(self.list_dir) # Deprecated for safety
        self.register(self.exec_command)
        self.register(self.edit_file)
        # self.register(self.delegate_to_coder) # Temporarily disable for now
        self.register(self.get_available_skills)

    def register(self, func: Callable):
        self._tools[func.__name__] = func
        self._schemas.append(self._generate_schema(func))

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return self._schemas

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name not in self._tools:
            return f"Error: Tool '{name}' not found."
        
        try:
            logger.info(f"Executing tool: {name} with args: {args}")
            func = self._tools[name]
            
            if inspect.iscoroutinefunction(func):
                result = await func(**args)
            else:
                result = func(**args)
            
            return str(result)
        except Exception as e:
            logger.error(f"Tool execution error for {name}: {e}", exc_info=True)
            return f"Error executing {name}: {str(e)}"

    # --- Primitive Tools ---
    
    def get_available_skills(self) -> str:
        """
        Lists all available skills that N.O.R.A. Core can use.
        Use this tool to discover what capabilities are available.
        """
        # Correctly reference the skills directory relative to the project root.
        # Assuming main.py is at the root of N.O.R.A.Core.
        skills_dir = "skills/" 
        try:
            if not os.path.exists(skills_dir) or not os.path.isdir(skills_dir):
                return f"Error: Skills directory '{os.path.abspath(skills_dir)}' not found."
            
            skill_folders = [d for d in os.listdir(skills_dir) if os.path.isdir(os.path.join(skills_dir, d)) and not d.startswith('__')]
            
            if not skill_folders:
                return "No skills are currently available."

            available_skills = []
            for skill in skill_folders:
                available_skills.append(f"- {skill}")

            return "Here are the available skills:\n" + "\\n".join(available_skills)
        except Exception as e:
            return f"Error while trying to list skills: {e}"

    def read_file(self, path: str) -> str:
        """Reads the contents of a file."""
        try:
            if not os.path.exists(path): return f"Error: File '{path}' not found."
            with open(path, 'r', encoding='utf-8') as f: return f.read()
        except Exception as e: return f"Error reading file: {e}"

    def write_file(self, path: str, content: str) -> str:
        """Writes content to a file, overwriting it."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f: f.write(content)
            return f"Successfully wrote to {path}"
        except Exception as e: return f"Error writing file: {e}"

    def exec_command(self, command: str) -> str:
        """Executes a shell command."""
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            output = result.stdout
            if result.stderr: output += f"\\n[STDERR]\\n{result.stderr}"
            
            # If there's no output but the command succeeded, return a clear success message.
            if not output.strip() and result.returncode == 0:
                return "Command executed successfully with no output."
            
            return output.strip()
        except subprocess.TimeoutExpired: return "Error: Command timed out."
        except Exception as e: return f"Error executing command: {e}"

    def edit_file(self, path: str, old_code: str, new_code: str) -> str:
        """Performs a precise find-and-replace on a file."""
        try:
            if not os.path.exists(path): return f"Error: File '{path}' not found."
            with open(path, 'r', encoding='utf-8') as f: content = f.read()
            if old_code not in content: return f"Error: 'old_code' not found in {path}."
            new_content = content.replace(old_code, new_code, 1)
            with open(path, 'w', encoding='utf-8') as f: f.write(new_content)
            return f"Successfully edited {path}."
        except Exception as e: return f"Error editing file: {e}"

    def _generate_schema(self, func: Callable) -> Dict[str, Any]:
        """Generates a JSON schema for a function."""
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or "No description."
        desc = doc.strip().split("\\n")[0]
        parameters = {"type": "OBJECT", "properties": {}, "required": []}
        for name, param in sig.parameters.items():
            if name in ('self', 'args', 'kwargs'): continue
            param_type = "STRING"
            if param.annotation == int: param_type = "INTEGER"
            elif param.annotation == bool: param_type = "BOOLEAN"
            parameters["properties"][name] = {"type": param_type, "description": "Parameter for " + name}
            if param.default == inspect.Parameter.empty:
                parameters["required"].append(name)
        return {"name": func.__name__, "description": desc, "parameters": parameters}
