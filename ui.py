"""终端 UI 层：事件订阅者 + 输入服务。

两个方向：
- 出（循环 → 屏幕）：订阅 events 总线，把所有事件渲染到终端。
  前缀、换行、分隔线、状态栏等渲染规则全部在这里从事件推导，循环一无所知。
- 入（屏幕 → 循环）：输入服务 user_input() / ask()，读输入前后自己熄灭/恢复状态行。

想换皮肤（改色/改布局）只动这里；想加消费者（日志/TUI）不需要动这里，
再 subscribe 一个即可。
"""
import os
import sys
import threading
import time

try:
    from rich.console import Console
    from rich.text import Text
    _RICH = True
except ImportError:  # 没装 rich 时全部退化为纯文本输出，agent 照跑
    _RICH = False

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    _PT = True
except ImportError:
    _PT = False

from events import subscribe

_pt_session = None  # 惰性创建：import 时就建 PromptSession 会在无终端环境（管道/CI）直接报错

# 老 GBK 控制台打印特殊字符会 UnicodeEncodeError，replace 成 ? 也不崩
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

STYLE = {
    "user": "bold cyan",
    "agent": "bold green",
    "tool": "yellow",
    "dim": "dim",
    "ok": "green",
    "err": "bold red",
    "wait": "blue",
}

# UI_FORCE_TERMINAL=1 是调试钩子：管道里也强制当真终端渲染（复现/验证 Live 行为用）。
# 注意 force_terminal 的三态语义：True=强制终端，False=强制非终端（颜色全灭），None=自动检测。
_force_terminal = True if os.environ.get("UI_FORCE_TERMINAL") == "1" else None
console = Console(highlight=False, force_terminal=_force_terminal) if _RICH else None

# 工具结果里判定失败的前缀（与 tools.py / sandbox.py 的错误约定对齐）
_ERROR_PREFIXES = ("错误", "Error", "(已拦截", "(读取失败", "(写入失败", "(编辑失败",
                   "(命令执行超时", "(依赖安装失败", "(沙箱执行失败", "(抓取失败",
                   "(不支持", "(未检测", "(非法", "(超时", "(列目录失败", "(用户拒绝",
                   "(下载失败", "(下载中止", "(文件不存在", "(图片过大", "(无法识别")


def is_error_result(text) -> bool:
    """工具结果是否失败（按错误前缀约定）。熔断记账和 tool_end 渲染都靠它"""
    return str(text).strip().startswith(_ERROR_PREFIXES)


# ============ 状态栏（UI 内部状态，循环不可见） ============

class StatusBar:
    """手绘单行状态栏：模型 | token 累计 | todo 进度 | 当前活动+耗时。
    不用 rich.Live——Live.stop() 要 join 它的内部刷新线程，持锁 join 撞上
    快速启停就是偶发永久阻塞（本仓库实测复现：start→stop→start 会卡死）。
    手绘方案只有一个 daemon 线程定期重画，永不 join，从设计上消除整个死锁类别。
    状态数据全部从事件推导（见 _handle），外部模块碰不到它。"""

    def __init__(self):
        self.model = ""
        self.total_in = 0
        self.total_cache = 0
        self.todos = None          # (done, total) 或 None
        self.activity = ""
        self._activity_since = None
        self._visible = False      # 当前是否画在屏幕上
        self._enabled = False      # 会话级开关（start/stop）
        self._ticker = None

    # ---- 会话生命周期 ----
    def start(self, model: str = ""):
        self._enabled = True
        if model:
            self.model = model
        self._ensure_ticker()
        self.show()

    def stop(self):
        self._enabled = False
        self.hide()

    # ---- 显隐（pause/resume 别名对齐旧接口） ----
    def show(self):
        if self._enabled and _RICH and console.is_terminal:
            self._visible = True
            self._draw()

    def hide(self):
        if self._visible:
            self._erase()
        self._visible = False

    pause = hide    # 正文开流/读输入前：熄灭
    resume = show   # 正文结束/输入完毕：亮起

    # ---- 数据（全部由事件喂） ----
    def set_activity(self, text: str):
        self.activity = text
        self._activity_since = time.time() if text else None
        self.show()

    def add_tokens(self, n_in: int, n_cache: int):
        self.total_in += n_in or 0
        self.total_cache += n_cache or 0
        self.show()

    def set_todos_from(self, items: list):
        if not items:
            self.todos = None
        else:
            done = sum(1 for t in items if t.get("status") == "completed")
            self.todos = (done, len(items))
        self.show()

    @staticmethod
    def _fmt(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    # ---- 输出配合：所有走屏幕的内容都过这里 ----
    def around_print(self, render):
        """先擦掉状态行，打印内容，再把状态行画回来——内容永远在它上方滚"""
        was = self._visible
        if was:
            self._erase()
        render()
        if was:
            self._draw()

    # ---- 绘制原语 ----
    def _line(self) -> str:
        parts = [f"● {self.model or '?'}",
                 f"{self._fmt(self.total_in)} in / {self._fmt(self.total_cache)} cache"]
        if self.todos:
            parts.append(f"todos {self.todos[0]}/{self.todos[1]}")
        if self.activity:
            elapsed = time.time() - self._activity_since if self._activity_since else 0
            parts.append(f"{self.activity} {elapsed:.0f}s")
        return " | ".join(parts)

    def _draw(self):
        """\r 回行首 + ANSI 清行 + 重写（不换行），光标留在行尾"""
        sys.stdout.write("\r\x1b[K\x1b[2m" + self._line() + "\x1b[0m")
        sys.stdout.flush()

    def _erase(self):
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()

    def _ensure_ticker(self):
        """耗时秒数靠 daemon 线程每 0.5s 重画。daemon 永不 join——
        rich Live 死锁的根源正是 stop 时 join 刷新线程，这里直接避开该设计"""
        if self._ticker is None and _RICH and console.is_terminal:
            def _tick():
                while True:
                    time.sleep(0.5)
                    if self._visible and self._activity_since:
                        self._draw()
            self._ticker = threading.Thread(target=_tick, daemon=True)
            self._ticker.start()


_status = StatusBar()


# ============ 渲染助手（私有） ============

def _dim(text):
    def _render():
        if _RICH:
            console.print(Text(str(text), style=STYLE["dim"]))
        else:
            print(text)
    _status.around_print(_render)


def _prefix():
    if _RICH:
        console.print(Text("● Jimmy ", style=STYLE["agent"]), end="")
    else:
        print("[Agent]: ", end="", flush=True)


def _out(text):
    """流式正文增量：console.out 不解析 markup，模型输出含 [] 也不会炸"""
    if _RICH:
        console.out(text, end="")
    else:
        print(text, end="", flush=True)


def _newline():
    (console.print if _RICH else print)()


def _divider():
    """console.rule 画满宽横线，每次调用现场量终端宽度（拉伸后下一回合自动适配）"""
    def _render():
        if _RICH:
            console.rule(style=STYLE["dim"])
        else:
            print("-" * 60)
    _status.around_print(_render)


def _error(source, detail):
    text = f"[{source} 错误] {detail}"
    def _render():
        if _RICH:
            console.print(Text(text, style=STYLE["err"]))
        else:
            print(text)
    _status.around_print(_render)


def _summarize_input(name: str, tool_input) -> str:
    """从工具参数里挑关键信息做一行摘要，不再是整坨 dict"""
    if not isinstance(tool_input, dict):
        return str(tool_input)[:80]
    if name == "run_command":
        return tool_input.get("command", "")[:100]
    if name in ("read_file", "edit_file", "write_file"):
        return tool_input.get("path", "")
    if name == "list_dir":
        return tool_input.get("path", ".")
    if name == "web_fetch":
        return tool_input.get("url", "")[:100]
    if name == "download_file":
        return f"{tool_input.get('url', '')[:70]} → {tool_input.get('save_path', '')}"
    if name == "view_image":
        return tool_input.get("path", "")
    if name == "load_skill":
        return tool_input.get("skill_name", "")
    if name == "update_todos":
        return f"{len(tool_input.get('todos', []))} 项"
    if name == "code_sandbox":
        pkgs = tool_input.get("packages") or []
        return (f"{tool_input.get('language', '')} {len(tool_input.get('code', ''))}字符"
                + (f" 装{pkgs}" if pkgs else ""))
    if name == "dispatch_subagent":
        label = tool_input.get("purpose") or tool_input.get("task", "")
        return f"{tool_input.get('agent_type', '')}: {label[:60]}"
    return str(tool_input)[:80]


def _tool_call(name: str, tool_input) -> None:
    summary = _summarize_input(name, tool_input)
    def _render():
        if _RICH:
            line = Text()
            line.append("> ", style=STYLE["tool"])
            line.append(name, style=STYLE["tool"])
            line.append(f"  {summary}", style=STYLE["dim"])
            console.print(line)
        else:
            print(f"  > {name}  {summary}")
    _status.around_print(_render)


def _tool_end(result) -> None:
    # view_image 的图片载荷：只显示 note，不糊 base64。
    # 载荷契约的知情者在这里（渲染层），循环只负责把 result 原样抛出来
    if isinstance(result, dict) and result.get("_image"):
        result = result["note"]
    lines = str(result).strip().splitlines()
    first = lines[0] if lines else ""
    ok = not is_error_result(result)
    def _render():
        if _RICH:
            line = Text()
            line.append("√ " if ok else "× ", style=STYLE["ok" if ok else "err"])
            line.append(first[:120], style=STYLE["dim"])
            console.print(line)
        else:
            print(("  √ " if ok else "  × ") + first[:120])
    _status.around_print(_render)


_TODO_STYLE = {
    "completed": ("√", "green"),
    "in_progress": (">", "yellow"),
    "pending": ("-", "dim"),
}


def _render_todos(todos: list):
    if not todos:
        return
    done = sum(1 for t in todos if t["status"] == "completed")

    def _render():
        if _RICH:
            console.print(Text(f"进度 {done}/{len(todos)} 已完成", style=STYLE["dim"]))
        else:
            print(f"进度 {done}/{len(todos)} 已完成")
        for t in todos:
            icon, style = _TODO_STYLE.get(t.get("status"), ("?", "dim"))
            text = f"  {icon} {t.get('id')}. {t.get('content', '')}"
            if _RICH:
                console.print(Text(text, style=style))
            else:
                print(text)
    _status.around_print(_render)


# ============ 事件订阅者：每个事件怎么变成屏幕上的东西，全部在这里决定 ============

_in_text = False  # 是否正处于一段正文流中（决定首个 delta 打前缀、非 text 事件补换行）


def _handle(event):
    global _in_text
    t = event.type
    # 正文流被任何非 text 事件终结：补换行 + 状态行恢复
    if t != "text" and _in_text:
        _newline()
        _status.resume()
        _in_text = False

    p = event.payload
    if t == "text":
        if not _in_text:
            _status.pause()   # 正文开流：熄灭状态行（Live 重绘会冲花半截行）
            _prefix()
            _in_text = True
        _out(p["delta"])
    elif t == "tool_start":
        _status.set_activity(f"执行 {p['name']}")
        _tool_call(p["name"], p.get("input"))
    elif t == "tool_end":
        _tool_end(p["result"])
    elif t == "error":
        _error(p.get("source", "?"), p.get("detail", ""))
    elif t == "info":
        _dim(p["text"])
    elif t == "usage":
        _status.add_tokens(p.get("input", 0), p.get("cache_read", 0))
        _dim(f"[tokens] input={p.get('input', 0)} read={p.get('cache_read', 0)}")
    elif t == "todo":
        _status.set_todos_from(p["items"])
        _render_todos(p["items"])
    elif t == "activity":
        _status.set_activity(p["text"])
    elif t == "turn_end":
        _status.set_activity("")
        _divider()


subscribe(_handle)


# ============ 输入服务（屏幕 → 循环的方向） ============

def start_session(model: str):
    """会话开始：状态行亮起（生命周期调用，不是事件）"""
    _status.start(model)


def stop_session():
    _status.stop()


def user_input() -> str:
    """主输入服务。读前熄状态行；读后恢复并补换行——
    prompt_toolkit 接受输入后不换行，正文不该接在提示符那行后面"""
    global _pt_session
    _status.pause()
    try:
        if _PT and sys.stdin.isatty():
            if _pt_session is None:
                _pt_session = PromptSession(history=InMemoryHistory())
            return _pt_session.prompt("[User]: ")
        return input("[User]: ")
    finally:
        _status.resume()
        _newline()


def ask(prompt: str) -> str:
    """确认类输入服务（命令确认等）：显示提示 + 读一行回答"""
    _status.pause()
    try:
        _dim(prompt)
        return input().strip().lower()
    finally:
        _status.resume()
        _newline()
