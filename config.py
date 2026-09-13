"""全局配置：模型、额度与各供应商 API 配置收口在一处。
注意：本文件含 API key，不要提交到 git。"""
import os

# ---- 主模型----
MAIN_API_KEY = "sk-cbf8ea52539c44cb8fba1d72a4bf5126"
MAIN_BASE_URL = "https://api.deepseek.com/anthropic"
MODEL = "deepseek-flash"

# ---- 记忆整理员（用便宜快的模型）----
COMPACT_API_KEY = "sk-cbf8ea52539c44cb8fba1d72a4bf5126"
COMPACT_BASE_URL = "https://api.deepseek.com/anthropic"
COMPACT_MODEL = "deepseek-flash"

# ---- embedding模型（RAG）----
RAG_API_KEY = "sk-caoufebmmnvyjtztdekbasbjvmwwtmqdleidmtvmkvikdokl"
RAG_API_URL = "https://api.siliconflow.cn/v1/embeddings"
RAG_MODEL = "BAAI/bge-m3"
RERANK_URL = "https://api.siliconflow.cn/v1/rerank"   # 精排接口（同一家平台，同一个 key）
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"              # 交叉编码器：query+候选一起读，逐对打分

# 单次响应的 token 上限：是"上限"不是"预支"，按实际生成量计费，调大不多花钱。
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "16384"))

# 截断后自动续写的最大次数：防模型陷入"写了断、断了写"的死循环
MAX_CONTINUATIONS = 3
