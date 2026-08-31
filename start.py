"""
LocWarp 一鍵啟動器
雙擊此檔案即可啟動 LocWarp
"""

import subprocess
import sys
import os
import time
import shutil
import webbrowser
import urllib.request
import socket

# 路徑設定
ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")

BACKEND_PORT = 8777
FRONTEND_PORT = 5173
FRONTEND_FALLBACK_PORT = 3001
FRONTEND_FALLBACK_SCAN_LIMIT = 20

procs = []


def print_banner():
    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   LocWarp — iOS 虛擬定位模擬器 v0.1     ║")
    print("  ╚══════════════════════════════════════════╝")
    print()


def check_tool(name, hint):
    if shutil.which(name):
        print(f"  [✓] 已找到 {name}")
        return True
    else:
        print(f"  [✗] 找不到 {name}，請先安裝：{hint}")
        return False


def is_port_open(port):
    """檢查 port 是否有服務在監聽"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def can_bind_port(port):
    """Return whether the frontend can bind its listen address to *port*."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


def choose_frontend_port():
    """Choose a usable frontend port, including Windows-reserved-port fallback."""
    candidates = [
        FRONTEND_PORT,
        *range(
            FRONTEND_FALLBACK_PORT,
            FRONTEND_FALLBACK_PORT + FRONTEND_FALLBACK_SCAN_LIMIT,
        ),
    ]
    for port in candidates:
        if can_bind_port(port):
            return port
    return None


def kill_port(port):
    """清理佔用指定 port 的進程"""
    if os.name == "nt":
        result = subprocess.run(
            f'netstat -ano | findstr ":{port}" | findstr "LISTENING"',
            capture_output=True, text=True, shell=True,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if parts:
                pid = parts[-1]
                subprocess.run(f"taskkill /pid {pid} /f",
                               shell=True, capture_output=True)


def wait_for_port(
    port,
    label,
    timeout=60,
    process=None,
    watched_processes=(),
):
    print(f"      等待{label}啟動中", end="", flush=True)
    start = time.time()
    while time.time() - start < timeout:
        if process is not None:
            return_code = process.poll()
            if return_code is not None:
                print(f" 失敗（程序已退出，exit code {return_code}）")
                return False
        for watched_label, watched_process in watched_processes:
            return_code = watched_process.poll()
            if return_code is not None:
                print(
                    f" 失敗（{watched_label}程序已退出，"
                    f"exit code {return_code}）"
                )
                return False
        if is_port_open(port):
            print(" OK ✓")
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    print(" 超時！")
    return False


def install_backend():
    print("  [1/4] 檢查後端依賴...", end=" ", flush=True)
    req = os.path.join(BACKEND, "requirements.txt")

    dry = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", req, "--dry-run", "-q"],
        capture_output=True, text=True,
    )

    if "would install" not in dry.stdout.lower():
        print("已就緒 ✓")
    else:
        print("安裝中...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req, "-q"],
            cwd=BACKEND,
        )
        print("        完成 ✓")


def install_frontend():
    print("  [2/4] 檢查前端依賴...", end=" ", flush=True)
    nm = os.path.join(FRONTEND, "node_modules")
    if os.path.isdir(nm):
        print("已就緒 ✓")
    else:
        print("安裝中...")
        subprocess.run(["npm", "install"], cwd=FRONTEND, shell=True)
        print("        完成 ✓")


def start_backend():
    print(f"  [3/4] 啟動後端服務 (port {BACKEND_PORT})...")

    # 清理殘留
    if is_port_open(BACKEND_PORT):
        print(f"      Port {BACKEND_PORT} 被佔用，清理中...")
        kill_port(BACKEND_PORT)
        time.sleep(1)

    p = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=BACKEND,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    procs.append(p)
    if not wait_for_port(BACKEND_PORT, "後端", process=p):
        return None
    return p


def start_frontend(backend_process=None):
    global FRONTEND_PORT

    requested_port = FRONTEND_PORT
    print(f"  [4/4] 啟動前端服務 (port {requested_port})...")

    # 清理殘留
    if is_port_open(requested_port):
        print(f"      Port {requested_port} 被佔用，清理中...")
        kill_port(requested_port)
        time.sleep(1)

    selected_port = choose_frontend_port()
    if selected_port is None:
        print("      找不到可用的前端 port（已檢查預設與 fallback 埠）")
        return None
    FRONTEND_PORT = selected_port
    if selected_port != requested_port:
        print(
            f"      Port {requested_port} 無法綁定，改用 port {selected_port}"
        )

    # 用 --port 強制指定 port，避免 Vite 跳到其他 port
    p = subprocess.Popen(
        ["npx", "vite", "--host", "--port", str(FRONTEND_PORT), "--strictPort"],
        cwd=FRONTEND,
        shell=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    procs.append(p)
    watched_processes = (
        (("後端", backend_process),) if backend_process is not None else ()
    )
    if not wait_for_port(
        FRONTEND_PORT,
        "前端",
        process=p,
        watched_processes=watched_processes,
    ):
        return None
    return p


def _enter_pressed():
    """Consume one console key and report whether it was Enter."""
    if os.name == "nt":
        import msvcrt

        if not msvcrt.kbhit():
            return False
        return msvcrt.getwch() in ("\r", "\n")

    import select

    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return False
    return sys.stdin.readline() in ("\n", "\r\n", "")


def wait_for_shutdown(backend_process, poll_interval=0.25):
    """Wait for Enter while ensuring the owned backend process stays alive."""
    while True:
        return_code = backend_process.poll()
        if return_code is not None:
            print(
                f"\n  [錯誤] 後端服務意外停止（exit code {return_code}）；"
                "LocWarp 將關閉前端。"
            )
            return False
        if _enter_pressed():
            return True
        time.sleep(poll_interval)


def cleanup():
    print("\n  正在關閉所有服務...")
    for p in procs:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    # 強制清理殘留 port
    kill_port(BACKEND_PORT)
    kill_port(FRONTEND_PORT)
    print("  已停止。再見！")


def check_admin():
    """Check if running with administrator privileges."""
    import ctypes
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def main():
    os.system("title LocWarp")
    print_banner()

    # 檢查管理員權限 (iOS 17+ 需要)
    if os.name == "nt" and not check_admin():
        print("  [!] 未以系統管理員身份執行")
        print("      iOS 17+ 裝置需要管理員權限才能建立通道")
        print("      請右鍵 LocWarp.bat → 以系統管理員身份執行")
        print()

    # 檢查環境
    ok = True
    ok = check_tool("python", "https://www.python.org/downloads/") and ok
    ok = check_tool("node", "https://nodejs.org/") and ok
    ok = check_tool("npm", "隨 Node.js 一起安裝") and ok
    print()

    if not ok:
        input("  缺少必要工具，請安裝後重試。按 Enter 離開...")
        return 1

    # 安裝依賴
    install_backend()
    print()
    install_frontend()
    print()

    # 啟動服務
    backend_process = start_backend()
    if backend_process is None:
        print("  [錯誤] 後端啟動失敗，請查看上方錯誤訊息")
        cleanup()
        input("  按 Enter 離開...")
        return 1
    print()

    if start_frontend(backend_process) is None:
        print("  [錯誤] 前端啟動失敗")
        cleanup()
        input("  按 Enter 離開...")
        return 1
    print()

    # 等待 Vite 完成首次編譯後再開瀏覽器
    time.sleep(2)
    url = f"http://localhost:{FRONTEND_PORT}"
    webbrowser.open(url)

    print("  ╔══════════════════════════════════════════╗")
    print("  ║          LocWarp 已就緒！                ║")
    print("  ╠══════════════════════════════════════════╣")
    print(f"  ║  前端畫面:  http://localhost:{FRONTEND_PORT}        ║")
    print(f"  ║  後端 API:  http://localhost:{BACKEND_PORT}        ║")
    print(f"  ║  API 文件:  http://localhost:{BACKEND_PORT}/docs   ║")
    print("  ╠══════════════════════════════════════════╣")
    print("  ║  按 Enter 停止所有服務                   ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    clean_exit = wait_for_shutdown(backend_process)
    cleanup()
    return 0 if clean_exit else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        cleanup()
