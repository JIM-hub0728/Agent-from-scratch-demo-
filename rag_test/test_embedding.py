"""
仅用于测试embedding模型
"""

import os 
import math
import requests  

API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
API_URL = ""
MODEL = ""


def embed(texts):
    """把一批文本交给 API，返回一批向量；第 i 个向量对应第 i 段文本。

    texts: 字符串列表，比如 ["句子一", "句子二"]
    返回: 浮点数列表的列表，比如0.01, ...], [0.03, ...]]
    """
    if not texts:
        return []
    payload = {"model": MODEL, "input": texts}

    resp = requests.post(API_URL, headers={"Authorization": f"Bearer {API_KEY}"},
                         json=payload, timeout=30)
    resp.raise_for_status()   # 4xx/5xx 直接抛异常
    result = resp.json()
    if "data" not in result:
        raise RuntimeError(f"API 返回异常: {result}")
    ordered = sorted(result["data"], key=lambda item: item["index"])

    return [item["embedding"] for item in ordered]


def cosine(a, b):
    """余弦相似度：衡量两个向量方向"有多接近，取值约 -1 ~ 1，越大越相似。
    公式：cos = 点积 / (a的长度 × b的长度)"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


if __name__ == "__main__":

    docs = [
        "RAG 是检索增强生成",   # 和查询语义相关
        "喜羊羊与灰太狼都不知道什么是RAG",             # 沾边但不直接回答
        "今天北京天气不错",                 # 完全不相关
    ]
    query = "RAG是什么"

    # 向量化
    doc_vecs = embed(docs)      
    query_vec = embed([query])[0] 

    # 逐一算相似度
    scores = []
    for text, vec in zip(docs, doc_vecs):  
        score = cosine(query_vec, vec) 
        scores.append((score, text))

    for score, text in sorted(scores, reverse=True):
        print(f"{score:.4f}  {text}")