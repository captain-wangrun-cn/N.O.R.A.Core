'''
Author: WR(captain-wangrun-cn)
Date: 2026-02-07 15:44:02
LastEditors: WR(captain-wangrun-cn)
LastEditTime: 2026-02-09 17:24:46
FilePath: \N.O.R.A.Core\brain\prompts.py
'''
from jinja2 import Environment, FileSystemLoader
import os
import sys  # Added sys import for path manipulation

# 设置 Jinja2 环境
template_dir = os.path.join(os.path.dirname(__file__), 'templates')
env = Environment(loader=FileSystemLoader(template_dir))

def get_system_prompt(instructions: list = None, platform: str = None) -> str:
    """
    使用 Jinja2 模板渲染最终的 System Prompt。
    
    Args:
        instructions: 额外的指令列表
        platform: 当前运行的平台名称 (如 'telegram'), 用于加载特定的 prompt
    """
    persona_template = env.get_template('persona_nora.jinja')
    persona_prompt = persona_template.render()
    
    # 尝试加载平台特定的 prompt
    platform_prompt = ""
    if platform:
        # 直接尝试读取 platforms/{platform}/PROMPT.md
        prompt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'platforms', platform, 'PROMPT.md')
        try:
            if os.path.exists(prompt_path):
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    platform_prompt = f.read()
        except Exception as e:
            print(f"Warning: Failed to load {platform} prompt: {e}")

    system_template = env.get_template('system.jinja')
    return system_template.render(
        persona_prompt=persona_prompt,
        instructions=instructions or [],
        platform_specific_prompt=platform_prompt
    )
