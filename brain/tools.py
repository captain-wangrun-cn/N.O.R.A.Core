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
        self.register(self.edit_file)
        self.register(self.delegate_to_coder)

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

    def edit_file(self, path: str, old_code: str, new_code: str) -> str:
        """
        对文件进行精准的查找和替换。
        Performs a precise find-and-replace operation on a file.
        :param path: The path to the file.
        :param old_code: The exact block of code to be replaced.
        :param new_code: The new block of code to insert.
        """
        try:
            if not os.path.exists(path):
                return f"Error: File '{path}' not found."
            
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            if old_code not in content:
                return f"Error: The specified `old_code` was not found in {path}."
            
            # Perform a single, precise replacement
            new_content = content.replace(old_code, new_code, 1)
            
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_content)
                
            return f"Successfully edited {path}."
        except Exception as e:
            return f"Error editing file: {e}"

    def delegate_to_coder(self, task_description: str) -> str:
        """
        将一个复杂的代码生成任务委托给专门的 Coder AI 模型。
        Delegates a complex code generation task to a specialized Coder AI model.
        :param task_description: A detailed description of the coding task.
        """
        try:
            from brain.llm import get_llm_client
            
            logger.info(f"Delegating task to Coder model: {task_description[:60]}...")
            
            # Get a client for the coder model
            # This assumes a 'coder' model is defined in config.yml
            coder_client = get_llm_client(model_alias="coder")
            
            # A specialized prompt for the coder model
            coder_system_prompt = (
                "You are an expert Python programmer. Your task is to generate clean, "
                "efficient, and correct Python code based on the user's request. "
                "Do not add any conversational fluff or explanations outside of the code. "
                "Only return the raw code block."
            )
            
            # We use asyncio.run() here because this tool method itself is not async,
            # but the underlying chat call is. This is a simple way to bridge sync and async.
            import asyncio
            
            # This is a blocking call within the tool's execution context
            code_result = asyncio.run(coder_client.chat(
                system_prompt=coder_system_prompt,
                user_prompt=task_description,
                history=[] # Coder works on isolated tasks
            ))
            
            return code_result

        except ValueError as ve:
            # Handle case where 'coder' model is not configured
            logger.warning(f"Could not delegate to coder: {ve}")
            return "Error: The 'coder' model is not configured in config.yml. Please ask the user to set it up."
        except Exception as e:
            logger.error(f"Error delegating to coder model: {e}")
            return f"An unexpected error occurred while delegating the task: {str(e)}"

    def _generate_schema(self, func: Callable) -> Dict[str, Any]:
        """
        Helper: Generate OpenAI/Gemini compatible function schema.
        Note: Gemini SDK (Protobuf) prefers UPPERCASE types (STRING, OBJECT, etc.)
        """
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or "No description."
        
        # Simple parsing of docstring
        desc = doc.split("\n")[0]
        
        parameters = {
            "type": "OBJECT", # Changed from "object"
            "properties": {},
            "required": []
        }
        
        for name, param in sig.parameters.items():
            if name == 'self': continue
            param_type = "STRING" # Default, changed from "string"
            if param.annotation == int: param_type = "INTEGER"
            elif param.annotation == bool: param_type = "BOOLEAN"
            
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
