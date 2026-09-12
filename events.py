"""事件总线：Agent 循环向外发射事件的唯一通道。

循环（agent.py / plan.py / memory_compact.py / subagent.py）只调用 emit()；
订阅者（终端 UI，或未来的日志器、TUI）各自消费，互不知道对方存在。
这让 Agent 循环可以独立演进：新增消费者不需要改循环，改循环不需要管渲染。

事件类型是稳定契约：
  text       正文增量          {delta}
  tool_start 工具开始          {name, input}
  tool_end   工具结束          {name, result}
  error      错误（一等公民）   {source, detail}
  info       辅助信息          {text}
  usage      token 用量        {input, cache_read}
  todo       计划单变化        {items}
  activity   当前活动          {text}（空串=空闲）
  turn_end   一轮对话结束      {}
"""
import threading
from dataclasses import dataclass, field


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)


_subscribers = []
_emit_lock = threading.RLock()  # 子代理/队友线程也会发射：渲染必须串行，防输出打架


def subscribe(fn):
    """注册一个消费者：fn(Event) -> None"""
    _subscribers.append(fn)


def emit(type: str, **payload):
    """发射一个事件。循环对外说话只许用这种方式"""
    event = Event(type, payload)
    with _emit_lock:
        for fn in _subscribers:
            fn(event)
