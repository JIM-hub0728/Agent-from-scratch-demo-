import json
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).parent / "memory"


def json_safe(value):
    """把任意值递归拍平成 json 能序列化的基础类型（SDK 块对象 -> dict）"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump())
    return str(value)


class MemoryStore:
    """记忆系统的存储层：只负责文件读写，不碰 API、不知道模型的存在"""

    def __init__(self, memory_dir: Path):
        # 构造时只登记路径，不做 IO（import 不该有副作用）
        self.memory_dir = memory_dir
        self.memory_file = memory_dir / "MEMORY.md"       # 核心记忆（整读整写）
        self.user_file = memory_dir / "USER.md"           # 用户画像（整读整写）
        self.history_file = memory_dir / "history.jsonl"  # 原始流水（只追加）

    def ensure_files(self):
        """惰性初始化：每个公开方法开头调用。只在缺失时创建，绝不覆盖已有记忆"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            self.memory_file.write_text("# 长期记忆\n", encoding="utf-8")
        if not self.user_file.exists():
            self.user_file.write_text("# 用户档案\n", encoding="utf-8")
        if not self.history_file.exists():
            self.history_file.touch()

    def append_history(self, message: dict):
        """追加一条消息到原始流水 history.jsonl（每行一个 JSON 对象）"""
        self.ensure_files()
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "role": message.get("role"),
            "content": json_safe(message.get("content")),
        }
        # "a" = append，写指针在文件尾；写成 "w" 会清空整个流水
        with self.history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_memory(self) -> str:
        self.ensure_files()
        return self.memory_file.read_text(encoding="utf-8")

    def write_memory(self, text: str):
        """全量覆写 MEMORY.md（compact 时模型产出完整新版本）"""
        self.ensure_files()
        self.memory_file.write_text(text.strip() + "\n", encoding="utf-8")

    def read_user(self) -> str:
        self.ensure_files()
        return self.user_file.read_text(encoding="utf-8")

    def write_user(self, text: str):
        """全量覆写 USER.md"""
        self.ensure_files()
        self.user_file.write_text(text.strip() + "\n", encoding="utf-8")

    def _today_episode_file(self) -> Path:
        """今天日期对应的情景记忆文件，如 memory/2026-08-31.md"""
        return self.memory_dir / f"{datetime.now():%Y-%m-%d}.md"

    def read_today_episode(self) -> str:
        """今日记忆，每轮注入 prompt；文件不存在时返回空串（正常状态）"""
        self.ensure_files()
        path = self._today_episode_file()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def append_episode(self, text: str):
        """compact 后追加一段到今日记忆"""
        self.ensure_files()
        with self._today_episode_file().open("a", encoding="utf-8") as f:
            f.write("\n" + text.strip() + "\n")


# 模块级单例：全程序共享同一个存储入口（from memory import memory）
memory = MemoryStore(MEMORY_DIR)
