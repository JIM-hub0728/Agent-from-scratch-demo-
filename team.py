"""
Agent Team 模块：持久队友 + 文件 inbox 消息总线。
与 subagent.py 的分工：子代理一次性（办完即散，只回传摘要）；
队友持久常驻（独立线程，有名字/角色/状态/inbox，可持续协作）。
通信：每个成员一个 .team/inbox/<名字>.jsonl，发送=追加一行，读取=读完清空。
名册持久化在 .team/config.json，进程重启后名册还在（但线程要重新 spawn）
"""

import threading
import json
import time
from pathlib import Path
import tools  # 复用基础工具的 schema 和 dispatch_tool，队友不重复实现工具

TEAM_DIR = Path(".team")
INBOX_DIR = TEAM_DIR / "inbox"

# 消息类型白名单：普通消息 / 广播 / 关机请求与应答（协议化的退出机制）
VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request", "shutdown_response"}
RUNTIME_STATUSES = {"idle", "working"}  # 线程活着才有的运行时状态


class MessageBus:
    """每个成员一个 JSONL inbox 文件。追加写天然线程安全（单次 open+write 很短）。"""

    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str, msg_type: str = "message") -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: invalid msg_type '{msg_type}'"
        msg = {"type": msg_type, "from": sender,
               "content": content, "timestamp": time.time()}
        with (self.dir / f"{to}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"已送达 {to} 的 inbox"

    def read_inbox(self, name: str) -> list:
        path = self.dir / f"{name}.jsonl"
        if not path.exists():
            return []
        messages = [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines() if line.strip()]
        path.write_text("", encoding="utf-8")  # 读完即清空：消息只被消费一次
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:  # 广播不发给自己
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"已广播给 {count} 位队友"


BUS = MessageBus(INBOX_DIR)


class TeammateManager:
    """管理持久队友：名册（config.json）、状态、各自的线程。"""

    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}            # name -> threading.Thread（只在内存里，不落盘）
        self.lock = threading.Lock() # 队友线程和主线程都会改名册，写操作要串行化
        self.client = None           # 由 agent.py 通过 init() 注入，模块不持有 API 配置
        self._mark_stale_members_offline()

    def _load_config(self) -> dict:
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _mark_stale_members_offline(self):
        """进程重启后 config 还在，但旧线程已死。
        启动时把上次遗留的 idle/working 一律改成 offline，避免名册撒谎。"""
        changed = False
        for m in self.config.get("members", []):
            if m.get("status") in RUNTIME_STATUSES:
                m["status"] = "offline"
                changed = True
        if changed:
            self._save_config()

    def _find_member(self, name: str):
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        with self.lock:
            m = self._find_member(name)
            if m:
                m["status"] = status
                self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """召入队友；同名队友还活着则只把新差事塞进 inbox，offline 则重启线程。"""
        name = name.strip()
        if not name:
            return "Error: name 不能为空"
        role = role.strip() or "teammate"
        with self.lock:
            member = self._find_member(name)
            if member:
                running = self.threads.get(name)
                if running and running.is_alive():
                    BUS.send("lead", name, prompt)  # 人在：不重启，直接递信
                    member["role"] = role
                    member["status"] = "working"
                    self._save_config()
                    return f"'{name}' 已在队中，新差事已送入它的 inbox"
                member["role"] = role          # 名册在但线程死了（offline）：唤回
                member["status"] = "working"
            else:
                member = {"name": name, "role": role, "status": "working"}
                self.config["members"].append(member)
            self._save_config()

        thread = threading.Thread(target=self._teammate_loop,
                                  args=(name, role, prompt), daemon=True)
        self.threads[name] = thread
        thread.start()
        return f"已召入队友 '{name}'（职司：{role}），线程已启动"

    # 队友允许用的基础工具白名单：故意不含 update_todos（计划属于 lead）
    TEAMMATE_BASIC_TOOLS = ["run_command", "web_fetch", "load_skill", "write_file"]

    def _teammate_loop(self, name: str, role: str, prompt: str):
        """队友的主循环：和子代理的关键差异——while True 常驻，没活就等信。"""
        system_prompt = (
            f"你是 Jimmy 团队中的固定队友，名叫{name}，职司是{role}。\n"
            "你不是一次性子代理，而是 agent team 的持久成员。\n"
            "用 send_message 可以给 lead 或其他队友发消息，read_inbox 读取自己的 inbox。\n"
            "收到差事尽快办妥；你的最终文本会自动回禀给 lead，然后等待下一封 inbox。\n"
            "若收到 shutdown_request，回一封 shutdown_response 后停止。"
        )
        teammate_tools = (
            [t for t in tools.CLIENT_TOOLS if t["name"] in self.TEAMMATE_BASIC_TOOLS]
            + [TEAMMATE_SEND_MESSAGE, TEAMMATE_READ_INBOX]
        )
        messages = [{"role": "user", "content": prompt}]
        has_work = True  # 带着第一件差事出生

        while True:
            # 每轮先收信：新消息以 <inbox> 包裹追加进自己的上下文
            for msg in BUS.read_inbox(name):
                if msg.get("type") == "shutdown_request":
                    BUS.send(name, msg.get("from", "lead"),
                             "收到，队友线程即将停止。", "shutdown_response")
                    self._set_status(name, "shutdown")
                    return
                messages.append({"role": "user", "content":
                    "<inbox>\n" + json.dumps(msg, ensure_ascii=False) + "\n</inbox>"})
                has_work = True

            if not has_work:  # 没事干：idle 轮询等信（1 秒一次，开销可忽略）
                self._set_status(name, "idle")
                time.sleep(1)
                continue

            self._set_status(name, "working")
            for turn in range(20):  # 一轮差事最多 20 次模型调用，防失控
                try:
                    msg = self.client.messages.create(
                        model="k3",
                        system=system_prompt,
                        tools=teammate_tools,
                        messages=messages,
                        max_tokens=4096,
                    )
                except Exception as e:  # API 抖动：告诉 lead，自己回 idle 待命
                    BUS.send(name, "lead", f"Error: 队友 {name} 调用模型失败：{e}")
                    self._set_status(name, "idle")
                    has_work = False
                    break

                messages.append({"role": "assistant", "content": msg.content})

                if msg.stop_reason != "tool_use":
                    # 办完：最终文本自动回禀 lead 的 inbox，然后回 idle 待命
                    final = next((b.text for b in msg.content if b.type == "text"), "")
                    if final.strip():
                        BUS.send(name, "lead", final.strip())
                    print(f"[队友 {name}]: 办完差事，回 idle 待命")
                    self._set_status(name, "idle")
                    has_work = False
                    break

                results = []
                for b in msg.content:
                    if b.type == "tool_use":
                        output = self._exec(name, b.name, b.input)
                        print(f"  [队友·{name}·{b.name}]: {str(output)[:160]}")
                        results.append({"type": "tool_result",
                                        "tool_use_id": b.id, "content": str(output)})
                messages.append({"role": "user", "content": results})
            else:
                # for 正常跑完 20 轮（没 break）：主动上报并暂停
                BUS.send(name, "lead", f"队友 {name} 达到 20 轮上限，已暂停等待下一步指令。")
                self._set_status(name, "idle")
                has_work = False

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        """队友的工具分发：基础工具走 tools.dispatch_tool，通信工具走 BUS"""
        if tool_name in self.TEAMMATE_BASIC_TOOLS:
            return tools.dispatch_tool(tool_name, args)
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"],
                            args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), ensure_ascii=False, indent=2)
        return f"Error: unknown teammate tool '{tool_name}'"

    def list_all(self) -> str:
        with self.lock:
            if not self.config["members"]:
                return "暂无队友。"
            lines = [f"Team: {self.config.get('team_name', 'default')}"]
            for m in self.config["members"]:
                note = "（需重新 spawn 唤回）" if m["status"] == "offline" else ""
                lines.append(f"  - {m['name']}（{m['role']}）：{m['status']}{note}")
            return "\n".join(lines)

    def member_names(self) -> list:
        with self.lock:
            return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)

# ---- 队友专属的两个通信工具 schema（只发给队友，不发给 lead）----
TEAMMATE_SEND_MESSAGE = {
    "name": "send_message",
    "description": "给 lead 或其他队友发送 inbox 消息。",
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "content": {"type": "string"},
            "msg_type": {"type": "string", "enum": sorted(VALID_MSG_TYPES)},
        },
        "required": ["to", "content"],
    },
}
TEAMMATE_READ_INBOX = {
    "name": "read_inbox",
    "description": "读取并清空自己的 inbox。",
    "input_schema": {"type": "object", "properties": {}},
}

# ---- lead（主 agent）的 5 个团队工具 schema ----
TEAM_TOOLS = [
    {
        "name": "spawn_teammate",
        "description": (
            "召入一个持久队友加入 agent team。队友有名字、职司、独立线程和 inbox，"
            "适合长期项目或固定角色协作。若队友状态是 offline，也用本工具唤回。"
            "一次性短期差事请改用 dispatch_subagent。"),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "队友名字，如 researcher、writer"},
                "role": {"type": "string", "description": "队友职司，如 调研员、写作者"},
                "prompt": {"type": "string", "description": "交给该队友的第一件差事"},
            },
            "required": ["name", "role", "prompt"],
        },
    },
    {
        "name": "list_teammates",
        "description": "列出所有队友的名字、职司和状态（idle/working/offline/shutdown）。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message",
        "description": "给某位固定队友发送 inbox 消息（分派后续差事、追问、协调）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
                "msg_type": {"type": "string", "enum": sorted(VALID_MSG_TYPES)},
            },
            "required": ["to", "content"],
        },
    },
    {
        "name": "read_inbox",
        "description": "读取并清空 lead 自己的 inbox，查看队友回禀。队友是异步干活的，派完活记得稍后查 inbox。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "broadcast",
        "description": "向所有固定队友广播一条消息。",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
]


def init(client):
    """由 agent.py 启动时调用：注入 API client（队友线程调模型要用）。
    和 run_subagent(client, ...) 同一个思路：API 配置的单一来源是 agent.py。"""
    TEAM.client = client


def register_handlers():
    """把 5 个团队工具注册进 tools.HANDLERS。
    这样主循环的 dispatch_tool 不用改一行代码就能分发新工具——
    HANDLERS 字典本来就是个开放注册表，新模块登记即用。"""
    tools.HANDLERS.update({
        "spawn_teammate": lambda name, role, prompt: TEAM.spawn(name, role, prompt),
        "list_teammates": lambda: TEAM.list_all(),
        "send_message": lambda to, content, msg_type="message":
            BUS.send("lead", to, content, msg_type),
        "read_inbox": lambda:
            json.dumps(BUS.read_inbox("lead"), ensure_ascii=False, indent=2),
        "broadcast": lambda content:
            BUS.broadcast("lead", content, TEAM.member_names()),
    })
