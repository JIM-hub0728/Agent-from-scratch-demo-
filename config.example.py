"""全局配置模板：复制为 config.py 并填入你的 API key。
注意：config.py 含敏感信息，已被 .gitignore 排除，不要提交。"""
import os

# ---- 主模型（Kimi）----
MODEL = "k3"
KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "your-kimi-api-key-here")
KIMI_BASE_URL = "https://api.kimi.com/coding/"

# ---- 记忆整理员（DeepSeek deepseek-flash：压缩/提取是格式化任务，用便宜快的模型）----
CURATOR_MODEL = "deepseek-flash"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "your-deepseek-api-key-here")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"

# 单次响应的 token 上限：是"上限"不是"预支"，按实际生成量计费，调大不多花钱。
# 4096 太小：写大文件/长回答必被截断。环境变量可临时覆盖（测试截断续写时用）
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "16384"))

# 截断后自动续写的最大次数：防模型陷入"写了断、断了写"的死循环
MAX_CONTINUATIONS = 3
