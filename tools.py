import subprocess
from pathlib import Path
import requests
from skill_loader import loader
from plan import todo_list
from bs4 import BeautifulSoup

CLIENT_TOOLS = [
    {
        "name": "run_command",
        "description": "在终端执行一条 shell 命令并返回输出",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "load_skill",
        "description": "按名称加载某个 skill 的完整说明。当任务与 system prompt 中某个 skill 的描述匹配时，先调用本工具获取详细步骤，再按步骤执行",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "skill 的名称（见 system prompt 中的可用列表）",
                },
            },
            "required": ["skill_name"],
        },
    },
    {
        "name": "web_fetch",
        "description": "抓取指定 URL的网页内容，返回纯文本正文。当用户给出具体网址，或 web_search 搜到值得深读的链接时使用",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的完整网址，需包含 http:// 或 https://",
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "update_todos",
        "description": (
            "创建或更新当前任务的 todolist。"
            "传入完整的 todos 数组（每次都是全量覆盖，而非增量）。"
            "用于：拆解多步骤任务、推进任务状态（pending → in_progress → completed）。"
            "约束：同一时间至多一个任务为 in_progress。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "完整的 todo 列表，按执行顺序排列",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":      {"type": "integer", "description": "序号，从 1 开始"},
                            "content": {"type": "string",  "description": "这一步要做什么"},
                            "status":  {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "状态"},
                        },
                        "required": ["id", "content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "把文本内容写入指定路径的文件（覆盖写），父目录不存在会自动创建。"
            "生成代码、HTML、Markdown 等文件时必须用本工具，禁止用 run_command 拼 echo 写文件。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径，相对当前工作目录或绝对路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文本内容",
                },
            },
            "required": ["path", "content"],
        },
    },
]

SERVER_TOOLS = [
    {
        "type": "web_search_20250305",  # 工具类型标识，带日期的版本号，协议规定的固定写法
        "name": "web_search",           # 模型看到的工具名
        "max_uses": 5,                  # 单次请求最多搜索几次，防止一次问答连环搜索烧太多额度
    }
]

def run_command(command: str) -> str:
    """在终端执行一条 shell 命令并返回输出"""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            errors="replace", timeout=30,
        )
        output = (result.stdout + result.stderr).strip()
        return output or "(命令执行完毕，无输出)"
    except subprocess.TimeoutExpired:
        return "(命令执行超时)"


def load_skill(skill_name: str) -> str:
    """按名称加载 skill 的完整说明"""
    return loader.get_skill_body(skill_name)

def web_fetch(url: str) -> str:
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding   # 按真实编码解码，避免中文乱码
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()                      # 删掉没有正文价值的部分
        text = soup.get_text(separator="\n", strip=True)
        return text[:8000] or "(页面无文本内容)"
    except requests.RequestException as e:
        return f"(抓取失败: {e})"

def update_todos(todos: list) -> str:
    """创建或更新 todolist（薄壳：干活的是 plan.todo_list）"""
    return todo_list.update(todos)


def write_file(path: str, content: str) -> str:
    """把文本内容写入文件（覆盖写），父目录不存在则自动创建"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {p}（{len(content)} 字符）"
    except OSError as e:
        return f"(写入失败: {e})"

HANDLERS = {
    "run_command": run_command,
    "write_file": write_file,
    "load_skill": load_skill,
    "web_fetch": web_fetch,
    "update_todos": update_todos,
}


def dispatch_tool(name: str, tool_input: dict) -> str:
    """按工具名分发调用，agent 主循环无需关心具体有哪些工具"""
    handler = HANDLERS.get(name)
    if not handler:
        return f"错误：未知工具 '{name}'"
    try:
        return handler(**tool_input)
    except TypeError as e:
        return f"错误：工具 '{name}' 参数不正确：{e}"
