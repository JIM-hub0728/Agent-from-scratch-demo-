import json
import re
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic

from memory import memory, json_safe
from events import emit
import config

RECENT_MESSAGES = 10         # 压缩后保留最近多少条
COMPACT_AFTER_MESSAGES = 30  # history 超过多少条触发压缩
COMPACT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "compact_prompt.md"
EXTRACT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "extract_prompt.md"
MIN_USER_CHARS = 30          # 退出门控：用户实际输入累计低于此值且全程没用过工具 → 无实质内容，跳过提取

# 整理员用独立的 DeepSeek client：压缩/提取是格式化任务，不配占用主模型的额度和缓存
_curator_client = Anthropic(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)

# agent.py 给真实用户输入加了 "[YYYY-MM-DD HH:MM:SS] " 前缀，据此把用户原话
# 和程序注入的 user 消息（工具结果、todolist 提醒）区分开
_USER_INPUT_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")


def _messages_to_text(messages: list) -> str:
    """把消息列表转成纯文本，每行一条：role: [JSON内容]"""
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        content = json.dumps(json_safe(msg.get("content")), ensure_ascii=False)
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _extract_tag(text: str, tag: str) -> str:
    """从整理员输出里抠出 <tag>...</tag> 的内容，抠不到返回空串"""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _is_tool_result_message(msg: dict) -> bool:
    """判断一条消息是否是工具结果（user 角色 + content 含 tool_result 块）"""
    content = msg.get("content")
    return (
        msg.get("role") == "user"
        and isinstance(content, list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _user_input_text(msg: dict) -> str:
    """真实用户输入的原文（去掉时间戳前缀）；非用户原话的消息返回空串"""
    if msg.get("role") != "user" or not isinstance(msg.get("content"), str):
        return ""
    match = _USER_INPUT_RE.match(msg["content"])
    return msg["content"][match.end():] if match else ""


def _user_inputs_text(history: list) -> str:
    """拼接本次会话所有用户原话，每行一条：用户档案只许以这些话为依据，
    assistant 的自我描述不能混进用户档案（主客体分离，在代码层把证据源分开）"""
    lines = []
    for msg in history:
        text = _user_input_text(msg)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _used_tools(history: list) -> bool:
    """本次会话是否调用过工具（assistant 消息里含 tool_use 块）"""
    for msg in history:
        content = msg.get("content")
        if msg.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                if block_type == "tool_use":
                    return True
    return False


def _has_new_info(history: list) -> bool:
    """轻量提取门控：大多数琐碎会话（打招呼、一句话问答）不值得跑提取，直接跳过"""
    if _used_tools(history):
        return True
    return sum(len(_user_input_text(msg)) for msg in history) >= MIN_USER_CHARS


def _call_curator(prompt: str, max_tokens: int):
    """调记忆整理员（独立 DeepSeek client），返回响应对象；API 失败打印原因并返回 None。
    compact 是格式化任务，关掉深度思考：输出 token 省约 8 成，还杜绝思考烧穿额度"""
    try:
        return _curator_client.messages.create(
            model=config.CURATOR_MODEL,
            max_tokens=max_tokens,
            thinking={"type": "disabled"},
            system="你是记忆整理员。请严格按要求输出 XML，不要输出额外解释。",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        emit("info", text=f"[记忆整理调用失败]: {exc}")
        return None


def _response_text(message) -> str:
    return next((b.text for b in message.content if b.type == "text"), "")


def _block_type(block):
    """块类型：history 里 SDK 对象（属性）和 dict（键）混存，两种取法都支持"""
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def _block_id(block):
    return block.get("id") if isinstance(block, dict) else getattr(block, "id", None)


def sanitize_history(history: list) -> list:
    """修补 history 里的孤儿 tool_use：每个 tool_use 都必须有匹配的 tool_result，
    缺了补一条"被中断"的合成结果。孤儿多产自 max_tokens 截断（模型写工具调用
    写到一半被切）或中途打断；不修补的话 API 直接 400 tool_call_id is not found，
    且孤儿留在 history 里之后每轮都 400，整个会话报废"""
    healed = []
    total = len(history)
    for i, msg in enumerate(history):
        healed.append(msg)
        if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        tool_ids = [_block_id(b) for b in msg["content"] if _block_type(b) == "tool_use"]
        if not tool_ids:
            continue
        # 看下一条消息（user 角色）里已回了哪些 tool_result
        next_msg = history[i + 1] if i + 1 < total else None
        answered = set()
        if (next_msg and next_msg.get("role") == "user"
                and isinstance(next_msg.get("content"), list)):
            answered = {b.get("tool_use_id") for b in next_msg["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_result"}
        missing = [tid for tid in tool_ids if tid not in answered]
        if not missing:
            continue
        patch = [{"type": "tool_result", "tool_use_id": tid,
                  "content": "(该工具调用被中断或未执行，请换用其他方式或重新发起)"}
                 for tid in missing]
        if answered:
            next_msg["content"].extend(patch)   # 下一条是 tool_result 消息但缺了几个：补齐
        else:
            healed.append({"role": "user", "content": patch})  # 否则插一条合成消息
    return healed


def compact_history(history: list) -> list:
    """history 超阈值时交给记忆整理员压缩成今日情景记忆，只保留最近一段。
    中途压缩只负责上下文瘦身；MEMORY.md / USER.md 的更新统一推迟到会话结束
    （extract_memory），避免模型把自己刚生成的内容当历史记忆反复强化。
    任何失败都返回原 history。"""
    if len(history) <= COMPACT_AFTER_MESSAGES:
        return history

    # 切点不能落在工具结果上：tool_result 必须和成对的 tool_use 同去同留，
    # 否则保留段以孤儿 tool_result 开头，下一轮请求会被 API 拒绝（400 tool_call_id is not found）
    start = len(history) - RECENT_MESSAGES
    while start > 0 and _is_tool_result_message(history[start]):
        start -= 1
    old_messages = history[:start]
    recent_messages = history[start:]
    if not old_messages:
        return history

    prompt = COMPACT_TEMPLATE_PATH.read_text(encoding="utf-8").format(
        old_conversation=_messages_to_text(old_messages),
        today_episode=memory.read_today_episode(),
        now_hhmm=datetime.now().strftime("%H:%M"),
    )

    message = _call_curator(prompt, max_tokens=2000)  # 只产一段 episode，2000 足够
    if message is None:
        emit("info", text="[记忆压缩失败，保留完整 history]")
        return history

    episode = _extract_tag(_response_text(message), "episode")
    # 抠不到 <episode> = 输出被截断或格式跑偏。必须当失败处理并保留完整 history：
    # 否则会静默丢弃旧消息还一个字记忆都不留
    if not episode:
        emit("info", text=f"[记忆压缩失败，保留完整 history]: 整理员输出未含 <episode>"
              f"（stop_reason={message.stop_reason}）")
        return history

    memory.append_episode(episode)
    emit("info", text=f"[记忆已压缩]: 压缩 {len(old_messages)} 条，保留最近 {len(recent_messages)} 条")
    return recent_messages


def extract_memory(history: list):
    """会话结束时统一提取记忆：更新 MEMORY.md / USER.md，并给今日情景收尾。
    先用轻量门控判断有没有值得记住的新信息，琐碎会话直接跳过、不调 LLM。"""
    if not history:
        return
    if not _has_new_info(history):
        emit("info", text="[本次会话无实质新信息，跳过记忆提取]")
        return

    prompt = EXTRACT_TEMPLATE_PATH.read_text(encoding="utf-8").format(
        old_conversation=_messages_to_text(history),
        user_only_messages=_user_inputs_text(history),
        current_memory=memory.read_memory(),
        current_user=memory.read_user(),
        today_episode=memory.read_today_episode(),
        now_hhmm=datetime.now().strftime("%H:%M"),
    )

    message = _call_curator(prompt, max_tokens=8000)  # 3000 装不下：全量重写 MEMORY.md + episode
    if message is None:
        return

    text = _response_text(message)
    episode = _extract_tag(text, "episode")
    updated_memory = _extract_tag(text, "updated_memory")
    updated_user = _extract_tag(text, "updated_user")

    # 三个标签全抠不出来 = 输出被截断或格式跑偏，一个字都不写，避免污染记忆文件
    if not (episode or updated_memory or updated_user):
        emit("info", text=f"[记忆提取失败]: 整理员输出未含有效标签（stop_reason={message.stop_reason}）")
        return

    if episode:
        memory.append_episode(episode)
    if updated_memory:
        memory.write_memory(updated_memory)
    if updated_user:
        memory.write_user(updated_user)
    emit("info", text="[记忆提取完成]: 长期记忆 / 用户档案 / 今日情景已更新")
