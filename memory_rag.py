# -*- coding: utf-8 -*-
"""记忆 RAG：给 memory/ 目录建向量索引，按语义检索历史记忆。

命令行用法：
  python memory_rag.py build            重建全部索引（记忆文件变了就跑一次）
  python memory_rag.py search 检索词     手动测试检索效果
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
# numpy 故意不在顶层 import：只有 build_index/search 用到它，
# 顶层 import 会让 agent 每次启动白交约 0.4 秒"启动税"（用到时才加载 = 零成本）

from config import RAG_API_KEY, RAG_API_URL, RAG_MODEL, RERANK_URL, RERANK_MODEL

# ---------- 路径配置 ----------
BASE_DIR = Path(__file__).parent        # 项目根目录（本文件所在处）
MEMORY_DIR = BASE_DIR / "memory"        # 现有的记忆目录
STORE_DIR = MEMORY_DIR / "rag_store"    # 索引存放处（memory/ 已被 .gitignore 覆盖）
VEC_FILE = STORE_DIR / "vectors.npy"    # 全部向量，一个 numpy 矩阵文件
CHUNK_FILE = STORE_DIR / "chunks.jsonl" # 每块的原文+元数据，一行一条

# ---------- 切块参数（以后调检索质量，主要就调这几个旋钮）----------
CHUNK_TARGET = 100   # 块的目标字符数：太小丢上下文，太大稀释语义
CHUNK_MAX = 600      # 单段超过此长度硬切（兜底）
CHUNK_MIN = 20       # 短于此长度的块不入库（"你好""谢谢"这类噪音）
SCORE_MIN = 0.5      # 检索相似度下限：低于此分视为"不相关"（经验旋钮，用真实查询校准后调）
COARSE_TOP_N = 30    # 粗筛圈多少个候选进精排：放宽别漏，收紧是精排的事


def embed(texts):
    """批量向量化：超过 32 段自动分批，防单次请求体过大导致超时"""
    if not texts:
        return []
    all_vectors = []
    for i in range(0, len(texts), 32):       # 步进 32，每次切出一批
        batch = texts[i:i + 32]
        payload = {"model": RAG_MODEL, "input": batch}
        resp = requests.post(RAG_API_URL,
                             headers={"Authorization": f"Bearer {RAG_API_KEY}"},
                             json=payload, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        if "data" not in result:
            raise RuntimeError(f"API 返回异常: {result}")
        # 按 index 排序，保证返回顺序和输入严格对齐
        ordered = sorted(result["data"], key=lambda item: item["index"])
        all_vectors.extend(item["embedding"] for item in ordered)  # 逐批累积
    return all_vectors


def rerank(query, docs):
    """交叉编码器精排：把 query 和每段候选文本拼在一起逐对阅读、打相关分。
    返回 [(候选在 docs 数组里的下标, 相关分)]，API 已按分数从高到低排好"""
    if not docs:
        return []
    payload = {"model": RERANK_MODEL, "query": query, "documents": docs,
               "top_n": len(docs)}        # 全部打分返回，最终切几片由调用方决定
    resp = requests.post(RERANK_URL,
                         headers={"Authorization": f"Bearer {RAG_API_KEY}"},
                         json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if "results" not in result:
        raise RuntimeError(f"rerank API 返回异常: {result}")
    # return_documents 默认 false：响应只带回 index 和分数、不回传原文（省 token），
    # index 对应我们传入 documents 数组的位置，靠它找回本地原文
    return [(item["index"], item["relevance_score"]) for item in result["results"]]


def chunk_text(text):
    """段落合并切块：先按空行拆成自然段，再把相邻小段合并到接近目标长度。
    比定长切尊重语义边界——不会从一句话中间劈开"""
    # \n\s*\n 匹配"空行"（允许空行里有空格），split 后得到自然段列表
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paragraphs:
        # 当前缓冲区再装上这段就超标 → 封存旧缓冲区，另起一块
        if buf and len(buf) + len(p) + 1 > CHUNK_TARGET:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{p}" if buf else p  # 段落并入缓冲区（首段直接放）
        # 某个段落自己就超长 → 硬切成若干截（兜底分支，很少触发）
        while len(buf) > CHUNK_MAX:
            chunks.append(buf[:CHUNK_MAX])
            buf = buf[CHUNK_MAX:]
    if buf:
        chunks.append(buf)
    # 滤掉太短的噪音块，它们入库只会稀释检索质量
    return [c for c in chunks if len(c) >= CHUNK_MIN]


_TS_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*")
# 匹配 agent.py 给真实用户输入加的时间戳前缀，建索引时剥掉它


def _clean_noise(text: str) -> str:
    """清洗流水里的搜索回显：'Search results for query: ...' 开头的行剥掉；
    正文中途混入回显的保守起见整段丢弃（与 subagent.py 的 INVALID_PATTERNS 同源）"""
    kept = [ln for ln in text.splitlines()
            if not ln.startswith("Search results for query:")]
    cleaned = "\n".join(kept).strip()
    return "" if "Search results for query:" in cleaned else cleaned


def _message_text(record):
    """把 history.jsonl 一条记录提取成纯文本；没法提取的返回空串。
    content 有两种形态：纯字符串（用户输入/系统提醒）、
    块列表（assistant 的 text/tool_use，或 user 角色的 tool_result）"""
    content = record.get("content")
    if isinstance(content, str):
        return _clean_noise(_TS_PREFIX.sub("", content).strip())   # 剥前缀+清噪音
    if isinstance(content, list):
        # 只保留 text 块：tool_use/tool_result 是过程噪音，检索价值低还占库
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return _clean_noise("\n".join(p for p in parts if p).strip())
    return ""


def collect_chunks():
    """扫描记忆目录，汇总所有待索引的块，每块 = {text, source, date}"""
    chunks = []

    # ---- 来源一：每日情景记忆 memory/YYYY-MM-DD.md（检索主力）----
    # glob 的 ????-??-??.md 只匹配日期文件名，不会误伤 MEMORY.md
    for path in sorted(MEMORY_DIR.glob("????-??-??.md")):
        text = path.read_text(encoding="utf-8")
        for c in chunk_text(text):
            chunks.append({"text": c, "source": path.name, "date": path.stem})

    # ---- 来源二：长期记忆 MEMORY.md ----
    # 现在它全文注入 prompt；哪天大到塞不下了，检索库里已经有它
    mem_path = MEMORY_DIR / "MEMORY.md"
    if mem_path.exists():
        for c in chunk_text(mem_path.read_text(encoding="utf-8")):
            chunks.append({"text": c, "source": "MEMORY.md", "date": "长期"})

    # ---- 来源三：原始流水 history.jsonl（一条消息切一次块）----
    history = MEMORY_DIR / "history.jsonl"
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)          # 每行 = {"ts","role","content"}
            text = _message_text(record)
            for c in chunk_text(text):         # 单条消息也可能很长，照样切块
                chunks.append({"text": c, "source": "history.jsonl",
                               "date": record.get("ts", "")[:10]})  # 取日期头10位

    # 去重：流水里同一条消息可能出现多次（重试/回显），相同的块只留第一份，
    # 否则重复内容会占满 top_k，把其他相关块挤出去
    seen, unique = set(), []
    for c in chunks:
        if c["text"] not in seen:
            seen.add(c["text"])
            unique.append(c)
    return unique


def build_index():
    """全量重建索引：收集块 → 向量化 → 两个文件落盘。
    v1 故意全量：简单、幂等（跑多少次结果一样）、不会和文件不同步"""
    import numpy as np   # 函数内 import：用到才加载；二次调用走 sys.modules 缓存，零成本
    chunks = collect_chunks()
    if not chunks:
        print("没有找到任何可索引的内容")
        return
    print(f"共 {len(chunks)} 个块，开始向量化...")
    vectors = embed([c["text"] for c in chunks])

    STORE_DIR.mkdir(parents=True, exist_ok=True)
    # 向量列表堆成 (N, 1024) 矩阵存盘；float32 精度够用，体积比默认 float64 减半
    np.save(VEC_FILE, np.asarray(vectors, dtype=np.float32))
    # 原文+元数据逐行存 JSONL：第 i 行 ↔ 矩阵第 i 行，靠行号对齐
    with CHUNK_FILE.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"索引完成：{len(chunks)} 块 → {VEC_FILE} / {CHUNK_FILE}")


def search(query, top_k=3):
    """语义检索入口——agent 的 search_memory 工具调用的就是它。
    两段式：双塔粗筛圈 50 个候选 → 交叉编码器精排取前 top_k。返回带出处的文本"""
    import numpy as np   # 函数内 import：用到才加载；二次调用走 sys.modules 缓存，零成本
    if not VEC_FILE.exists():
        return "(记忆索引不存在，请先运行 python memory_rag.py build)"

    db = np.load(VEC_FILE)                    # (N, 1024) 全库向量矩阵
    chunks = [json.loads(line) for line in
              CHUNK_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    q = np.asarray(embed([query])[0], dtype=np.float32)   # (1024,) 查询向量

    # 向量化余弦：db @ q 是矩阵乘向量 = 每个库向量与 q 的点积（一次算完全库）
    # 再除以各自的模长——就是手写 cosine() 的公式，只是从循环变成了矩阵运算
    scores = db @ q / (np.linalg.norm(db, axis=1) * np.linalg.norm(q) + 1e-9)
    # （1e-9 防零向量除零，工程惯例）

    # ---- 第一段 · 粗筛（双塔）：全库余弦排序，放宽圈 N 个候选，宁多勿漏 ----
    candidates = np.argsort(scores)[::-1][:COARSE_TOP_N]  # argsort 升序，[::-1] 翻降序

    # 相似度闸：候选全部低于下限就如实说"没找到"——
    # 把 0.4 分的不相关块塞给 agent，比承认没有更糟（会诱导它瞎编），还省一次精排调用
    candidates = [i for i in candidates if scores[i] >= SCORE_MIN]
    if not candidates:
        return (f"(未检索到与「{query}」足够相关的记忆：粗筛最高分仅 "
                f"{scores.max():.4f}，低于下限 {SCORE_MIN}。"
                f"请如实告知用户没找到，禁止编造；或换个说法重试)")

    # ---- 第二段 · 精排（交叉编码器）：query 和候选原文一起读，逐对打分 ----
    fallback_note = ""
    try:
        ranked = rerank(query, [chunks[i]["text"] for i in candidates])
    except Exception as exc:
        ranked = []   # 精排服务故障不该拖垮检索：退回粗筛顺序顶上
        fallback_note = f"(精排不可用，已退回粗筛顺序：{exc})\n"

    if ranked:
        # ranked 的下标是"候选数组里的位置"，要映射回库行号；取前 top_k 片
        final = [(candidates[idx], rscore) for idx, rscore in ranked[:top_k]]
    else:
        final = [(i, None) for i in candidates[:top_k]]

    # 拼成带出处的文本块：两个分数都标上，方便对比粗筛/精排的尺度差异；
    # 出处让 agent 能引用"这是哪天的事"，也防止它瞎编
    blocks = []
    for rank, (i, rscore) in enumerate(final, 1):
        c = chunks[i]
        score_txt = (f"粗筛 {scores[i]:.3f} / 精排 {rscore:.4f}" if rscore is not None
                     else f"粗筛 {scores[i]:.3f}（未精排）")
        blocks.append(f"[{rank}] ({score_txt}, 来源: {c['source']}, "
                      f"日期: {c['date']})\n{c['text']}")
    return fallback_note + "\n\n".join(blocks)


if __name__ == "__main__":
    # 命令行入口：build 建索引；search 手动测试；乱输就打印模块顶部文档
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build_index()
    elif len(sys.argv) >= 3 and sys.argv[1] == "search":
        print(search(sys.argv[2]))
    else:
        print(__doc__)
