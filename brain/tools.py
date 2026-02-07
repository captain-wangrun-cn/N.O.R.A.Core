# brain/tools.py

import os
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
    print(f"Executing {__name__} with arguments: {kwargs}")
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
        self.register(self.edit_file)
        self.register(self.get_available_skills)
        self.register(self.create_new_skill) # Register the new macro tool
        # self.register(self.list_dir) # Deprecated
        # self.register(self.exec_command) # Deprecated for safety
        # self.register(self.delegate_to_coder) # Temporarily disable

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

    # --- Macro & Primitive Tools ---

    def create_new_skill(self, skill_name: str, description: str) -> str:
        """
        Creates a complete boilerplate for a new skill. Use this to initialize a new capability.
        This single action will create the directory, SKILL.md, __init__.py, and a main.py template.
        :param skill_name: The name of the new skill (e.g., 'web_search').
        :param description: A one-sentence description of what the new skill does.
        """
        logger.info(f"Request to create new skill: {skill_name}")
        skills_base_dir = "skills"
        skill_dir = os.path.join(skills_base_dir, skill_name)

        # 1. Check-then-Act: Verify if skill already exists
        if os.path.exists(skill_dir):
            return f"Error: Skill '{skill_name}' already exists at '{skill_dir}'. Please choose a different name."

        try:
            # 2. Create the directory structure
            os.makedirs(skill_dir)

            # 3. Create SKILL.md
            with open(os.path.join(skill_dir, "SKILL.md"), 'w', encoding='utf-8') as f:
                f.write(f"# {skill_name}\n\n{description}\n")

            # 4. Create __init__.py
            with open(os.path.join(skill_dir, "__init__.py"), 'w', encoding='utf-8') as f:
                pass # Empty file

            # 5. Create main.py with a template
            main_py_content = SKILL_MAIN_PY_TEMPLATE.format(description=description)
            with open(os.path.join(skill_dir, "main.py"), 'w', encoding='utf-8') as f:
                f.write(main_py_content)
            
            # 6. Return a clear success message with progress
            feedback = (
                f"Successfully created new skill '{skill_name}'.\n"
                f"Step 1/3 Complete: Skill boilerplate generated at '{skill_dir}'.\n"
                f"Directory structure:\n"
                f"- {skill_dir}/\n"
                f"  - SKILL.md (Description written)\n"
                f"  - __init__.py (Created)\n"
                f"  - main.py (Template created)\n\n"
                f"Next step for you is to use the 'write_file' or 'edit_file' tool to add the core logic to '{os.path.join(skill_dir, 'main.py')}'."
            )
            return feedback

        except Exception as e:
            logger.error(f"Error creating new skill '{skill_name}': {e}", exc_info=True)
            return f"An unexpected error occurred while creating the skill: {e}"

    def get_available_skills(self) -> str:
        """
        Lists all available skills that N.O.R.A. Core can use.
        Use this tool to discover what capabilities are available.
        """
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

            return "Here are the available skills:\n" + "\n".join(available_skills)
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
        desc = doc.strip().split("\n")[0]
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
