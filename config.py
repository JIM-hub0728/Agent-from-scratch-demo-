"""全局配置：模型、额度与各供应商 API 配置收口在一处。
注意：本文件含 API key，不要提交到 git。"""
import os

# ---- 主模型----
MAIN_API_KEY = ""
MAIN_BASE_URL = ""
MODEL = ""

# ---- 记忆整理员（用便宜快的模型）----
COMPACT_API_KEY = ""
COMPACT_BASE_URL = ""
COMPACT_MODEL = ""

# ---- embedding模型（RAG）----
RAG_API_KEY = ""
RAG_API_URL = ""
RAG_MODEL = ""
RERANK_URL = ""   
RERANK_MODEL = "" 

# 单次响应的 token 上限：是"上限"不是"预支"，按实际生成量计费，调大不多花钱。
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "16384"))

# 截断后自动续写的最大次数：防模型陷入"写了断、断了写"的死循环
MAX_CONTINUATIONS = 3
