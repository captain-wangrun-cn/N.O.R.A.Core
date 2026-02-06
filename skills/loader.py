import os
import re
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

class SkillLoader:
    """
    OpenClaw 风格的技能加载器。
    负责扫描 skills 目录下的 SKILL.md 文件，提取元数据。
    """

    def __init__(self, skills_dir: str = "skills"):
        # 获取绝对路径
        if not os.path.isabs(skills_dir):
            base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            skills_dir = os.path.join(base_path, skills_dir)
        
        self.skills_dir = skills_dir

    def scan_skills(self) -> List[Dict[str, str]]:
        """
        扫描目录，返回所有可用技能的摘要列表。
        返回: [{'name': '...', 'description': '...', 'path': '...'}]
        """
        skills = []
        if not os.path.exists(self.skills_dir):
            return []

        for item in os.listdir(self.skills_dir):
            skill_path = os.path.join(self.skills_dir, item)
            skill_md = os.path.join(skill_path, "SKILL.md")

            if os.path.isdir(skill_path) and os.path.exists(skill_md):
                meta = self._parse_skill_md(skill_md)
                if meta:
                    meta['path'] = skill_md # 记录绝对路径，方便读取
                    skills.append(meta)

        logger.info(f"扫描到 {len(skills)} 个技能: {[s['name'] for s in skills]}")
        return skills

    def _parse_skill_md(self, file_path: str) -> Dict[str, str]:
        """简单的正则解析，提取 <name> 和 <description>"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 使用正则提取 XML 风格标签
            name_match = re.search(r'<name>(.*?)</name>', content, re.DOTALL)
            desc_match = re.search(r'<description>(.*?)</description>', content, re.DOTALL)

            if name_match and desc_match:
                return {
                    "name": name_match.group(1).strip(),
                    "description": desc_match.group(1).strip()
                }
            else:
                logger.warning(f"文件 {file_path} 格式不正确，缺少 <name> 或 <description> 标签。")
                return None
        except Exception as e:
            logger.error(f"解析技能文件 {file_path} 失败: {e}")
            return None
