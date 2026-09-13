# -*- coding: utf-8 -*-
"""撤销栈：按"轮"分组的文件改动快照，/undo 弹栈还原。

为什么用栈：撤销的语义是 LIFO——最后发生的改动最先撤销，栈顶永远是"最近一步"。
粒度是"轮"（一次用户输入到回复结束）：一轮可能连改多个文件，整组一起撤。

只覆盖经 tools.py 落盘的文件改动（write_file / edit_file / download_file）；
run_command 的副作用是黑盒（无法通用撤销），记忆系统的自动写入不在此列。
快照只活在内存里：进程退出后不可再撤（要持久化得把快照落盘，属于后续扩展）。
"""
from pathlib import Path

MAX_GROUPS = 50  # 栈深上限：每层是完整的文件原文副本，防内存无限涨


class UndoStack:
    def __init__(self):
        self._stack = []      # 栈本体：list 当栈用，append=压栈，pop=弹栈
        self._current = None  # 进行中的本轮快照组（begin_turn ~ end_turn 之间）

    def begin_turn(self):
        """一轮开始（agent.py 收到用户输入后调用）：开一个新的空快照组"""
        self._current = []

    def record(self, path):
        """文件被改动前调用（tools.py 的三个写工具里）：备份改动前的原文。
        同一轮同一文件只备份第一次——还原目标是"本轮开始前的状态"，
        而不是逐步回滚这一轮里的每一次写入"""
        if self._current is None:
            return                       # 不在回合内（如命令行直调）：不记账
        p = Path(path).resolve()         # resolve 统一相对/绝对路径，防同文件备份两份
        if any(f["path"] == p for f in self._current):
            return                       # 已备份过：最原始的原文已在手，跳过
        self._current.append({
            "path": p,
            "existed": p.exists(),
            # 存在就存原文（bytes 读法文本/二进制通吃）；
            # 不存在记 None——本轮新建的文件，撤销 = 删掉它
            "old_bytes": p.read_bytes() if p.exists() else None,
        })

    def end_turn(self):
        """一轮结束（agent.py 回合收尾时调用）：有改动的快照组压栈"""
        if self._current:                # 空列表是 falsy：本轮没改文件，不占栈位
            self._stack.append(self._current)
            if len(self._stack) > MAX_GROUPS:
                self._stack.pop(0)       # 超深：从栈底淘汰最老的一层
        self._current = None

    def undo(self) -> str:
        """弹出最近一轮快照组，把组内所有文件还原到那轮开始前的状态"""
        if not self._stack:
            return "(没有可撤销的改动)"
        group = self._stack.pop()
        restored, deleted = [], []
        for f in reversed(group):        # 逆序还原：和改动发生的顺序反过来
            p = f["path"]
            if f["existed"]:
                p.write_bytes(f["old_bytes"])    # 字节级回填，文本/二进制都无损
                restored.append(p.name)
            else:
                p.unlink(missing_ok=True)        # 本轮新建的文件：撤销 = 删除
                deleted.append(p.name)
        parts = []
        if restored:
            parts.append(f"已还原 {len(restored)} 个文件（{'、'.join(restored)}）")
        if deleted:
            parts.append(f"已删除 {len(deleted)} 个新建文件（{'、'.join(deleted)}）")
        return f"撤销完成：{'；'.join(parts)}。（栈里还有 {len(self._stack)} 轮可撤）"


# 模块级单例：和 memory / todo_list / loader 同一个模式，全程序共享一座栈
undo_stack = UndoStack()
