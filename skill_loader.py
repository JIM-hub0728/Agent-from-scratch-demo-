import re
from pathlib import Path

import yaml

DEFAULT_SKILLS_DIR = Path(__file__).parent / "skills"


class SkillLoader:
    def __init__(self, skill_dir: Path):
        self.skill_dir = skill_dir
        self.skills = {}
        self._load_all()

    def _load_all(self):
        if not self.skill_dir.exists():
            return
        for f in sorted(self.skill_dir.rglob("SKILL.md")):
            text = f.read_text(encoding="utf-8")
            meta, body = self._parse_front_matter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    def _parse_front_matter(self, text: str) -> tuple:
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2).strip()

    def get_catalog(self) -> str:
        """所有 skill 的名称+一句话描述，注入 system prompt（渐进式第一层）"""
        if not self.skills:
            return "(暂无可用 skill)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "(无描述)")
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    def get_skill_body(self, skill_name: str) -> str:
        """skill 的完整正文，供 load_skill 工具按需加载（渐进式第二层）"""
        skill = self.skills.get(skill_name)
        if not skill:
            available = ", ".join(self.skills) or "(无)"
            return f"错误：未找到名为 '{skill_name}' 的 skill。当前可用：{available}"
        return skill["body"]


# 模块级单例：tools.py 和 agent.py 共用同一个实例
loader = SkillLoader(DEFAULT_SKILLS_DIR)
