"""Furniture Workflow 网站一键启动器.

双击打包后的 exe（或直接运行本脚本）会：
  1. 启动后端 API 服务（uvicorn，端口 8000）
  2. 启动前端 Web 服务（Next.js production build，端口 3000）
  3. 等待两个服务就绪后自动用系统默认浏览器打开 http://127.0.0.1:3000
  4. 当前窗口实时显示两个服务的日志；关闭窗口或按 Ctrl+C 即停止服务

依赖机器上已安装的 Python 与 Node 环境。本文件只把本地白名单配置传给子进程，绝不打印密钥。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

API_PORT = 8000
WEB_PORT = 3000
API_HEALTH_URL = f"http://127.0.0.1:{API_PORT}/api/v1/health"
WEB_URL = f"http://127.0.0.1:{WEB_PORT}/"
WEBSITE_VERSION = "0.16.0"

# .env.local is deliberately ignored by Git. Keep this list explicit so the
# launcher never forwards arbitrary local variables (for example shell or
# test switches) into a production-like run. Values already present in the
# process environment take precedence over this file.
LOCAL_ENV_KEYS = frozenset({
    "APP_ENV",
    "OUTPUT_ROOT",
    "WORKFLOW_RELEASE_VERSION",
    "DATABASE_URL",
    "WEB_BASE_URL",
    "LOCAL_REVIEW_MODE",
    "WEBSITE_MODEL_MODE",
    "LUNAMAX_MODEL_MODE",
    "PROVIDER_MODE",
    "WEBSITE_L2_BROWSER_ENGINE",
    "WEBSITE_L2_NAVIGATION_TIMEOUT_MS",
    "WEBSITE_L2_TEMP_FAILURE_RETRIES",
    "WEBSITE_BROWSER_HANDOFF_SECONDS",
    "WEBSITE_L2_COUNT_PROBES",
    "WEBSITE_BRAIN_API_KEY",
    "WEBSITE_BRAIN_BASE_URL",
    "WEBSITE_BRAIN_MODEL",
    "WEBSITE_BRAIN_TIMEOUT_SECONDS",
    "WEBSITE_BRAIN_MAX_RETRIES",
    "WEBSITE_BRAIN_RPM_LIMIT",
    "WEBSITE_VISION_API_KEY",
    "WEBSITE_VISION_BASE_URL",
    "WEBSITE_VISION_MODEL",
    "WEBSITE_VISION_TIMEOUT_SECONDS",
    "WEBSITE_VISION_MAX_RETRIES",
    "WEBSITE_VISION_RPM_LIMIT",
    "LUX3D_API_KEY",
    "LUX3D_BASE_URL",
    "LUX3D_VERSION",
    "LUX3D_FACE_COUNT",
    "LUX3D_QUERY_INTERVAL",
    "LUX3D_QUERY_MAX_ATTEMPTS",
    "MODELING_ENABLED",
    "EXACT_COUNT_AUTHORIZATION",
    "BLENDER_WORKER_ENABLED",
    "BLENDER_EXECUTABLE",
    "AUTO_MODEL_COST_CEILING_MINOR",
    "MODEL_ESTIMATED_COST_PER_ITEM_MINOR",
    "INTERNAL_SERVICE_TOKEN",
    "INTRANET_AUTH_USER",
    "INTRANET_AUTH_PASSWORD",
})


def app_root() -> Path:
    """定位 Website 项目根目录（含 services/api、apps/web）。"""
    if getattr(sys, "frozen", False):  # 打包成 exe 时
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_env_values(root: Path) -> dict[str, str]:
    """从 .env.local 读取白名单配置；返回值不会被启动器打印。"""
    values: dict[str, str] = {}
    env_file = root / ".env.local"
    if not env_file.is_file():
        return values
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in LOCAL_ENV_KEYS and value:
            values[key] = value
    return values


def find_executable(*names: str) -> str | None:
    """在 PATH 中查找可执行文件（兼容 .cmd / .exe）。"""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def check_port_free(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.5):
            return False  # 已有服务在响应
    except Exception:
        return True


def wait_for(url: str, timeout: float = 90.0, label: str = "") -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                if resp.status < 500:
                    print(f"[启动器] {label or url} 已就绪（{resp.status}）")
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def reader_thread(proc: subprocess.Popen[str], prefix: str) -> None:
    """把子进程输出逐行转发到当前窗口。"""
    if proc.stdout:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            print(f"{prefix} {line.rstrip()}")
    if proc.stderr:
        for line in iter(proc.stderr.readline, ""):
            if not line:
                break
            print(f"{prefix} {line.rstrip()}")


def main() -> int:
    # 无论是真实控制台还是被重定向的管道，都统一用 UTF-8 输出，避免日志中的
    # 特殊字符（如 ✔）触发 GBK 编码异常导致线程崩溃。
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass

    root = app_root()
    env_values = load_env_values(root)

    print("=" * 60)
    print(" Furniture Workflow —— 网站一键启动器")
    print("=" * 60)
    print(f" 项目根目录：{root}")
    print(f" Website 版本：{WEBSITE_VERSION}")

    PYTHON = find_executable("python", "python.exe")
    NPM = find_executable("npm", "npm.cmd")
    if not PYTHON:
        print("[启动器] 未找到 Python，请先安装 Python 3.11+。")
        input("按回车键退出...")
        return 1
    if not NPM:
        print("[启动器] 未找到 Node/npm，请先安装 Node 20.9+。")
        input("按回车键退出...")
        return 1

    if not (root / "apps" / "web" / "node_modules").is_dir():
        print("[启动器] 前端依赖未安装，请先在 apps/web 下执行 npm ci。")
        input("按回车键退出...")
        return 1
    if not (root / "apps" / "web" / ".next" / "BUILD_ID").is_file():
        print("[启动器] 未找到前端 production build，请先在 apps/web 下执行 npm run build。")
        input("按回车键退出...")
        return 1

    # 检测端口占用
    for name, port in (("后端 API", API_PORT), ("前端 Web", WEB_PORT)):
        if not check_port_free(port):
            print(f"[启动器] 端口 {port} 已被占用（{name} 已在运行？）。请关闭占用进程后重试。")
            input("按回车键退出...")
            return 1

    # 构建子进程环境
    env = dict(os.environ)
    # Explicit process variables win; .env.local is a private, ignored local
    # fallback for company machines that launch by double-clicking this file.
    for key, value in env_values.items():
        env.setdefault(key, value)
    env["PYTHONPATH"] = os.pathsep.join([
        str(root),
        str(root / "services" / "api"),
        str(root / "packages" / "workflow-engine" / "src"),
        env.get("PYTHONPATH", ""),
    ])
    env["OUTPUT_ROOT"] = env.get("OUTPUT_ROOT") or str(root.parent / "output" / "web_projects")
    env["WEB_BASE_URL"] = env.get("WEB_BASE_URL") or f"http://localhost:{WEB_PORT}"
    # Keep browser requests same-origin so embedded browsers and isolated
    # browser contexts can reach the API through Next's internal proxy. The
    # proxy forwards to the local API without exposing loopback assumptions to
    # the client bundle.
    env["NEXT_PUBLIC_API_BASE_URL"] = "/api/v1"

    # 1. 启动后端 API
    api_cmd = [PYTHON, "-m", "uvicorn", "app.asgi:app", "--app-dir", str(root / "services" / "api"),
               "--host", "127.0.0.1", "--port", str(API_PORT)]
    print("\n[启动器] 正在启动后端 API（uvicorn，端口 8000）...")
    api_proc = subprocess.Popen(api_cmd, cwd=str(root), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    threading.Thread(target=reader_thread, args=(api_proc, "[API]"), daemon=True).start()

    # 2. 启动前端 Web
    web_cmd = [NPM, "run", "start", "--", "-p", str(WEB_PORT)]
    print("\n[启动器] 正在启动前端 Web（Next.js production，端口 3000）...")
    web_proc = subprocess.Popen(web_cmd, cwd=str(root / "apps" / "web"), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    threading.Thread(target=reader_thread, args=(web_proc, "[WEB]"), daemon=True).start()

    # 3. 等待就绪并打开浏览器
    api_ok = wait_for(API_HEALTH_URL, timeout=90.0, label="后端 API")
    web_ok = wait_for(WEB_URL, timeout=120.0, label="前端 Web")

    if not api_ok or not web_ok:
        print("\n[启动器] 服务启动超时，请查看上方日志排查问题。")
        print("[启动器] 按 Ctrl+C 或关闭窗口即可停止服务。")
    else:
        print("\n" + "=" * 60)
        print(" 服务已全部就绪，正在用浏览器打开网站 ...")
        print(f" 地址：{WEB_URL}")
        print(" 关闭本窗口或按 Ctrl+C 即停止网站服务。")
        print("=" * 60)
        webbrowser.open(WEB_URL)

    # 4. 保持运行，捕获 Ctrl+C / 关闭窗口后清理子进程
    try:
        while True:
            time.sleep(1.0)
            if api_proc.poll() is not None and web_proc.poll() is not None:
                print("\n[启动器] 两个服务均已退出。")
                break
    except KeyboardInterrupt:
        print("\n[启动器] 正在停止服务 ...")
    finally:
        for proc, name in ((api_proc, "API"), (web_proc, "Web")):
            if proc.poll() is None:
                proc.terminate()
        time.sleep(2.0)
        for proc, name in ((api_proc, "API"), (web_proc, "Web")):
            if proc.poll() is None:
                proc.kill()
        print("[启动器] 服务已停止。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 兜底，避免黑色窗口一闪而过
        print(f"[启动器] 发生错误：{exc}")
        try:
            input("按回车键退出...")
        except EOFError:
            pass
        sys.exit(1)
