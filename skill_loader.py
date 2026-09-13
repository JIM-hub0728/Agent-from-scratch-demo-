import re
from pathlib import Path

import yaml

DEFAULT_SKILLS_DIR = Path(__file__).parent / "skills"
HEAD_READ_CHARS = 8192  # 启动扫描只读文件头部这么多字符：够装下 front matter，不读正文


class SkillLoader:
    def __init__(self, skill_dir: Path):
        self.skill_dir = skill_dir
        self.skills = {}
        self._load_all()

    def _load_all(self):
        """启动扫描：只登记"有什么 skill、叫什么、一句话能干什么"（第一层目录）。
        正文一个字都不读——那是首次 load_skill 时的事（懒加载防启动雪崩）"""
        if not self.skill_dir.exists():
            return
        for f in sorted(self.skill_dir.rglob("SKILL.md")):
            meta = self._read_meta_only(f)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": None, "path": str(f)}  # body=None 占位：未加载

    def _read_meta_only(self, path: Path) -> dict:
        """只读文件头部来解析 front matter（拿 name/description）。
        头部装不下整个 front matter 的极端情况，退化为无 meta（用目录名兜底）"""
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(HEAD_READ_CHARS)
        meta, _ = self._parse_front_matter(head)
        return meta

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
        """skill 的完整正文：首次调用时才读盘并缓存（渐进式第二层，真·懒加载）"""
        skill = self.skills.get(skill_name)
        if not skill:
            available = ", ".join(self.skills) or "(无)"
            return f"错误：未找到名为 '{skill_name}' 的 skill。当前可用：{available}"
        if skill["body"] is None:  # 缓存未命中：首次用到才读全文
            text = Path(skill["path"]).read_text(encoding="utf-8")
            _, skill["body"] = self._parse_front_matter(text)
        return skill["body"]


# 模块级单例：tools.py 和 agent.py 共用同一个实例
loader = SkillLoader(DEFAULT_SKILLS_DIR)
