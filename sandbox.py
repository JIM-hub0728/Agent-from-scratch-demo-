"""代码沙箱：隔离运行 Python/Node 代码、装依赖、跑测试。

隔离手段：独立工作区目录 + 剥离敏感环境变量 + 无 shell 直接执行（绕开 cmd 的不稳定）
+ 超时杀整棵进程树 + 输出过大由 tools 层落盘。产物留在工作区，agent 可用 read_file/list_dir 查看。

注意边界：这是工作区级隔离，不是容器级安全沙箱——无法硬性限制内存/CPU，
代码仍能访问网络与文件系统。需要更强隔离时应换 Docker / Windows Job Object。
"""
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SANDBOX_ROOT = Path(tempfile.gettempdir()) / "agent_sandbox"
WORKSPACE = SANDBOX_ROOT / "workspace"   # 任务工作目录（产物在这里）
VENV_DIR = SANDBOX_ROOT / "venv"         # Python 虚拟环境：跨任务复用，免反复建
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
INSTALL_TIMEOUT = 180
MAX_CODE_CHARS = 100_000
MAX_ARTIFACTS = 20

_LANG_ALIASES = {"python": "python", "py": "python",
                 "node": "node", "javascript": "node", "js": "node"}
_ENTRY_NAME = {"python": "main.py", "node": "main.js"}
_PKG_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@/_.~+\-=,\[\]]*$")  # 包名白名单，防 shell 元字符
_SENSITIVE_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD", re.IGNORECASE)
_SKIP_DIRS = {"node_modules", "__pycache__"}

_run_lock = threading.Lock()  # 主 agent / 子代理 / 队友共用同一工作区，串行防互踩


def _safe_env() -> dict:
    """剥掉名字里带 KEY/TOKEN/SECRET/PASSWORD 的环境变量：沙箱代码摸不到密钥"""
    env = {k: v for k, v in os.environ.items() if not _SENSITIVE_RE.search(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _ensure_venv() -> Path:
    """首次使用时建虚拟环境（约十几秒），之后复用"""
    py = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)],
                       check=True, capture_output=True, timeout=INSTALL_TIMEOUT)
    return py


def _pip_install(packages: list) -> str:
    """装 Python 依赖到沙箱 venv；成功返回空串，失败返回错误信息"""
    py = _ensure_venv()
    base = [str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    result = subprocess.run(base + packages,
                            capture_output=True, text=True, errors="replace",
                            timeout=INSTALL_TIMEOUT)
    if result.returncode != 0:
        # 用户 pip 可能配了失效的镜像源（如 403 的 tuna）：换 PyPI 官方源兜底重试一次
        result = subprocess.run(base + ["-i", "https://pypi.org/simple"] + packages,
                                capture_output=True, text=True, errors="replace",
                                timeout=INSTALL_TIMEOUT)
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-1000:]
        return f"(依赖安装失败: {tail})"
    return ""


def _npm_install(packages: list) -> str:
    """装 Node 依赖到工作区。npm 在 Windows 是 .cmd 必须经 shell 启动；包名已过白名单，注入无路"""
    npm = shutil.which("npm")
    if not npm:
        return "(未检测到 npm，无法安装 Node 依赖)"
    pkg_json = WORKSPACE / "package.json"
    if not pkg_json.exists():
        pkg_json.write_text('{"name":"sandbox","private":true}', encoding="utf-8")
    result = subprocess.run(
        [npm, "install", "--silent", *packages], cwd=WORKSPACE, shell=(os.name == "nt"),
        capture_output=True, text=True, errors="replace", timeout=INSTALL_TIMEOUT)
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip()[-1000:]
        return f"(依赖安装失败: {tail})"
    return ""


def _kill_tree(proc):
    """超时后杀整棵进程树：只杀父进程，孙进程会变孤儿继续烧资源"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def _list_artifacts(entry: str) -> list:
    """工作区里除入口文件和依赖目录之外的文件（即本次运行可能产生的产物）"""
    files = []
    for p in sorted(WORKSPACE.rglob("*")):
        if _SKIP_DIRS & set(p.parts):
            continue
        if p.is_file() and p.name != entry:
            files.append(str(p.relative_to(WORKSPACE)))
        if len(files) >= MAX_ARTIFACTS:
            files.append("...(更多文件省略)")
            break
    return files


def reset_workspace():
    """清空工作区（保留 venv 依赖缓存）"""
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)


def run_code(language: str, code: str, packages: list = None,
             timeout: int = DEFAULT_TIMEOUT, reset: bool = False) -> str:
    """沙箱主入口：写代码文件 → 装依赖 → 无 shell 执行 → 汇总结果"""
    lang = _LANG_ALIASES.get((language or "").lower())
    if not lang:
        return f"(不支持的语言: {language!r}，只支持 python / node)"
    if not code.strip():
        return "(代码为空)"
    if len(code) > MAX_CODE_CHARS:
        return f"(代码超过 {MAX_CODE_CHARS} 字符上限，请拆分或落盘后运行)"
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))

    packages = packages or []
    bad = [p for p in packages if not _PKG_RE.match(p)]
    if bad:
        return f"(非法包名 {bad}：只允许字母数字和 @/_.~+-=,[] 等常规字符)"

    with _run_lock:
        try:
            if reset or not WORKSPACE.exists():
                reset_workspace()
            entry = _ENTRY_NAME[lang]
            (WORKSPACE / entry).write_text(code, encoding="utf-8")

            if packages:
                error = _pip_install(packages) if lang == "python" else _npm_install(packages)
                if error:
                    return error

            if lang == "python":
                cmd = [str(_ensure_venv()), entry]
            else:
                node = shutil.which("node")
                if not node:
                    return "(未检测到 Node.js，请先安装或改用 python)"
                cmd = [node, entry]

            proc = subprocess.Popen(  # 无 shell 直接执行：绕开 cmd 的引号/转义坑
                cmd, cwd=WORKSPACE, env=_safe_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8",  # 与 _safe_env 的 PYTHONIOENCODING=utf-8 对齐：子进程吐 UTF-8，就得按 UTF-8 解码，否则中文输出全是乱码
                errors="replace")
            try:
                output, _ = proc.communicate(timeout=timeout)
                header = f"(exit code {proc.returncode})"
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                output, _ = proc.communicate()
                header = f"(超时：超过 {timeout}s 上限，已终止整个进程树)"
        except (OSError, subprocess.SubprocessError) as e:
            return f"(沙箱执行失败: {e})"

        body = (output or "").strip() or "(无输出)"
        artifacts = _list_artifacts(entry)
        if artifacts:
            body += (f"\n(工作区产物: {', '.join(artifacts)}；"
                     f"完整路径前缀 {WORKSPACE}，可用 read_file 读取)")
        return header + "\n" + body


def _cleanup_workspace():
    """退出时回收产物（venv 留在系统临时目录当缓存，由 OS 定期清理）"""
    shutil.rmtree(WORKSPACE, ignore_errors=True)


atexit.register(_cleanup_workspace)
