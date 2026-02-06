import os
import re
import logging
from typing import List, Dict, Optional
import yaml

logger = logging.getLogger(__name__)

class SkillLoader:
    """
    OpenClaw/AgentSkills 风格的技能加载器。
    负责扫描 skills 目录下的 SKILL.md 文件，提取元数据。
    优先解析 YAML Frontmatter，兼容旧的 XML 标签。
    """

    def __init__(self, skills_dir: str = "skills"):
        if not os.path.isabs(skills_dir):
            base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            skills_dir = os.path.join(base_path, skills_dir)
        
        self.skills_dir = skills_dir

    def scan_skills(self) -> List[Dict[str, any]]:
        """
        扫描目录，返回所有可用技能的摘要列表。
        """
        skills = []
        if not os.path.exists(self.skills_dir):
            logger.warning(f"技能目录不存在: {self.skills_dir}")
            return []

        for item in os.listdir(self.skills_dir):
            skill_path = os.path.join(self.skills_dir, item)
            skill_md = os.path.join(skill_path, "SKILL.md")

            if os.path.isdir(skill_path) and os.path.exists(skill_md):
                meta = self._parse_skill_md(skill_md)
                if meta and 'name' in meta and 'description' in meta:
                    meta['path'] = skill_md
                    skills.append(meta)
                else:
                    logger.warning(f"技能 {item} 的 SKILL.md 缺少 name 或 description，已跳过。")

        logger.info(f"扫描到 {len(skills)} 个技能: {[s['name'] for s in skills]}")
        return skills

    def _parse_skill_md(self, file_path: str) -> Optional[Dict[str, any]]:
        """
        解析 SKILL.md, 优先 YAML Frontmatter, 其次 XML 标签。
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 1. 尝试解析 YAML Frontmatter
            yaml_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
            if yaml_match:
                try:
                    meta = yaml.safe_load(yaml_match.group(1))
                    if isinstance(meta, dict):
                        logger.debug(f"成功从 {os.path.basename(file_path)} 解析到 YAML Frontmatter。")
                        return meta
                except yaml.YAMLError as e:
                    logger.error(f"解析 {file_path} 的 YAML Frontmatter 失败: {e}")
                    return None

            # 2. 如果没有 YAML, 回退到 XML 标签解析
            logger.debug(f"在 {os.path.basename(file_path)} 未找到 YAML, 尝试 XML 标签。")
            name_match = re.search(r'<name>(.*?)</name>', content, re.DOTALL)
            desc_match = re.search(r'<description>(.*?)</description>', content, re.DOTALL)

            if name_match and desc_match:
                return {
                    "name": name_match.group(1).strip(),
                    "description": desc_match.group(1).strip()
                }
            
            logger.warning(f"文件 {file_path} 既无有效 YAML Frontmatter 也无 XML 标签。")
            return None
            
        except Exception as e:
            logger.error(f"解析技能文件 {file_path} 失败: {e}")
            return None
