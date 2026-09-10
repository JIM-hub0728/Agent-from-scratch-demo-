VALID_STATUS = {"pending", "in_progress", "completed"}
STATUS_ICON = {"pending": "[ ]", "in_progress": "[~]", "completed": "[✔ ]"}

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

        #2，校验：同一个时间只能有一个in_progress，违规则让模型改正
        in_progress = [t for t in cleaned if t["status"]=="in_progress"]
        if len(in_progress) > 1:
            return f"Error：同一时间只能执行一个任务，请重新规划。当前待办项：{self.render()}"

        #3.终端可视化
        self.todos = cleaned
        print(self.render())

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

todo_list = TodoList()  # 模块级单例：tools.py 和 agent.py 共用同一个实例