"""子代理模块：
独立上下文 + 多身份 + 并发派遣。
子代理跑自己的 messages 小循环，办完只回传一段摘要给主 history，
中间过程不污染主上下文（这就是"主上下文压缩"）。
模型: k3（与主 agent相同，client 由主循环传入）
"""
from concurrent.futures import ThreadPoolExecutor
import tools
from events import emit
import config

def build_subagent_prompt(title: str, duty:str, boundary:str) -> str:
    """子代理身份：system prompt + 身份 + 边界"""
    return f"""
你是{title}，是主agent Jimmy派出的子代理，专办一件差事。
职责是：{duty}，边界是：{boundary}，
用工具高质量高速度完成任务，最后用一段简短中文向主agent汇报结果。
只汇报关键信息，不要复述每一步细节，
你不能再派遣其它子代理，所有的事情要自己跑工具完成。
"""

# 无效汇报的特征串：观测到的失败模式，以后发现新的垃圾形态就往这个列表里加
INVALID_PATTERNS = ["Search results for query:"]

SALVAGE_MIN_CHARS = 60  # 剥离噪音前缀后剩下的不足此长度 = 搜索结果碎片，不值得救


def is_valid_report(final: str) -> bool:
    """判断子代理的最终文本是不是一份合格汇报。
    三条启发式：非空、足够长、不含已知的搜索噪音回显。"""
    text = (final or "").strip()
    if len(text) < 30:  # 真正的调研汇报很少短于 30 字；空串也在这一关被拦下
        return False
    if any(p in text for p in INVALID_PATTERNS):
        return False
    return True


def _salvage_report(text: str) -> str:
    """抢救式剥离：汇报以噪音格式开头时剥掉前缀看剩下的——可能是真内容蒙冤。
    剩下的是短碎片（搜索结果回显）就不救了，返回空串"""
    text = (text or "").strip()
    for pattern in INVALID_PATTERNS:
        if text.startswith(pattern):
            text = text[len(pattern):].strip()
    return text if len(text) >= SALVAGE_MIN_CHARS else ""


def _diagnose(final: str) -> str:
    """失败类别诊断：回传给主 agent，作为它调整策略的依据"""
    text = (final or "").strip()
    if not text:
        return "空汇报（模型未输出任何文字，可能服务异常）"
    if any(p in text for p in INVALID_PATTERNS):
        return "汇报复述了搜索结果的噪音格式，未提炼出有效信息"
    return f"汇报过短（仅 {len(text)} 字，信息量不足）"


def _count_tool_uses(messages: list) -> int:
    """子代理一共发起过多少次工具调用（诊断用：让主 agent 知道它试过多少）"""
    n = 0
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            n += sum(1 for b in m["content"] if getattr(b, "type", None) == "tool_use")
    return n


def _best_effort(final: str) -> str:
    """尽力而为：原文合格用原文；不合格先抢救剥离再校验。返回空串=都救不了"""
    if is_valid_report(final):
        return final.strip()
    return _salvage_report(final)

SUBAGENT = {
    "searcher":{
        "title": "搜索员",
        "system_prompt": build_subagent_prompt(
            "搜索员",
            "查资料、抓网页、读信息、归纳结论",
            "只读不写；不得修改或编纂任何文件，只把关键发现或结论汇报回去。"
        ),
        "tools": ["load_skill", "web_search", "web_fetch", "read_file", "list_dir"],
        "max_turns": 10,
    },
    "writer":{
        "title": "写手",
        "system_prompt": build_subagent_prompt(
            "写手",
            "根据主agent提供的资料和要求，撰写一份高质量的文稿",
            "可读写可执行；动手前先了解现状汇报时列出改了什么和验证结果。"
        ),
        "tools": ["run_command", "load_skill", "read_file", "write_file", "edit_file", "list_dir", "code_sandbox", "download_file"],
        "max_turns": 15,
    },
    "coder":{
        "title": "程序员",
        "system_prompt": build_subagent_prompt(
            "程序员",
            "根据主agent提供的需求和资料，编写一份高质量的代码",
            "可读写可执行；动手前先了解现状汇报时列出改了什么和验证结果。"
        ),
        "tools": ["run_command", "load_skill", "read_file", "write_file", "edit_file", "list_dir", "code_sandbox", "download_file"],
        "max_turns": 15,
    },
}

def run_subagent(client, task:str, agent_type:str = "coder", purpose:str = "") -> str:
    """启动一个独立 message loop 的子代理，跑完只把最终文本回传给主 agent
      client 由主循环传入：子代理模块自己不持有 API 配置，保持无状态。"""
    spec = SUBAGENT.get(agent_type) or SUBAGENT["coder"]  # 模型传错身份名时兜底
    sub_tools = [t for t in tools.CLIENT_TOOLS + tools.SERVER_TOOLS if t["name"] in spec["tools"]]
    label = purpose or task[:50]  # 用 purpose 做 label，没提供就截 task 前 50字
    emit("info", text=f"\n[派遣子代理{spec['title']}]:{label}")

    messages = [{"role": "user", "content": task}]
    for turn in range(spec["max_turns"]):
        msg = client.messages.create(
            model=config.MODEL,
            system=spec["system_prompt"],
            tools=sub_tools,
            messages=messages,
            max_tokens=4096,  # k3 的思考也烧 token：1024 会被思考烧光，导致响应里只有 thinking 块没有正文
        )
        messages.append({"role":"assistant", "content":msg.content})

        if msg.stop_reason == "tool_use":
            results = []
            for b in msg.content:
                if b.type == "tool_use":
                    emit("info", text=f"  [SubagentTool]: {b.name}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": tools.dispatch_tool(b.name, b.input),
                    })
            messages.append({"role":"user", "content": results})
            continue

        if msg.stop_reason == "max_tokens":
            # 输出被截断（多半是思考烧光额度）：不当成完成，催它跳过思考直接给结论，再试一轮
            emit("info", text=f"  [Subagent]: 第 {turn + 1} 轮被 max_tokens 截断，催其直接汇报")
            messages.append({"role": "user", "content":
                "你上一轮的输出达到长度上限被截断了。请停止展开思考，"
                "直接用一段中文给出目前已有的关键结论。"})
            continue

        # 其余 stop_reason（end_turn 等）：取最终文本，尽力抢救
        final = next((b.text for b in msg.content if b.type == "text"), "")
        candidate = _best_effort(final)
        if candidate:
            emit("info", text=f"[子代理汇报]: {candidate}\n")
            return candidate

        # 救不了 → 自我纠正一轮：格式病是小病，让它用自己的话重说一遍，多半能自愈
        emit("info", text="  [Subagent]: 汇报不合格，令其用自己的话重述")
        messages.append({"role": "user", "content":
            "你的汇报不合格（太空洞，或复述了搜索结果的原始格式）。"
            "请用自己的话重新归纳本次工作的关键结论，分点列出，不少于 100 字。"
            "不要调用任何工具，直接给文字总结。"})
        retry_msg = client.messages.create(
            model=config.MODEL, system=spec["system_prompt"],
            tools=sub_tools, messages=messages, max_tokens=4096)
        retry_final = next((b.text for b in retry_msg.content if b.type == "text"), "")
        candidate = _best_effort(retry_final)
        if candidate:
            emit("info", text=f"[子代理汇报]: {candidate}\n")
            return candidate

        # 真失败了：结构化诊断回传主 agent（让它知道怎么改），终端只留一行
        reason = _diagnose(retry_final or final)
        emit("info", text=f"[子代理失败]: {reason}")
        return (f"（子代理任务失败。类别：{reason}；"
                f"子代理已尝试 {_count_tool_uses(messages)} 次工具调用；"
                f"其最终汇报前 80 字：{(retry_final or final)[:80]!r}。"
                f"建议：换关键词或换数据源重试、缩小任务范围、改派其他身份的子代理，"
                f"或干脆主 agent 自行补查。）")
    return "子代理未完成任务"

DISPATCH_TOOL =  {
    "name": "dispatch_subagent",
    "description":(
        "派遣子代理去办事。适用于：抓取并阅读多个网页、批量执行命令，写文档，写代码等、"
        "细节繁多但与主线无关的探索性任务。"
        "子代理有独立上下文，办完只回传一段文字总结，不污染主上下文。"
        "若有多件事互不依赖，可在同一次回复中发出多个dispatch_subagent并发执行。"
        "要在task中写清要做什么，希望返回什么格式的总结。"
    ),
    "input_schema":{
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "子代理要办的事，写清楚要做什么、希望返回什么格式的总结。"
                ),
            },
            "agent_type": {
                "type": "string",
                "enum": list(SUBAGENT.keys()),
                "description": (
                    "子代理类型：searcher=搜索员，抓取网页、读信息、归纳结论；"
                    "writer=写手，根据资料和要求撰写文稿；"
                    "coder=程序员，根据需求和资料编写代码。"
                ),
            },
            "purpose": {
                "type": "string",
                "description": (
                    "一句话用途标签（可选），仅用于终端打印，不影响子代理行为。"
                ),
            },
        },
        "required": ["task", "agent_type"],
    }
}