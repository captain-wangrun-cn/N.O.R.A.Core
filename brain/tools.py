import os
import subprocess
import logging
import inspect
from typing import List, Dict, Callable, Any

logger = logging.getLogger(__name__)

class ToolManager:
    """
    管理 AI 可用的原子工具。
    """
    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._schemas: List[Dict[str, Any]] = []
        
        # 注册内置工具
        self.register(self.read_file)
        self.register(self.write_file)
        self.register(self.list_dir)
        self.register(self.exec_command)

    def register(self, func: Callable):
        """注册一个工具函数，并自动生成 Schema。"""
        self._tools[func.__name__] = func
        self._schemas.append(self._generate_schema(func))

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """获取兼容 Gemini/OpenAI 的工具定义列表。"""
        return self._schemas

    def get_tool_map(self) -> Dict[str, Callable]:
        """获取工具名称到函数的映射。"""
        return self._tools

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """执行工具并返回结果字符串。"""
        if name not in self._tools:
            return f"Error: Tool '{name}' not found."
        
        try:
            logger.info(f"Executing tool: {name} with args: {args}")
            result = self._tools[name](**args)
            return str(result)
        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            return f"Error executing {name}: {str(e)}"

    # --- Built-in Primitive Tools ---

    def read_file(self, path: str) -> str:
        """
        读取文件内容。
        Read the contents of a file.
        :param path: The path to the file (relative to workspace).
        """
        try:
            if not os.path.exists(path):
                return f"Error: File '{path}' not found."
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            return f"Error reading file: {e}"

    def write_file(self, path: str, content: str) -> str:
        """
        写入文件内容（覆盖）。自动创建目录。
        Write content to a file. Creates directories if needed.
        :param path: The path to the file.
        :param content: The content to write.
        """
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            return f"Successfully wrote to {path}"
        except Exception as e:
            return f"Error writing file: {e}"

    def list_dir(self, path: str = ".") -> str:
        """
        列出目录下的文件和子目录。
        List files and directories in a path.
        :param path: The directory path (default is current dir).
        """
        try:
            if not os.path.exists(path):
                return f"Error: Directory '{path}' not found."
            items = os.listdir(path)
            # Add type indicator
            result = []
            for item in items:
                full_path = os.path.join(path, item)
                type_suffix = "/" if os.path.isdir(full_path) else ""
                result.append(f"{item}{type_suffix}")
            return "\n".join(result)
        except Exception as e:
            return f"Error listing directory: {e}"

    def exec_command(self, command: str) -> str:
        """
        执行 Shell 命令。请谨慎使用。
        Execute a shell command. Use with caution.
        :param command: The command to execute (e.g., 'ls -la', 'python script.py').
        """
        try:
            # 简单超时限制 30秒
            result = subprocess.run(
                command, 
                shell=True, 
                capture_output=True, 
                text=True, 
                timeout=30
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[STDERR]\n{result.stderr}"
            return output.strip() or "(No output)"
        except subprocess.TimeoutExpired:
            return "Error: Command timed out after 30 seconds."
        except Exception as e:
            return f"Error executing command: {e}"

    def _generate_schema(self, func: Callable) -> Dict[str, Any]:
        """
        Helper: Generate OpenAI/Gemini compatible function schema from docstring and type hints.
        Simplistic implementation.
        """
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or "No description."
        
        # Simple parsing of docstring for parameter descriptions (could be improved)
        desc = doc.split("\n")[0]
        
        parameters = {
            "type": "object",
            "properties": {},
            "required": []
        }
        
        for name, param in sig.parameters.items():
            if name == 'self': continue
            param_type = "string" # Default
            if param.annotation == int: param_type = "integer"
            elif param.annotation == bool: param_type = "boolean"
            
            parameters["properties"][name] = {
                "type": param_type,
                "description": f"Parameter {name}" # Placeholder
            }
            if param.default == inspect.Parameter.empty:
                parameters["required"].append(name)

        return {
            "name": func.__name__,
            "description": desc,
            "parameters": parameters
        }
