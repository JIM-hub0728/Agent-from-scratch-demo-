import atexit
import base64
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from skill_loader import loader
from plan import todo_list
import sandbox
import ui

READ_MAX_LINES = 2000      # read_file 一次最多读多少行
LIST_MAX_ENTRIES = 500     # list_dir 最多列多少项
CMD_OUTPUT_LIMIT = 8000    # run_command 输出超过此长度就落盘
CMD_HEAD_PREVIEW = 2000    # 落盘时返回给模型的头部预览长度

_read_paths = set()                 # 本次会话读过/写过的文件（先读后写校验的台账）
_confirm_lock = threading.Lock()    # 主循环/子代理/队友同时请求确认时排队
_auto_approve = False               # 用户输入 a 后置真：本次会话命令不再逐条确认
_tmp_files = []                     # 大输出落盘的临时文件，退出时清理
_failure_streak = 0                 # 连续工具失败次数（失败熔断的账本）
_fetch_fail_counts = {}             # 每个 URL 的失败次数（重复失败拦截用）

# web_fetch 视为"文本"的 Content-Type 特征；命不中的按二进制拦截
_FETCH_TEXTISH = ("text", "html", "json", "xml", "javascript", "charset")
# 抓这些主机名没有意义：本机幻觉地址 + 连通性测试专用域名
_BLOCKED_FETCH_HOSTS = {"localhost", "127.0.0.1", "::1",
                        "example.com", "example.org", "example.net"}

# 高危命令硬拦截清单：宁可误伤也不删库，命中直接拒绝执行
DENY_PATTERNS = [
    r"\bformat\b",    # 格式化磁盘
    r"\bdiskpart\b",  # 磁盘分区
    r"\bmkfs\b",      # 类 Unix 格式化
    r"\bshutdown\b",  # 关机/重启
    r"\brd\s+/s\b",   # cmd 递归删目录
    r"\bdel\s+/[sq]", # cmd 静默/递归删文件
    r"rm\s+-[a-z]*r", # rm -r / rm -rf
]

# 能力声明已合并进文件底部的 TOOLS 注册表；TOOL_CAPABILITIES 是由它派生的视图（见底部）
DEFAULT_CAPABILITIES = {"read_only": False, "concurrent_safe": False, "risk": "write"}


def capabilities_of(name: str) -> dict:
    """读工具的能力声明；未登记的一律最保守（不可并发、按写处理）"""
    return {**DEFAULT_CAPABILITIES, **TOOL_CAPABILITIES.get(name, {})}


def iter_tool_batches(blocks: list):
    """按能力声明把工具调用分批：连续的并发安全工具合成一个并行批，其余各自串行。
    产出 ("parallel", [block, ...]) 或 ("serial", block)。
    以前只有 dispatch_subagent 能并发（按名字特判）；现在调度看声明不看名字"""
    i = 0
    while i < len(blocks):
        if capabilities_of(blocks[i].name)["concurrent_safe"]:
            batch = []
            while i < len(blocks) and capabilities_of(blocks[i].name)["concurrent_safe"]:
                batch.append(blocks[i])
                i += 1
            yield ("parallel", batch)
        else:
            yield ("serial", blocks[i])
            i += 1


# 工具声明已合并进文件底部的 TOOLS 注册表；CLIENT_TOOLS 是由它派生的 API 视图（见底部）

SERVER_TOOLS = [
    {
        "type": "web_search_20250305",  # 工具类型标识，带日期的版本号，协议规定的固定写法
        "name": "web_search",           # 模型看到的工具名
        "max_uses": 2,                  # 单次请求最多搜索几次：k3 有逢轮必搜的习惯，收严控成本
    }
]


def _norm(path: str) -> str:
    """路径归一化（绝对化 + Windows 大小写/斜杠统一），先读后写台账的 key"""
    return os.path.normcase(str(Path(path).resolve()))


def _confirm_tool_call(name: str, tool_input: dict) -> bool:
    """risk=exec 的工具执行前请用户点头（原来是 run_command 私有的确认逻辑，
    推广成元数据驱动：任何标 exec 的工具都走这里）。
    主循环、子代理、队友线程共用一把锁；输入 a 后本次会话不再询问"""
    global _auto_approve
    with _confirm_lock:
        if _auto_approve:
            return True
        detail = tool_input.get("command") or str(tool_input)[:80]
        answer = ui.ask(f"[执行确认] {name}: {detail}\n允许执行？[y]允许 / [n]拒绝 / [a]本次会话全部允许: ")
        if answer == "a":
            _auto_approve = True
            return True
        return answer in ("y", "yes")


def run_command(command: str) -> str:
    """在终端执行一条 shell 命令并返回输出；高危命令拦截（确认由调度层的权限引擎统一管）"""
    for pattern in DENY_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"(已拦截：命令命中高危模式 {pattern!r}，本系统禁止执行。如确有需要，请让用户手动执行)"
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            errors="replace", timeout=30,
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > CMD_OUTPUT_LIMIT:
            return _offload_output(output)
        return output or "(命令执行完毕，无输出)"
    except subprocess.TimeoutExpired:
        return "(命令执行超时)"


def _offload_output(output: str) -> str:
    """大输出落盘：返回头部预览 + 临时文件路径，完整内容不丢、上下文不爆"""
    fd, tmp_path = tempfile.mkstemp(prefix="agent_cmd_", suffix=".log", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(output)
    _tmp_files.append(tmp_path)
    return (output[:CMD_HEAD_PREVIEW]
            + f"\n\n...(输出共 {len(output)} 字符，此处为前 {CMD_HEAD_PREVIEW} 字符预览；"
              f"完整内容已保存到 {tmp_path}，可用 read_file 分段读取)")


def _cleanup_tmp_files():
    for tmp in _tmp_files:
        try:
            os.remove(tmp)
        except OSError:
            pass


atexit.register(_cleanup_tmp_files)


def read_file(path: str, offset: int = 1, limit: int = READ_MAX_LINES) -> str:
    """读取文本文件内容。文件不存在也算"读过"（已确认当前状态），此后允许 write_file"""
    try:
        p = Path(path)
        if not p.exists():
            _read_paths.add(_norm(path))
            return f"(文件不存在: {p})"
        raw = p.read_bytes()
        if b"\x00" in raw[:8192]:
            _read_paths.add(_norm(path))  # 二进制也算"读过"（已确认它是二进制），此后允许覆盖/下载替换
            return "(二进制文件，不支持读取)"
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(offset - 1, 0)
        selected = lines[start:start + max(limit, 1)]
        body = "\n".join(selected) or "(空文件)"
        _read_paths.add(_norm(path))
        if start + len(selected) < len(lines):
            body += (f"\n...(共 {len(lines)} 行，当前为第 {start + 1}-{start + len(selected)} 行；"
                     f"用 offset 参数继续读取)")
        return body
    except OSError as e:
        return f"(读取失败: {e})"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """查找替换式局部编辑：old_string 必须唯一出现；先读后写强制校验"""
    try:
        p = Path(path)
        if not p.exists():
            return f"(文件不存在: {p}；创建新文件请用 write_file)"
        if _norm(path) not in _read_paths:
            return (f"错误：你还没有用 read_file 读过 {p}。"
                    f"为防止凭记忆乱改，请先 read_file 确认最新内容再编辑")
        text = p.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_string)
        if count == 0:
            return "错误：未找到要替换的内容（old_string 不匹配）。请 read_file 确认最新内容后重试"
        if count > 1:
            return f"错误：old_string 在文件中出现了 {count} 次，无法确定替换位置。请多带上下文使其唯一"
        p.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
        return f"已编辑 {p}（替换 1 处，{len(old_string)} → {len(new_string)} 字符）"
    except OSError as e:
        return f"(编辑失败: {e})"


def list_dir(path: str = ".", recursive: bool = False) -> str:
    """列目录：子目录带 / 后缀，文件带字节数；默认只列一层，超量截断"""
    try:
        root = Path(path)
        if not root.is_dir():
            return f"(目录不存在: {root})"
        iterator = root.rglob("*") if recursive else root.iterdir()
        entries = []
        for entry in sorted(iterator):
            rel = entry.relative_to(root)
            if entry.is_dir():
                entries.append(f"{rel}/")
            else:
                entries.append(f"{rel} ({entry.stat().st_size}B)")
            if len(entries) >= LIST_MAX_ENTRIES:
                entries.append(f"...(超过 {LIST_MAX_ENTRIES} 项已截断，请缩小 path 或关掉 recursive)")
                break
        return "\n".join(entries) or "(空目录)"
    except OSError as e:
        return f"(列目录失败: {e})"


def load_skill(skill_name: str) -> str:
    """按名称加载 skill 的完整说明"""
    return loader.get_skill_body(skill_name)

def web_fetch(url: str) -> str:
    # 幻觉 URL 守卫：空主机名/保留地址直接拒发，不浪费请求
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return "(抓取失败: URL 缺少主机名，是编出来的地址。请从搜索结果或用户给的链接里选真实地址)"
    if host in _BLOCKED_FETCH_HOSTS:
        return f"(抓取失败: {host} 是保留/测试地址，抓了也没内容。请用真实数据源)"
    # 重复失败拦截：同一地址连跪两次，第三次拒发
    if _fetch_fail_counts.get(url, 0) >= 2:
        return "(抓取失败: 该地址已连续失败 2 次，禁止再试。请换数据源，或用 web_search 找新链接)"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        # 二进制守卫：乱码不进上下文，指路 download_file / view_image
        content_type = resp.headers.get("Content-Type", "").lower()
        if content_type and not any(t in content_type for t in _FETCH_TEXTISH):
            return (f"(抓取失败: 该地址返回二进制资源（{content_type.split(';')[0]}），"
                    f"web_fetch 只能抓文本。保存文件用 download_file，看图片用 view_image)")
        resp.encoding = resp.apparent_encoding   # 按真实编码解码，避免中文乱码
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()                      # 删掉没有正文价值的部分
        _fetch_fail_counts.pop(url, None)        # 成功：清除该地址的失败记录
        return soup.get_text(separator="\n", strip=True)[:8000] or "(页面无文本内容)"
    except requests.RequestException as e:
        _fetch_fail_counts[url] = _fetch_fail_counts.get(url, 0) + 1
        return f"(抓取失败: {e})"


def record_tool_result(result) -> int:
    """工具结果记账：返回当前连续失败次数（成功则归零）。agent 主循环用它做失败熔断"""
    global _failure_streak
    if ui.is_error_result(result):
        _failure_streak += 1
    else:
        _failure_streak = 0
    return _failure_streak


def reset_failure_streak():
    """熔断介入后清零：给模型一次改过的机会，别让它永远背着失败记录"""
    global _failure_streak
    _failure_streak = 0


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def download_file(url: str, save_path: str, max_mb: int = 50) -> str:
    """下载二进制资源（图片/PDF/压缩包等）到本地。
    流式写盘：大文件不一次性进内存；超过 max_mb 中止并删掉半截文件，不留垃圾"""
    try:
        p = Path(save_path)
        if p.exists() and _norm(save_path) not in _read_paths:
            return (f"错误：{p} 已存在，但你还没有用 read_file 读过它。"
                    f"为防止误覆盖，请先 read_file 或换个文件名")
        with requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"},
                          stream=True) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "未知").split(";")[0].strip()
            limit = max(1, int(max_mb)) * 1024 * 1024
            p.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            oversized = False
            with p.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    size += len(chunk)
                    if size > limit:
                        oversized = True
                        break
                    f.write(chunk)
        if oversized:
            p.unlink(missing_ok=True)  # 超限：删掉半截文件，避免留下打不开的残品
            return f"(下载中止：文件超过 {max_mb}MB 上限，不完整文件已删除)"
        _read_paths.add(_norm(save_path))  # 刚下载的内容来源明确，视为已读
        return f"已下载 → {p}（{_fmt_size(size)}，{content_type}）"
    except requests.RequestException as e:
        return f"(下载失败: {e})"


MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 单张图片上限（Anthropic 协议限制 + token 成本）

_IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def _detect_media_type(head: bytes):
    """按魔数识别图片格式（不信扩展名，扩展名可以乱改）"""
    for magic, media_type in _IMAGE_MAGIC:
        if head.startswith(magic):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def view_image(path: str):
    """读取本地图片并返回图片块载荷；agent 主循环会把它作为 image 内容块放进 tool_result，
    模型借此"看见"图片。失败返回错误字符串（与其他工具的错误约定一致）"""
    try:
        p = Path(path)
        if not p.exists():
            return f"(文件不存在: {p})"
        data = p.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            return (f"(图片过大: {_fmt_size(len(data))}，超过 5MB 上限。"
                    f"可先用 code_sandbox 装 Pillow 压缩后再查看)")
        media_type = _detect_media_type(data[:16])
        if not media_type:
            return "(无法识别的图片格式：仅支持 PNG/JPEG/GIF/WebP)"
        _read_paths.add(_norm(path))
        return {"_image": True, "media_type": media_type,
                "data": base64.b64encode(data).decode(),
                "note": f"图片 {p.name}（{_fmt_size(len(data))}，{media_type}）"}
    except OSError as e:
        return f"(读取失败: {e})"


def to_tool_result_content(result):
    """tool_result 的 content 构造：view_image 的图片载荷转成 image 内容块（模型才看得见），
    其余工具结果原样转字符串"""
    if isinstance(result, dict) and result.get("_image"):
        return [
            {"type": "text", "text": result["note"]},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": result["media_type"],
                                         "data": result["data"]}},
        ]
    return str(result)

def update_todos(todos: list) -> str:
    """创建或更新 todolist（薄壳：干活的是 plan.todo_list）"""
    return todo_list.update(todos)


def write_file(path: str, content: str, mode: str = "overwrite") -> str:
    """把文本内容写入文件，父目录不存在则自动创建。
    先读后写硬校验：改动已存在的文件（覆盖或追加）前，必须先用 read_file 读过它"""
    try:
        p = Path(path)
        if p.exists() and _norm(path) not in _read_paths:
            return (f"错误：{p} 已存在，但你还没有用 read_file 读过它。"
                    f"为防止凭幻觉覆盖，请先 read_file 再决定如何修改"
                    f"（即使你用 run_command 看过内容，也必须用 read_file 读一次）")
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with p.open("a", encoding="utf-8") as f:  # "a"=追加，写指针在文件尾
                f.write(content)
            action = "已追加"
        else:
            p.write_text(content, encoding="utf-8")
            action = "已写入"
        _read_paths.add(_norm(path))  # 刚写过的内容模型自己知道，视为已读
        return f"{action} {p}（本段 {len(content)} 字符）"
    except OSError as e:
        return f"(写入失败: {e})"


def code_sandbox(language: str, code: str, packages: list = None,
                 timeout: int = sandbox.DEFAULT_TIMEOUT, reset: bool = False) -> str:
    """代码沙箱（薄壳：干活的是 sandbox.run_code）；大输出同样落盘"""
    out = sandbox.run_code(language=language, code=code,
                           packages=packages, timeout=timeout, reset=reset)
    return _offload_output(out) if len(out) > CMD_OUTPUT_LIMIT else out

# ============ 工具注册表：每个工具的全部信息在一条里 ============
#   name/description/input_schema  → 发给 API 的声明
#   capabilities                   → 能力声明（调度用：只读/并发安全/风险级）
#   handler                        → 执行体（dispatch 用）
# 新增工具 = 在上面函数区写 handler，再在下面加一条；三个视图自动派生，调度器零改动。
TOOLS = [
    {
        "name": "run_command",
        "description": (
            "在终端执行一条 shell 命令并返回输出。超时 30 秒；"
            "输出过长时自动截断，完整内容会存到临时文件（可用 read_file 分段读取）；"
            "执行前会向用户请求确认，命中高危模式的命令会被直接拦截。"
            "跑代码、装依赖、跑测试请优先用 code_sandbox（隔离环境，无需确认）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
            },
            "required": ["command"],
        },
        "capabilities": {"read_only": False, "concurrent_safe": False, "risk": "exec"},
        "handler": run_command,
    },
    {
        "name": "read_file",
        "description": (
            "读取文本文件内容，大文件用 offset/limit 分段读。"
            "修改任何已有文件之前，必须先用本工具读取目标文件（write_file/edit_file 会强制校验）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对当前工作目录或绝对路径"},
                "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
                "limit": {"type": "integer", "description": f"最多读取的行数，默认 {READ_MAX_LINES}"},
            },
            "required": ["path"],
        },
        "capabilities": {"read_only": True, "concurrent_safe": True, "risk": "read"},
        "handler": read_file,
    },
    {
        "name": "edit_file",
        "description": (
            "查找替换式局部编辑已有文件：把 old_string 替换为 new_string。"
            "old_string 必须在文件中唯一出现（不唯一就多带上下文）；调用前必须先 read_file 该文件。"
            "创建新文件或整份重写请用 write_file。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要编辑的文件路径"},
                "old_string": {"type": "string", "description": "要被替换的原文，必须在文件中唯一出现"},
                "new_string": {"type": "string", "description": "替换后的新内容"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        "capabilities": {"read_only": False, "concurrent_safe": False, "risk": "write"},
        "handler": edit_file,
    },
    {
        "name": "list_dir",
        "description": (
            "列出目录内容：子目录带 / 后缀，文件带字节数。"
            "默认只列一层；recursive=true 时递归列出（结果过多会自动截断）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认当前目录"},
                "recursive": {"type": "boolean", "description": "是否递归列出所有层级，默认 false"},
            },
        },
        "capabilities": {"read_only": True, "concurrent_safe": True, "risk": "read"},
        "handler": list_dir,
    },
    {
        "name": "code_sandbox",
        "description": (
            "在隔离沙箱中运行 Python/Node 代码：独立工作目录、剥离密钥的环境变量、"
            "无 shell 直接执行、超时自动终止进程树。可用 packages 预装依赖（PyPI/npm）。"
            "产物保留在沙箱工作区（可用 read_file 读取）。"
            "跑代码、装依赖、跑测试一律用本工具；系统级命令才用 run_command。"
            "注意：这是工作区级隔离，不是容器级安全沙箱。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": ["python", "node"],
                             "description": "用 python 还是 node(js) 运行"},
                "code": {"type": "string", "description": "要执行的完整源代码"},
                "packages": {"type": "array", "items": {"type": "string"},
                             "description": "需要预装的依赖包名（pip / npm），可空"},
                "timeout": {"type": "integer",
                            "description": f"秒数上限，默认 {sandbox.DEFAULT_TIMEOUT}，最大 {sandbox.MAX_TIMEOUT}"},
                "reset": {"type": "boolean",
                          "description": "是否先清空沙箱工作区再运行，默认 false"},
            },
            "required": ["language", "code"],
        },
        "capabilities": {"read_only": False, "concurrent_safe": False, "risk": "sandbox"},
        "handler": code_sandbox,
    },
    {
        "name": "load_skill",
        "description": "按名称加载某个 skill 的完整说明。当任务与 system prompt 中某个 skill 的描述匹配时，先调用本工具获取详细步骤，再按步骤执行",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "skill 的名称（见 system prompt 中的可用列表）"},
            },
            "required": ["skill_name"],
        },
        "capabilities": {"read_only": True, "concurrent_safe": True, "risk": "read"},
        "handler": load_skill,
    },
    {
        "name": "web_fetch",
        "description": "抓取指定 URL的网页内容，返回纯文本正文。当用户给出具体网址，或 web_search 搜到值得深读的链接时使用",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的完整网址，需包含 http:// 或 https://"},
            },
            "required": ["url"],
        },
        "capabilities": {"read_only": True, "concurrent_safe": True, "risk": "read"},
        "handler": web_fetch,
    },
    {
        "name": "download_file",
        "description": (
            "下载二进制资源（图片、PDF、压缩包等）并保存到本地文件。"
            "web_fetch 只能抓文本；需要把图片等二进制文件存盘时用本工具。"
            "返回保存路径、文件大小和类型，不返回内容本身。"
            "覆盖已存在的文件前必须先 read_file 该文件（强制校验）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要下载的完整网址，需包含 http:// 或 https://"},
                "save_path": {"type": "string", "description": "保存到本地的路径，相对当前工作目录或绝对路径"},
                "max_mb": {"type": "integer",
                           "description": "允许的最大文件大小（MB），默认 50；超过则中止下载并删除不完整文件"},
            },
            "required": ["url", "save_path"],
        },
        "capabilities": {"read_only": False, "concurrent_safe": False, "risk": "write"},
        "handler": download_file,
    },
    {
        "name": "view_image",
        "description": (
            "查看一张本地图片的内容（把图片送入上下文，你就能『看到』它）。"
            "支持 PNG/JPEG/GIF/WebP，单张不超过 5MB。"
            "典型用法：download_file 下载图片后理解内容、查看设计稿/截图/照片。"
            "注意：图片较耗 token，一次只看需要的几张；过大的图先用 code_sandbox 装 Pillow 压缩。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "本地图片文件路径"},
            },
            "required": ["path"],
        },
        "capabilities": {"read_only": True, "concurrent_safe": True, "risk": "read"},
        "handler": view_image,
    },
    {
        "name": "update_todos",
        "description": (
            "创建或更新当前任务的 todolist。"
            "传入完整的 todos 数组（每次都是全量覆盖，而非增量）。"
            "用于：拆解多步骤任务、推进任务状态（pending → in_progress → completed）。"
            "约束：同一时间至多一个任务为 in_progress；必须按列表顺序逐个执行；"
            "只有当前处于 in_progress 的任务才能标记为 completed（先做完，后打勾）。"
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
        "capabilities": {"read_only": False, "concurrent_safe": False, "risk": "write"},
        "handler": update_todos,
    },
    {
        "name": "write_file",
        "description": (
            "把文本内容写入指定路径的文件，父目录不存在会自动创建。"
            "生成代码、HTML、Markdown 等文件时必须用本工具，禁止用 run_command 拼 echo 写文件。"
            "覆盖已存在的文件前必须先 read_file 该文件（强制校验，防止凭幻觉覆盖）。"
            "大文件必须分段写入：第一段用默认的 overwrite，后续每段用 mode=append 续写，"
            "每段控制在 3000 字符以内，避免单次输出过长被截断。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，相对当前工作目录或绝对路径"},
                "content": {"type": "string", "description": "本次写入的文本内容（分段写入时是本段的内容）"},
                "mode": {"type": "string", "enum": ["overwrite", "append"],
                         "description": "overwrite=覆盖写（默认）；append=追加到文件末尾，用于大文件分段写入"},
            },
            "required": ["path", "content"],
        },
        "capabilities": {"read_only": False, "concurrent_safe": False, "risk": "write"},
        "handler": write_file,
    },
]

# ---- 派生视图：外部模块的读取口不变（agent / subagent / team 无感） ----
# CLIENT_TOOLS      发给 API 的纯声明（剥掉能力和 handler，它们不出门）
# TOOL_CAPABILITIES 调度用的能力表
# HANDLERS          分发用的执行体表
CLIENT_TOOLS = [{k: t[k] for k in ("name", "description", "input_schema")} for t in TOOLS]
TOOL_CAPABILITIES = {t["name"]: t["capabilities"] for t in TOOLS}
HANDLERS = {t["name"]: t["handler"] for t in TOOLS}


def dispatch_tool(name: str, tool_input: dict) -> str:
    """按工具名分发调用，agent 主循环无需关心具体有哪些工具。
    权限引擎在这里统一拦截：risk=exec 的工具先确认再执行（原来是 run_command 自己管自己）"""
    handler = HANDLERS.get(name)
    if not handler:
        return f"错误：未知工具 '{name}'"
    if capabilities_of(name)["risk"] == "exec" and not _confirm_tool_call(name, tool_input):
        return "(用户拒绝了本次执行。请向用户说明意图并征得同意，或换用其他方式完成)"
    try:
        return handler(**tool_input)
    except TypeError as e:
        return f"错误：工具 '{name}' 参数不正确：{e}"
