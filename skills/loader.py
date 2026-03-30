import os
import re
import logging
from typing import List, Dict, Optional, Any
import yaml
from workspace_config import get_workspace_manager

logger = logging.getLogger(__name__)

class SkillLoader:
    """
    OpenClaw/AgentSkills 风格的技能加载器。
    负责扫描 skills 目录下的 SKILL.md 文件，提取元数据。
    优先解析 YAML Frontmatter，兼容旧的 XML 标签。
    """

    def __init__(self, skills_dir: Optional[str] = None):
        if skills_dir:
            if not os.path.isabs(skills_dir):
                ws = get_workspace_manager()
                skills_dir = os.path.join(ws.root, skills_dir)
            self.skills_dir = os.path.abspath(skills_dir)
        else:
            # 默认使用 workspace/skills
            self.skills_dir = get_workspace_manager().skills_dir

    def scan_skills(self) -> List[Dict[str, Any]]:
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
                    
                    # Special override for skill_creator based on expert analysis
                    if meta['name'] == 'skill_creator':
                        meta['description'] = "直接调用此工具来编写新技能代码。参数只需要技能名称和功能描述，严禁在调用前反复扫描文件系统。"
                    
                    skills.append(meta)
                else:
                    logger.warning(f"技能 {item} 的 SKILL.md 缺少 name 或 description，已跳过。")

        logger.info(f"扫描到 {len(skills)} 个技能: {[s['name'] for s in skills]}")
        return skills

    def _parse_skill_md(self, file_path: str) -> Optional[Dict[str, Any]]:
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
            
            # 3. 最后回退：从 Markdown 标题和首段文字推断 name/description
            # 支持 "# skill_name\n\ndescription text" 这种 LLM 自动生成的格式
            heading_match = re.match(r'^#\s+(.+)', content.strip())
            if heading_match:
                name = heading_match.group(1).strip()
                # 取标题后的第一段非空文字作为 description
                remaining = content.strip()[heading_match.end():].strip()
                # 跳过空行，取第一段
                desc_lines = []
                for line in remaining.split('\n'):
                    stripped = line.strip()
                    if stripped.startswith('#') or stripped.startswith('##'):
                        break  # 遇到下一个标题就停
                    if stripped:
                        desc_lines.append(stripped)
                    elif desc_lines:
                        break  # 遇到空行且已有内容就停
                if desc_lines:
                    description = ' '.join(desc_lines)
                    logger.debug(f"从 {os.path.basename(file_path)} 的 Markdown 标题推断出 name='{name}', description='{description[:50]}...'")
                    return {"name": name, "description": description}

            logger.warning(f"文件 {file_path} 既无有效 YAML Frontmatter 也无 XML 标签。")
            return None
            
        except Exception as e:
            logger.error(f"解析技能文件 {file_path} 失败: {e}")
            return None
