import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import threading
import time
import psutil
import pystray
from PIL import Image, ImageDraw
import os
import signal
import sys

# ---------------- 配置持久化 ----------------
CONFIG_FILE = "llama_monitor_config.json"

DEFAULT_CONFIG = {
    "server_cmd": "llama-server -m D:\models\lmstudio-community\Qwen3-0.6B-GGUF\Qwen3-0.6B-Q6_K.gguf -ngl 99 -c 4096 --host 0.0.0.0 --port 8080 --alias local",
    "mem_high_mb": 1532,         # 模型进程占用超过此值时重启
    "mem_low_mb": 1024,          # 系统可用内存低于此值时关闭
    "mem_start_mb": 2048,        # 系统可用内存高于此值时启动 llama
    "debounce_sec": 30,          # 防抖动时长（秒）— 用于重启/关闭
    "start_debounce_sec": 10,    # 启动防抖时长（秒）— 单独配置
    "check_interval": 2,         # 检查间隔
}

def load_config():
    import json
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    import json
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存配置失败:", e)


class LlamaMonitor:
    def __init__(self):
        self.config = load_config()
        self.proc = None
        self.running = False
        self.monitor_thread = None
        self.lock = threading.Lock()
        self.tray_icon = None

        # 防抖动状态
        self.high_since = None
        self.low_since = None
        self.start_since = None   # 系统内存充足、等待启动的计时起点

        self.build_ui()

    # ---------------- UI ----------------
    def build_ui(self):
        self.root = tk.Tk()
        self.root.title("Llama Server 监控")
        self.root.geometry("560x460")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="启动命令:").grid(row=0, column=0, sticky="w")
        self.cmd_var = tk.StringVar(value=self.config["server_cmd"])
        ttk.Entry(frm, textvariable=self.cmd_var, width=50).grid(row=0, column=1, columnspan=2, sticky="we")

        ttk.Label(frm, text="模型占用上限 (MB):").grid(row=1, column=0, sticky="w", pady=5)
        self.mem_high_var = tk.StringVar(value=str(self.config["mem_high_mb"]))
        ttk.Entry(frm, textvariable=self.mem_high_var, width=12).grid(row=1, column=1, sticky="w")

        ttk.Label(frm, text="系统剩余下限 (MB):").grid(row=2, column=0, sticky="w", pady=5)
        self.mem_low_var = tk.StringVar(value=str(self.config["mem_low_mb"]))
        ttk.Entry(frm, textvariable=self.mem_low_var, width=12).grid(row=2, column=1, sticky="w")

        ttk.Label(frm, text="系统剩余启动阈值 (MB):").grid(row=3, column=0, sticky="w", pady=5)
        self.mem_start_var = tk.StringVar(value=str(self.config["mem_start_mb"]))
        ttk.Entry(frm, textvariable=self.mem_start_var, width=12).grid(row=3, column=1, sticky="w")

        ttk.Label(frm, text="重启/关闭防抖 (秒):").grid(row=4, column=0, sticky="w", pady=5)
        self.debounce_var = tk.StringVar(value=str(self.config["debounce_sec"]))
        ttk.Entry(frm, textvariable=self.debounce_var, width=12).grid(row=4, column=1, sticky="w")

        ttk.Label(frm, text="启动防抖 (秒):").grid(row=5, column=0, sticky="w", pady=5)
        self.start_debounce_var = tk.StringVar(value=str(self.config["start_debounce_sec"]))
        ttk.Entry(frm, textvariable=self.start_debounce_var, width=12).grid(row=5, column=1, sticky="w")

        ttk.Label(frm, text="检查间隔 (秒):").grid(row=6, column=0, sticky="w", pady=5)
        self.interval_var = tk.StringVar(value=str(self.config["check_interval"]))
        ttk.Entry(frm, textvariable=self.interval_var, width=12).grid(row=6, column=1, sticky="w")

        btn_frame = ttk.Frame(frm)
        btn_frame.grid(row=7, column=0, columnspan=3, pady=10, sticky="we")
        self.start_btn = ttk.Button(btn_frame, text="启动监控", command=self.start)
        self.start_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止监控", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        ttk.Button(btn_frame, text="隐藏到托盘", command=self.hide_to_tray).pack(side="left", padx=5)

        self.status_var = tk.StringVar(value="状态: 未启动")
        ttk.Label(frm, textvariable=self.status_var, foreground="blue").grid(row=8, column=0, columnspan=3, sticky="w", pady=5)

        self.log_text = tk.Text(frm, height=10, width=70)
        self.log_text.grid(row=9, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(9, weight=1)
        frm.columnconfigure(1, weight=1)

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        try:
            self.log_text.insert("end", line)
            self.log_text.see("end")
        except Exception:
            pass
        print(line, end="")

    # ---------------- 进程管理 ----------------
    def launch_server(self):
        cmd = self.cmd_var.get().strip()
        if not cmd:
            self.log("启动命令为空")
            return False
        try:
            if os.name == "nt":
                self.proc = subprocess.Popen(cmd, shell=True)
            else:
                self.proc = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
            self.log(f"启动 llama-server, PID={self.proc.pid}")
            return True
        except Exception as e:
            self.log(f"启动失败: {e}")
            return False

    def kill_server(self):
        if self.proc is None:
            return
        try:
            if os.name == "nt":
                subprocess.run(f"taskkill /F /T /PID {self.proc.pid}", shell=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                except Exception:
                    self.proc.terminate()
        except Exception as e:
            self.log(f"终止进程异常: {e}")
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.log("llama-server 已停止")
        self.proc = None

    def restart_server(self):
        self.log("正在重启 llama-server ...")
        self.kill_server()
        time.sleep(1)
        self.launch_server()

    # ---------------- 进程树内存 ----------------
    def get_tree_memory(self, pid):
        """获取进程及其子进程的总内存 (RSS, MB)"""
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return 0
        total = 0
        try:
            procs = [parent] + parent.children(recursive=True)
        except psutil.NoSuchProcess:
            procs = [parent]
        for p in procs:
            try:
                total += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total / (1024 * 1024)

    # ---------------- 监控循环 ----------------
    def monitor_loop(self):
        while self.running:
            try:
                self.read_config_from_ui()
                high_limit = self.config["mem_high_mb"]
                low_limit = self.config["mem_low_mb"]
                start_limit = self.config["mem_start_mb"]
                debounce = self.config["debounce_sec"]
                start_debounce = self.config["start_debounce_sec"]
                interval = max(0.5, self.config["check_interval"])

                now = time.time()

                # ---- 进程不存在/已退出：按内存阈值 + 启动防抖决定是否启动 ----
                if self.proc is None or self.proc.poll() is not None:
                    if self.proc is not None:
                        self.log("检测到 llama-server 已退出, 等待内存条件满足后重启...")
                        self.proc = None
                    self.high_since = None
                    self.low_since = None

                    sys_avail_mb = psutil.virtual_memory().available / (1024 * 1024)

                    if sys_avail_mb >= start_limit:
                        if self.start_since is None:
                            self.start_since = now
                            self.log(f"系统可用内存 {sys_avail_mb:.0f}MB ≥ 启动阈值 {start_limit}MB, "
                                     f"开始计时 ({start_debounce}s 后启动)")
                        elif now - self.start_since >= start_debounce:
                            self.log(f"系统内存持续 {start_debounce}s 满足启动条件, 启动 llama-server")
                            self.launch_server()
                            self.start_since = None
                    else:
                        if self.start_since is not None:
                            self.log(f"系统可用内存 {sys_avail_mb:.0f}MB 低于启动阈值 {start_limit}MB, 重置启动计时")
                        self.start_since = None
                        self.status_var.set(
                            f"状态: 等待内存充足 | 系统可用={sys_avail_mb:.0f}MB / 启动阈值={start_limit}MB"
                        )

                    time.sleep(interval)
                    continue

                # ---- 进程在运行，重置启动计时 ----
                self.start_since = None

                # 系统可用内存 (MB)
                sys_avail_mb = psutil.virtual_memory().available / (1024 * 1024)
                proc_mem_mb = self.get_tree_memory(self.proc.pid)

                self.status_var.set(
                    f"状态: 运行中 PID={self.proc.pid} | 进程内存={proc_mem_mb:.0f}MB | 系统可用={sys_avail_mb:.0f}MB"
                )

                # 检查模型占用过高 -> 重启
                if proc_mem_mb >= high_limit:
                    if self.high_since is None:
                        self.high_since = now
                        self.log(f"进程内存 {proc_mem_mb:.0f}MB 超过上限 {high_limit}MB, 开始计时防抖")
                    elif now - self.high_since >= debounce:
                        self.log(f"进程内存持续 {debounce}s 超过上限, 执行重启")
                        self.restart_server()
                        self.high_since = None
                        self.low_since = None
                else:
                    self.high_since = None

                # 检查系统内存过低 -> 关闭
                if sys_avail_mb <= low_limit:
                    if self.low_since is None:
                        self.low_since = now
                        self.log(f"系统可用内存 {sys_avail_mb:.0f}MB 低于下限 {low_limit}MB, 开始计时防抖")
                    elif now - self.low_since >= debounce:
                        self.log(f"系统内存持续 {debounce}s 低于下限, 执行关闭")
                        self.kill_server()
                        self.running = False
                        self.low_since = None
                        self.high_since = None
                        # 更新 UI（在子线程里通过 after）
                        self.root.after(0, self.on_stopped)
                        break
                else:
                    self.low_since = None

                time.sleep(interval)
            except Exception as e:
                self.log(f"监控循环异常: {e}")
                time.sleep(2)

    def read_config_from_ui(self):
        try:
            self.config["server_cmd"] = self.cmd_var.get()
            self.config["mem_high_mb"] = int(self.mem_high_var.get())
            self.config["mem_low_mb"] = int(self.mem_low_var.get())
            self.config["mem_start_mb"] = int(self.mem_start_var.get())
            self.config["debounce_sec"] = float(self.debounce_var.get())
            self.config["start_debounce_sec"] = float(self.start_debounce_var.get())
            self.config["check_interval"] = float(self.interval_var.get())
            save_config(self.config)
        except ValueError:
            pass

    # ---------------- 控制 ----------------
    def start(self):
        if self.running:
            return
        self.read_config_from_ui()
        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("状态: 启动中...")
        self.start_since = None
        # 点击启动时立即启动，不判断阈值
        if not self.launch_server():
            self.running = False
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.status_var.set("状态: 启动失败")
            return
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.kill_server()
        self.start_since = None
        self.status_var.set("状态: 已停止")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def on_stopped(self):
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("状态: 已因系统内存不足关闭")

    # ---------------- 托盘 ----------------
    def make_tray_image(self):
        img = Image.new("RGB", (64, 64), (60, 60, 60))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(0, 180, 0))
        d.text((24, 20), "L", fill=(255, 255, 255))
        return img

    def hide_to_tray(self):
        self.root.withdraw()
        if self.tray_icon is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示", self.show_window, default=True),
            pystray.MenuItem("启动监控", lambda: self.root.after(0, self.start)),
            pystray.MenuItem("停止监控", lambda: self.root.after(0, self.stop)),
            pystray.MenuItem("退出", self.quit_all),
        )
        self.tray_icon = pystray.Icon("llama_monitor", self.make_tray_image(), "Llama Server 监控", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def show_window(self, icon=None, item=None):
        self.root.after(0, self.root.deiconify)

    def quit_all(self, icon=None, item=None):
        self.running = False
        self.kill_server()
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.after(0, self.root.destroy)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = LlamaMonitor()
    app.run()
