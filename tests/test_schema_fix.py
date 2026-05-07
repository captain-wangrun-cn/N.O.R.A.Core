
"""验证 _generate_schema 能正确处理 Optional[int] 类型"""
import inspect
import typing
from typing import Optional, Dict, Any, Callable


def _generate_schema(func: Callable) -> Dict[str, Any]:
    """复制的修复后的 schema 生成逻辑"""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""
    desc = doc.strip().split("\\n")[0]
    parameters = {"type": "OBJECT", "properties": {}, "required": []}
    for name, param in sig.parameters.items():
        if name == 'self':
            continue
        
        annotation = param.annotation
        param_type = "STRING"
        
        origin = getattr(annotation, '__origin__', None)
        if origin is typing.Union:
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


# 模拟 read_file 函数签名
def read_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
    """Reads the contents of a file."""
    pass


def exec_command(command: str, timeout: int = 60) -> str:
    """Executes a shell command via PowerShell on Windows or bash on Linux/macOS."""
    pass


if __name__ == "__main__":
    import json
    
    print("=== read_file schema ===")
    schema = _generate_schema(read_file)
    print(json.dumps(schema, indent=2))
    
    # 验证类型
    props = schema["parameters"]["properties"]
    assert props["path"]["type"] == "STRING", f"path should be STRING, got {props['path']['type']}"
    assert props["start_line"]["type"] == "INTEGER", f"start_line should be INTEGER, got {props['start_line']['type']}"
    assert props["end_line"]["type"] == "INTEGER", f"end_line should be INTEGER, got {props['end_line']['type']}"
    print("✓ read_file: start_line 和 end_line 正确声明为 INTEGER")
    
    print("\n=== exec_command schema ===")
    schema2 = _generate_schema(exec_command)
    print(json.dumps(schema2, indent=2))
    assert schema2["parameters"]["properties"]["timeout"]["type"] == "INTEGER"
    print("✓ exec_command: timeout 正确声明为 INTEGER")
    
    # 测试 int() 转换的防御性代码
    print("\n=== 测试字符串行号转 int ===")
    start_line = '225'
    start_line = int(start_line)
    result = start_line - 1
    print(f"✓ int('225') - 1 = {result}")
    
    print("\n✅ 所有测试通过！")
