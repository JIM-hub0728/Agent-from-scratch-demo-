VALID_STATUS = {"pending", "in_progress", "completed"}
STATUS_ICON = {"pending": "[ ]", "in_progress": "[~]", "completed": "[√]"}  # 用 √ 不用 ✔：✔(U+2714) 在 GBK 控制台 print 会直接编码崩溃
from events import emit

class TodoList:
    """计划单：状态唯一。模型只能通过update工具修改它"""
    def __init__(self):
        self.todos = []

    def render(self) -> str:
        """渲染为文本，供模型阅读"""
        if not self.todos:
            return "(暂无计划)"
        done = sum(1 for t in self.todos if t["status"]=="completed")
        lines = [f"进度：{done}/{len(self.todos)} 已完成"]
        for t in self.todos:
            icon = STATUS_ICON.get(t.get("status","pending"), "[?]")
            lines.append(f"{icon} {t.get('id')}: {t.get('content','')}")
        return "\n".join(lines) + "\n"

    def update(self, todos:list) -> str:
        """工具核心：清洗 → 校验 → 落账 → 返回带清单的回执"""
        #1.清洗：去空项、非法状态回退 pending
        cleaned = []
        for i, t in enumerate(todos, start=1):
            content = (t.get("content") or "").strip()
            if not content:
                continue
            status = t.get("status", "pending")
            if status not in VALID_STATUS:
                status = "pending"
            cleaned.append({"id":t.get("id",i), "content":content,"status":status})

        #2.校验：违规则拒绝落账，让模型改正后重发
        # 2a.同一个时间只能有一个in_progress
        in_progress = [t for t in cleaned if t["status"]=="in_progress"]
        if len(in_progress) > 1:
            return f"Error：同一时间只能执行一个任务，请重新规划。当前待办项：{self.render()}"

        # 2b.状态机约束，堵住两种抢跑：
        #   - 新完成的项必须从上次的 in_progress 流转而来：先做完，后打勾，不许没做就打勾
        #     （上次已 completed 的项保持 completed 是合法的：每次全量覆盖都会携带旧账）
        #   - in_progress/completed 要求排在前面（列表顺序）的项全部 completed：按顺序一个一个做
        prev_status = {t["id"]: t["status"] for t in self.todos}
        all_prev_done = True
        for t in cleaned:
            if t["status"] in ("in_progress", "completed") and not all_prev_done:
                return (f"Error：任务 {t['id']} 前面还有未完成项，必须按顺序逐个执行。"
                        f"当前待办项：{self.render()}")
            if (t["status"] == "completed"
                    and prev_status.get(t["id"]) not in ("in_progress", "completed")):
                return (f"Error：任务 {t['id']} 还未执行（需先置为 in_progress 并完成），"
                        f"不能标记为 completed。当前待办项：{self.render()}")
            if t["status"] != "completed":
                all_prev_done = False

        #3.对外广播：计划单变了（谁想显示谁订阅，plan 不关心渲染）
        self.todos = cleaned
        emit("todo", items=self.todos)

        #4.返回当前清单，以tool_result喂给模型
        pending = [t for t in self.todos if t["status"]=="pending"]
        done = [t for t in self.todos if t["status"]=="completed"]
        summary = f"todos updated: total={len(self.todos)}, completed={len(done)}, in_progress={len(in_progress)}, pending={len(pending)}"
        return summary + "\n当前列表: \n" + self.render()

    def unfinished(self) -> list:
        """未完成的要收尾验证"""
        return [t for t in self.todos if t["status"] != "completed"]

    def reset(self):
        """清空所有计划"""
        self.todos = []
        emit("todo", items=[])

todo_list = TodoList()  # 模块级单例：tools.py 和 agent.py 共用同一个实例