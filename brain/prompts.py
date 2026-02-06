from jinja2 import Environment, FileSystemLoader
import os

# 设置 Jinja2 环境
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
env = Environment(loader=FileSystemLoader(template_dir))

def get_system_prompt(instructions: list = None) -> str:
    """
    使用 Jinja2 模板渲染最终的 System Prompt。
    """
    persona_template = env.get_template('persona_nora.jinja')
    persona_prompt = persona_template.render()
    
    system_template = env.get_template('system.jinja')
    return system_template.render(
        persona_prompt=persona_prompt,
        instructions=instructions or []
    )
