import json
import re
from datetime import datetime
from pathlib import Path

from memory import memory, json_safe

RECENT_MESSAGES = 10         # 压缩后保留最近多少条
COMPACT_AFTER_MESSAGES = 30  # history 超过多少条触发压缩
COMPACT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "compact_prompt.md"
COMPACT_MODEL = "k3"         # 整理员用的模型


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


def compact_history(history: list, client, force: bool = False) -> list:
    """history 超阈值时交给记忆整理员压缩，只保留最近一段；任何失败都返回原 history。
    force=True 时无视阈值，用于退出前的收尾整理（保证每个会话都留下记忆）。"""
    if not force and len(history) <= COMPACT_AFTER_MESSAGES:
        return history

    # 切点不能落在工具结果上：tool_result 必须和成对的 tool_use 同去同留，
    # 否则保留段以孤儿 tool_result 开头，下一轮请求会被 API 拒绝（400 tool_call_id is not found）
    start = len(history) - RECENT_MESSAGES
    while start > 0 and _is_tool_result_message(history[start]):
        start -= 1
    old_messages = history[:start]
    recent_messages = history[start:]
    if not old_messages:
        if force and history:
            # 退出收尾：会话太短切不出旧段时，整段交给整理员（反正要退出，无需保留）
            old_messages, recent_messages = history, []
        else:
            return history

    prompt = COMPACT_TEMPLATE_PATH.read_text(encoding="utf-8").format(
        old_conversation=_messages_to_text(old_messages),
        current_memory=memory.read_memory(),
        current_user=memory.read_user(),
        today_episode=memory.read_today_episode(),
        now_hhmm=datetime.now().strftime("%H:%M"),
    )

    try:
        message = client.messages.create(
            model=COMPACT_MODEL,
            max_tokens=8000,  # 3000 装不下：全量重写 MEMORY.md + episode + k3 的思考开销
            thinking={"type": "disabled"},  # compact 是格式化任务，关掉深度思考：输出 token 省约 8 成，还杜绝思考烧穿额度
            system="你是记忆整理员。请严格按要求输出 XML，不要输出额外解释。",
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in message.content if b.type == "text"), "")
    except Exception as exc:
        print(f"[记忆压缩失败，保留完整 history]: {exc}")
        return history

    episode = _extract_tag(text, "episode")
    updated_memory = _extract_tag(text, "updated_memory")
    updated_user = _extract_tag(text, "updated_user")

    # 三个标签全抠不出来 = 输出被截断或格式跑偏。
    # 必须当失败处理并保留完整 history：否则会静默丢弃旧消息还一个字记忆都不留
    if not (episode or updated_memory or updated_user):
        print(f"[记忆压缩失败，保留完整 history]: 整理员输出未含有效标签"
              f"（stop_reason={message.stop_reason}）")
        return history

    if episode:
        memory.append_episode(episode)
    if updated_memory:
        memory.write_memory(updated_memory)
    if updated_user:
        memory.write_user(updated_user)

    print(f"[记忆已压缩]: 压缩 {len(old_messages)} 条，保留最近 {len(recent_messages)} 条")
    return recent_messages
