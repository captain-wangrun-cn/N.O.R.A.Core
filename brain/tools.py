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
    Manages and executes all available tools, including primitive and messaging tools.
    """
    def __init__(self, adapter: BaseAdapter):
        self._tools: Dict[str, Callable] = {}
        self._schemas: List[Dict[str, Any]] = []
        self.adapter = adapter
        
        # Register all toolsets
        self._register_primitive_tools()
        self._register_messaging_tools()

    def _register_primitive_tools(self):
        self.register(self.read_file)
        self.register(self.write_file)
        self.register(self.list_dir)
        self.register(self.exec_command)
        self.register(self.edit_file)
        self.register(self.delegate_to_coder)
    
    def _register_messaging_tools(self):
        # self.register(self.send_intermediate_message)
        pass

    def register(self, func: Callable):
        """Registers a function as a tool, automatically generating its schema."""
        self._tools[func.__name__] = func
        self._schemas.append(self._generate_schema(func))

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Returns the list of all tool schemas for the LLM."""
        return self._schemas

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        """Executes a tool by name, handling both sync and async functions."""
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

    # --- Messaging Tools ---
    async def send_intermediate_message(self, text: str) -> str:
        """
        Sends an intermediate part of a long response to the user.
        :param text: The content of the message chunk to send.
        """
        chat_id = self.adapter.current_chat_id
        if not chat_id:
            return "Error: Could not determine current chat_id."
        try:
            await self.adapter.send_message(chat_id, text)
            return "Intermediate message sent successfully."
        except Exception as e:
            logger.error(f"Error in send_intermediate_message tool: {e}")
            return f"Error sending message: {e}"

    # --- Primitive Tools ---
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

    def list_dir(self, path: str = ".") -> str:
        """Lists files and directories in a path."""
        try:
            if not os.path.exists(path): return f"Error: Directory '{path}' not found."
            items = os.listdir(path)
            result = [f"{item}{'/' if os.path.isdir(os.path.join(path, item)) else ''}" for item in items]
            return "\n".join(result)
        except Exception as e: return f"Error listing directory: {e}"

    def exec_command(self, command: str) -> str:
        """Executes a shell command. 对于 `mkdir -p` 命令，如果返回 `(No output)` 则表示执行成功，无需再次尝试。"""
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            output = result.stdout
            if result.stderr: output += f"\n[STDERR]\n{result.stderr}"
            return output.strip() or "(No output)"
        except subprocess.TimeoutExpired: return "Error: Command timed out."
        except Exception as e: return f"Error executing command: {e}"

    def edit_file(self, path: str, old_code: str, new_code: str) -> str:
        """Performs a precise find-and-replace on a file."""
        try:
            if not os.path.exists(path): return f"Error: File '{path}' not found."
            with open(path, 'r', encoding='utf-8') as f: content = f.read()
            if old_code not in content: return f"Error: `old_code` not found in {path}."
            new_content = content.replace(old_code, new_code, 1)
            with open(path, 'w', encoding='utf-8') as f: f.write(new_content)
            return f"Successfully edited {path}."
        except Exception as e: return f"Error editing file: {e}"

    def delegate_to_coder(self, task_description: str) -> str:
        """Delegates a complex coding task to a specialized AI model."""
        try:
            from brain.llm import get_llm_client
            logger.info(f"Delegating to Coder: {task_description[:60]}...")
            coder_client = get_llm_client(model_alias="coder")
            coder_prompt = "You are an expert Python programmer. Your task is to generate clean, readable, and correct Python code based on the user's request. Only return the raw code block, without any conversational fluff or explanations."
            
            import asyncio
            return await coder_client.chat(
                system_prompt=coder_prompt,
                user_prompt=task_description,
                history=[]
            )
        except Exception as e:
            logger.error(f"Error delegating to coder: {e}", exc_info=True)
            return f"Error: Could not delegate to coder. Reason: {str(e)}"

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
