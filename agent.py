# import os
import atexit
import json
from anthropic import Anthropic, APIError
import tools
from skill_loader import loader
from memory import memory
from memory_compact import compact_history, extract_memory, sanitize_history
from datetime import datetime
from plan import todo_list
from undo import undo_stack
from concurrent.futures import ThreadPoolExecutor
import subagent
import team
import ui
import config
from events import emit

# API 配置全部收口在 config.py（两家供应商的 key / base_url 都在那里）
client = Anthropic(
    api_key=config.MAIN_API_KEY,
    base_url=config.MAIN_BASE_URL,
)

team.init(client)          # 注入 client：队友线程调模型要用
team.register_handlers()   # 把 5 个团队工具登记进 tools.HANDLERS

ui.start_session(config.MODEL)      # 底部常驻状态行（混合模式 TUI）
atexit.register(ui.stop_session)    # 崩溃/异常退出也要收掉状态行，还用户干净终端


def build_system_prompt() -> str:
    """每轮请求现场构建：compact 会改写记忆文件，prompt 必须读到最新记忆。
    Kimi 按前缀自动缓存，所以整个 prompt 要逐字节稳定：易变的时间戳放用户消息里，
    偶尔会变的记忆放最后（变了也不影响前面部分的缓存命中）"""
    return f"""
你是 Jimmy，智能助手，回答简洁有条理，用中文回复。
不确定的事直接向用户提问，禁止编造。
环境：Windows；跑代码、装依赖、跑测试优先用 code_sandbox（隔离沙箱）；系统级命令才用 run_command（cmd.exe，执行前会向用户请求确认）。
网络：web_search 关键词搜索实时信息；web_fetch 抓取指定 URL 的网页全文；download_file 下载图片/PDF 等二进制文件到本地。
记忆：search_memory 语义检索历史记忆（已搭 RAG），按意思或关键词搜索过去的每日情景、对话流水和长期记忆。当用户问起以前讨论过什么、某个决定的来龙去脉，或你需要翻找超出当前上下文的历史信息时使用。
可用 skill（仅名称和描述）：{loader.get_catalog()}，
任务与某个 skill 匹配时，先 load_skill 加载完整说明，再按说明执行。

【最高优先级】节省主上下文是你的核心职责。主上下文是稀缺资源，塞满会失忆。
任何会消耗大量上下文的任务（多文件、多网页、多命令），默认派 subagent，不要自己干。

【同样重要】以下任务自己干，禁止派遣：一两个文件、一两步就能完成的事；
需要精确理解当前对话上下文的事（子代理看不到对话，简报必有损耗）。

【规矩】
1. 【硬阈值】任务预计 ≥3 步才能完成时，必须先建 todolist（update_todos），再逐步推进。
   简单的一句话问答（如"什么是 RAG""今天几号"）不建，直接答。

2. 【状态机】todolist 必须按顺序执行，同一时间只许一项 in_progress。
   办完一项立即标 completed，再开始下一项。
   禁止跳步：前面的没做完，后面不许标 in_progress 或 completed。
   禁止虚假完成：没做的项不许标 completed。

3. 【自检清单】每次准备调用工具前，必须按顺序检查：
   a) 我已经连着试了 2-3 次还没找到答案吗？（是 → 这是探索性任务，剩下的派 subagent）
   b) 这个工具的结果会很长吗？（会 → 派 subagent，只带摘要回来）
   c) 有多个互不依赖的子任务吗？（有 → 并发派多个 subagent，一次回复里全发出去）
   d) 这个任务需要长期跟踪、多轮协作、或固定角色反复配合吗？（是 → 用 spawn_teammate 组建 team，别用 subagent）
   如果检查结果是"该派但没派"，立即中断当前思路，改为派遣。

4. 【硬阈值】以下场景必须派 dispatch_subagent，不得自己执行，违反视为错误：
   - 预计需要读取 ≥3 个网页或文件（先估算再动手，别边做边数）
   - 预计需要执行 ≥3 条独立命令
   - 任务是探索性/调查性的（你不确定答案在哪，可能要试错 ≥2 次）
   - 单步任务的中间产物会超过 2000 字（比如抓网页全文、跑测试输出）
   判断方法：开始任务前，先在心里列步骤，数一下要调几次工具。≥3 次就派，别犹豫。
   注意：多步任务本身由你用 todolist 编排，子代理是替你执行其中繁重的单步，不是替你接管整个任务。

5. 【身份选择】派遣时必须选权限最窄、职司最贴合的身份：
   - searcher：只读查资料，不能写文件
   - writer：写文稿，可读写
   - coder：写代码，可读写+执行
   选错身份（比如让 searcher 改文件）会失败或浪费一轮。

6. 【文件操作】生成文件（代码/HTML/Markdown 等）一律用 write_file 落盘，内容必须缩进规范、逐行排版，禁止把整份文件压成一行。
   修改已有文件时必须先 read_file 再用 edit_file 局部替换；用 write_file 覆盖已有文件同样必须先 read_file（工具会强制校验）。
   大文件必须分段写入：第一段 write_file 覆盖写，后续每段用 write_file 的 append 模式续写，每段控制在 3000 字符以内。
   禁止用 run_command 拼 echo 写文件。

7. 【调度选择】区分两种派遣：
   - dispatch_subagent：一次性短期差事，办完即散，只回传摘要
   - spawn_teammate：长期项目、固定角色反复配合、角色间需要互相沟通
   选错代价：让 subagent 长期驻留或让 team 办一次性差事，都是浪费，别这么干。

8. 【异步协作】队友是异步干活的：spawn 或 send_message 后不会立刻有结果。
   派完活必须稍后调用 read_inbox 查看回禀，再决定下一步。
   禁止连续追问"好了吗"：等不到回禀就去做别的事，或过一会再查。

9. 【搜索纪律】只在确实需要实时信息时才调用 web_search，且必须给出明确 query。
   打招呼、闲聊、询问记忆/偏好类问题，答案就在上下文里，禁止搜索。
   单次回复最多搜 2 次（max_uses 限制），别浪费。

10. 【失败处理】子代理汇报失败时，不要照搬给用户；可自行补查或改派重试一次，仍失败就向用户说明现状。
    自行补查同样受规矩 4 的 ≥3 网页/文件限制，别把抓取刷屏到主上下文。
    连续 2 次失败必须更换策略，禁止重试同一地址；禁止编造 URL；禁止抓搜索引擎结果页（噪音）。

11. 【记忆检索】用户问起过去的讨论、决定、偏好细节，或需要翻找超出当前上下文的历史信息时，
    先用 search_memory 语义检索历史记忆；检索结果为空就明说没找到，禁止凭印象编造。

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
        user_input = ui.user_input()
    except (EOFError, KeyboardInterrupt):
        user_input = "/exit"  # Ctrl+C / Ctrl+D 也走退出流程，顺手整理记忆
    if user_input.strip() == "/team":    # 查看队友名册和状态（给人看的）
        emit("info", text=team.TEAM.list_all())
        continue
    if user_input.strip() == "/inbox":   # 查看队友给 lead 的回禀（给人看的）
        emit("info", text=json.dumps(team.BUS.read_inbox("lead"), ensure_ascii=False, indent=2))
        continue
    if user_input.strip() == "/undo":    # 撤销上一轮的文件改动（弹撤销栈）
        result = undo_stack.undo()
        emit("info", text=result)
        # 同步告知模型：否则它还以为文件保持着自己刚写完的样子，会接着幻觉干活
        note = {"role": "user", "content": f"(系统：用户执行了撤销。{result})"}
        history.append(note)
        memory.append_history(note)
        continue
    if user_input.strip() == "/exit":    # 退出前统一提取记忆：长期记忆只在会话结束时写，避免中途自我强化
        emit("info", text="[退出前整理记忆...]")
        extract_memory(history)
        ui.stop_session()
        break
    # 时间戳放用户消息里：留在 system 里会让自动前缀缓存每秒失效
    user_message = {"role": "user",
                    "content": f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {user_input}"}
    history.append(user_message)
    memory.append_history(user_message)  # 流水落盘：凡 history.append 必紧跟
    undo_stack.begin_turn()  # 开一轮新的改动快照：本轮所有写文件都会先备份原文

    # Agent 内层循环：模型可能连续多轮调用工具，直到不再请求工具为止
    truncations = 0  # 本回合内已连续被 max_tokens 截断的次数（防续写死循环）
    while True:
        history = sanitize_history(history)  # 修补孤儿 tool_use：缺结果的调用补"被中断"，防 400 锁死会话
        # 循环只发射事件：正文增量/用量/活动状态。前缀、换行、状态行熄灭等渲染规则由 UI 订阅者决定
        emit("activity", text="等待模型响应")
        try:
            with client.messages.stream(
                model=config.MODEL,
                system=build_system_prompt(),
                tools=tools.CLIENT_TOOLS + tools.SERVER_TOOLS + [subagent.DISPATCH_TOOL] + team.TEAM_TOOLS,
                messages=history,
                max_tokens=config.MAX_TOKENS,
            ) as stream:
                for text in stream.text_stream:  # 只吐正文增量，自动跳过 thinking
                    emit("text", delta=text)
                message = stream.get_final_message()
        except APIError as exc:
            emit("error", source="API", detail=f"{exc}；本轮未回复，请重新输入")
            break  # 跳出内层循环回到用户输入：一次 API 失败不再杀死整个进程
        # token 用量：read=前缀缓存命中量（折扣价），input=未命中量（原价）
        u = message.usage
        emit("usage", input=u.input_tokens,
             cache_read=getattr(u, 'cache_read_input_tokens', 0) or 0)
        
        # 原样回传 assistant 的完整内容（含 thinking / text / tool_use 块）
        assistant_message = {"role": "assistant", "content": message.content}
        history.append(assistant_message)
        memory.append_history(assistant_message)

        # max_tokens 截断：自动续写而不是中断收场。被截断的工具调用没执行过，
        # sanitizer 会在下轮请求前把它标记为"被中断"；模型配合分段规则重写/续写
        if message.stop_reason == "max_tokens" and truncations < config.MAX_CONTINUATIONS:
            truncations += 1
            emit("info", text=f"[输出达到长度上限，自动续写（{truncations}/{config.MAX_CONTINUATIONS}）]")
            continuation = {"role": "user", "content":
                "(系统：你的上一条输出因长度上限被截断，其中未完成的工具调用未被执行。"
                "请从中断处继续，不要重复已输出的内容；大文件必须分段写入："
                "第一段用 write_file 覆盖写，后续每段用 write_file 的 append 模式续写。)"}
            history.append(continuation)
            memory.append_history(continuation)
            continue
        if message.stop_reason == "max_tokens":
            emit("info", text=f"[已连续截断 {config.MAX_CONTINUATIONS} 次，停止自动续写；"
                              f"可尝试调高 config.MAX_TOKENS 或把任务拆小]")

        if message.stop_reason != "tool_use":
            if todo_list.todos:
                if todo_list.unfinished():
                    emit("todo", items=todo_list.todos)
                    reminder = {
                        "role": "user",
                        "content": f"任务尚未完成，以下事项仍未完成，请按计划继续执行，"
                        "并按规矩更新 todolist 状态：\n" + todo_list.render()
                    }
                    history.append(reminder)
                    memory.append_history(reminder)
                    continue  # 继续内层循环，模型会再次生成工具调用或文本回复
                emit("info", text="[计划全部完成]")
                todo_list.reset()
            break  # 内层循环结束，模型不再请求工具，回到外层

        # 执行模型请求的工具，结果以 user 消息回传（按名字分发，新增工具无需改这里）
        # 把tools分为两类，一类是普通工具，一类是subagent（多个侧并发）
        tool_blocks = [b for b in message.content if b.type == "tool_use"]
        subagent_blocks = [b for b in tool_blocks if b.name == "dispatch_subagent"]
        other_blocks = [b for b in tool_blocks if b.name != "dispatch_subagent"]

        results_map = {}
        streak = 0

        def _exec_one(block):
            return block.id, block.name, tools.dispatch_tool(block.name, block.input)

        # 按能力声明分批调度：连续的并发安全工具合批并行，其余串行。
        # 以前只有 dispatch_subagent 能并发（按名字特判）；现在调度看声明不看名字
        for kind, batch in tools.iter_tool_batches(other_blocks):
            group = batch if kind == "parallel" else [batch]
            for b in group:
                emit("tool_start", name=b.name, input=b.input)
            if kind == "parallel" and len(group) > 1:
                with ThreadPoolExecutor(max_workers=len(group)) as pool:
                    for bid, name, result in pool.map(_exec_one, group):
                        results_map[bid] = result
                        emit("tool_end", name=name, result=result)
                        streak = tools.record_tool_result(result)
            else:
                for b in group:
                    _, name, result = _exec_one(b)
                    results_map[b.id] = result
                    emit("tool_end", name=name, result=result)
                    streak = tools.record_tool_result(result)  # 失败记账：熔断要用

        if len(subagent_blocks) > 1:
            for block in subagent_blocks:
                emit("tool_start", name="dispatch_subagent", input=block.input)
            def _run_one(block):
                return block.id, subagent.run_subagent(
                    client, task=block.input["task"],
                    agent_type=block.input.get("agent_type"),
                    purpose=block.input.get("purpose", "")
                )
            with ThreadPoolExecutor(max_workers=len(subagent_blocks)) as pool:
                for block_id, summary in pool.map(_run_one, subagent_blocks):
                    results_map[block_id] = summary
                    emit("tool_end", name="dispatch_subagent", result=summary)
        else:
            for block in subagent_blocks:
                emit("tool_start", name="dispatch_subagent", input=block.input)
                summary = subagent.run_subagent(
                    client, task=block.input["task"],
                    agent_type=block.input.get("agent_type"),
                    purpose=block.input.get("purpose", "")
                )
                results_map[block.id] = summary
                emit("tool_end", name="dispatch_subagent", result=summary)
        for block in message.content:
            if block.type == "server_tool_use":
                emit("info", text=f"[Tool(server)]: {block.name}")

        # tool_result 构造：view_image 的图片载荷要转成 image 内容块，模型才"看得见"
        tool_results = [
            {"type": "tool_result", "tool_use_id": b.id,
             "content": tools.to_tool_result_content(results_map[b.id])}
            for b in tool_blocks
        ]
        tool_message = {"role": "user", "content": tool_results}
        history.append(tool_message)
        memory.append_history(tool_message)

        # 失败熔断：连续 ≥3 次工具失败，强制模型停下当前策略。
        # 没有这道闸，fallback 会退化成"同一地址死磕几十次"的刷屏式自杀
        if streak >= 3:
            circuit = {"role": "user", "content":
                f"(系统熔断提醒：你已连续 {streak} 次工具调用失败。立即停止当前策略："
                f"禁止重试已失败的地址或命令，禁止编造 URL；"
                f"改用其他方法，或向用户说明现状并请示。)"}
            history.append(circuit)
            memory.append_history(circuit)
            emit("info", text=f"[熔断] 连续 {streak} 次失败，已强制要求模型改变策略")
            tools.reset_failure_streak()  # 熔断已介入，清零给模型一次改过机会

    # 最终文本已在流式输出时实时打印，这里无需再打印

    # 一轮完整对话结束：旧对话超阈值时压缩进今日情景（长期记忆留到退出时统一提取）
    history = compact_history(history)
    undo_stack.end_turn()  # 本轮有文件改动则压入撤销栈（没改动不占栈位）
    emit("turn_end")  # 回合结束：分隔线/状态清理由 UI 订阅者决定
