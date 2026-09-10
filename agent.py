import os
import json
from anthropic import Anthropic
import tools
from skill_loader import loader
from memory import memory
from memory_compact import compact_history
from datetime import datetime
from plan import todo_list
from concurrent.futures import ThreadPoolExecutor
import subagent
import team

# Kimi API 配置
# 从环境变量读取 API Key，避免把密钥写死在代码里、误提交到 git。
# 本地运行前先设置环境变量 KIMI_API_KEY（Windows: set KIMI_API_KEY=你的key）
KIMI_API_KEY = os.environ["KIMI_API_KEY"]

client = Anthropic(
    api_key=KIMI_API_KEY,
    base_url="https://api.kimi.com/coding/",
)

team.init(client)          # 注入 client：队友线程调模型要用
team.register_handlers()   # 把 5 个团队工具登记进 tools.HANDLERS


def build_system_prompt() -> str:
    """每轮请求现场构建：compact 会改写记忆文件，prompt 必须读到最新记忆。
    Kimi 按前缀自动缓存，所以整个 prompt 要逐字节稳定：易变的时间戳放用户消息里，
    偶尔会变的记忆放最后（变了也不影响前面部分的缓存命中）"""
    return f"""
你是 Jimmy，智能助手，回答简洁有条理，用中文回复。
不确定的事直接向用户提问，禁止编造。
环境：Windows，run_command 在 cmd.exe 中执行。
网络：web_search 关键词搜索实时信息；web_fetch 抓取指定 URL 的网页全文。
可用 skill（仅名称和描述）：{loader.get_catalog()}，
任务与某个 skill 匹配时，先 load_skill 加载完整说明，再按说明执行。

【规矩】
1. 任务需要多个步骤才能完成时，请先建 todolist 再逐步推进，调用 update_todos 逐步完成清单。
2. 开始某一步前把它改为 in_progress（同一时间只许一项）；办完立即改为 completed，再开始下一项。
3. 简单的一句话问答不必生成 todolist，直接回答。
4. 生成文件（代码/HTML/Markdown 等）一律用 write_file 落盘，内容必须缩进规范、逐行排版，禁止把整份文件压成一行。
5. 遇到细节繁多但与主线对话无关的差事（如抓多个网页、批量跑命令、探索性调查），
   应调用 dispatch_subagent 派子代理去办，主上下文只听汇报，保持干净。
   若子代理明确汇报"未给出有效汇报"，不要照搬给用户，应自行补查或改派子代理重试一次。
6. 若多件差事互不依赖，可在同一次回复中同时派遣多个子代理，并发执行节省时间。
   身份选择：searcher 搜索员（只读查资料）；writer 写手（撰文稿）；coder 程序员（写代码）。
   优先选权限最窄、职司最贴合的身份。
7. todolist 和子代理是配合关系而非二选一：多步骤任务先建 todolist；
   执行到某一步，若该步细节繁多（抓多个网页、批量跑命令、探索性调查），
   就为这一步派遣子代理，子代理汇报后继续推进 todolist 的下一步。
8. 区分两种调度：dispatch_subagent 是临时派差（办完即散，只回传摘要）；
   spawn_teammate 是固定班底（有名字、角色、状态和 inbox，可持续反复协作）。
   一次性短期差事派子代理；长期项目、需要固定角色反复配合、需要角色间互相沟通的，组建 team。
9. 队友是异步干活的：spawn 或 send_message 后不会立刻有结果，
   稍后调用 read_inbox 查看队友回禀，再决定下一步。

【长期记忆】
{memory.read_memory()}

【用户画像】
{memory.read_user()}

【今日情景】
{memory.read_today_episode() or "(暂无)"}
"""


# 上下文记忆（Anthropic 协议里 system 是独立参数，不放 messages 里）
history = []


while(True):
    try:
        user_input = input("[User]:")
    except (EOFError, KeyboardInterrupt):
        user_input = "/exit"  # Ctrl+C / Ctrl+D 也走退出流程，顺手整理记忆
    if user_input.strip() == "/team":    # 查看队友名册和状态（给人看的）
        print(team.TEAM.list_all())
        continue
    if user_input.strip() == "/inbox":   # 查看队友给 lead 的回禀（给人看的）
        print(json.dumps(team.BUS.read_inbox("lead"), ensure_ascii=False, indent=2))
        continue
    if user_input.strip() == "/exit":    # 退出前强制 compact：每个会话都留下记忆，不靠长度碰运气
        print("[退出前整理记忆...]")
        compact_history(history, client, force=True)
        break
    # 时间戳放用户消息里：留在 system 里会让自动前缀缓存每秒失效
    user_message = {"role": "user",
                    "content": f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {user_input}"}
    history.append(user_message)
    memory.append_history(user_message)  # 流水落盘：凡 history.append 必紧跟

    # Agent 内层循环：模型可能连续多轮调用工具，直到不再请求工具为止
    while True:
        message = client.messages.create(
            model="k3",  # 可选: kimi-for-coding, kimi-for-coding-highspeed, k3-256k, k3（按会员等级）
            system=build_system_prompt(),
            tools=tools.CLIENT_TOOLS + tools.SERVER_TOOLS + [subagent.DISPATCH_TOOL] + team.TEAM_TOOLS,
            messages=history,
            max_tokens=4096,
        )
        # Kimi 是自动前缀缓存（无需打标记）：read=命中量（折扣价），input=未命中量（原价）
        u = message.usage
        print(f"[tokens] input={u.input_tokens} "
              f"read={getattr(u, 'cache_read_input_tokens', 0) or 0}")
        
        # 原样回传 assistant 的完整内容（含 thinking / text / tool_use 块）
        assistant_message = {"role": "assistant", "content": message.content}
        history.append(assistant_message)
        memory.append_history(assistant_message)

        if message.stop_reason != "tool_use":
            if todo_list.todos:
                if todo_list.unfinished():
                    print(todo_list.render())
                    reminder = {
                        "role": "user",
                        "content": f"任务尚未完成，以下事项仍未完成，请按计划继续执行，"
                        "并按规矩更新 todolist 状态：\n" + todo_list.render()
                    }
                    history.append(reminder)
                    memory.append_history(reminder)
                    continue  # 继续内层循环，模型会再次生成工具调用或文本回复
                print("[计划全部完成]\n" + todo_list.render())
                todo_list.reset()
            break  # 内层循环结束，模型不再请求工具，回到外层

        # 执行模型请求的工具，结果以 user 消息回传（按名字分发，新增工具无需改这里）
        # 把tools分为两类，一类是普通工具，一类是subagent（多个侧并发）
        tool_blocks = [b for b in message.content if b.type == "tool_use"]
        subagent_blocks = [b for b in tool_blocks if b.name == "dispatch_subagent"]
        other_blocks = [b for b in tool_blocks if b.name != "dispatch_subagent"]

        results_map = {}
        for block in other_blocks:
            print(f"[Tool]: {block.name}({block.input})")
            results_map[block.id] = tools.dispatch_tool(block.name, block.input)

        if len(subagent_blocks) > 1:
            print(f"[SubAgent]: 并发派遣({len(subagent_blocks)} subagents)")
            def _run_one(block):
                return block.id, subagent.run_subagent(
                    client, task=block.input["task"],
                    agent_type=block.input.get("agent_type"),
                    purpose=block.input.get("purpose", "")
                )
            with ThreadPoolExecutor(max_workers=len(subagent_blocks)) as pool:
                for block_id, summary in pool.map(_run_one, subagent_blocks):
                    results_map[block_id] = summary
        else:
            for block in subagent_blocks:
                print(f"[SubAgent]: 派遣({block.input})")
                results_map[block.id] = subagent.run_subagent(
                    client, task=block.input["task"],
                    agent_type=block.input.get("agent_type"),
                    purpose=block.input.get("purpose", "")
                )
        for block in message.content:
            if block.type == "server_tool_use":
                print(f"[Tool]: {block.name}({block.input})")

        tool_results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": results_map[b.id]}
            for b in tool_blocks
        ]
        tool_message = {"role": "user", "content": tool_results}
        history.append(tool_message)
        memory.append_history(tool_message)

    # 打印最终文本回复
    for block in message.content:
        if block.type == "text":
            print(f"[Agent]: {block.text}\n")

    # 一轮完整对话结束：旧对话超阈值时压缩成记忆文件（history 要接住新列表）
    history = compact_history(history, client)
