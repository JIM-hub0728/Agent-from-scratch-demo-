"""全局配置模板：复制为 config.py 并填入你的 API key。
注意：config.py 含敏感信息，已被 .gitignore 排除，不要提交。"""
import os

# ---- 主模型（Kimi，Anthropic 兼容端点）----
MAIN_API_KEY = "your-kimi-api-key-here"
MAIN_BASE_URL = "https://api.kimi.com/coding/"
MODEL = "k3"

# ---- 记忆整理员（DeepSeek deepseek-flash：压缩/提取是格式化任务，用便宜快的模型）----
COMPACT_MODEL = "deepseek-flash"
COMPACT_API_KEY = "your-deepseek-api-key-here"
COMPACT_BASE_URL = "https://api.deepseek.com/anthropic"

# ---- embedding / rerank（RAG，SiliconFlow，两个接口共用一个 key）----
RAG_API_KEY = "your-siliconflow-api-key-here"
RAG_API_URL = "https://api.siliconflow.cn/v1/embeddings"
RAG_MODEL = "BAAI/bge-m3"
RERANK_URL = "https://api.siliconflow.cn/v1/rerank"   # 精排接口（同一家平台，同一个 key）
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"              # 交叉编码器：query+候选一起读，逐对打分

# 单次响应的 token 上限：是"上限"不是"预支"，按实际生成量计费，调大不多花钱。
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "16384"))

# 截断后自动续写的最大次数：防模型陷入"写了断、断了写"的死循环
MAX_CONTINUATIONS = 3
