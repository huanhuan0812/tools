"""
IP 自动轮换工具（Tkinter 版）
- 每 N 秒切换本机 IPv4 最后一位
- 最后一位范围可配置（默认 10~99），可排除指定值（默认 69）
- 开始前保存原始网络配置，停止/关窗时自动恢复
- 自动读取当前网卡 IP/掩码/网关/DNS 并填入输入框
- 支持「立即切换」单次换 IP（循环运行时也可用，与循环互斥不冲突）
- 支持配置文件持久化（JSON）
- 需要管理员权限运行
"""

import subprocess
import re
import sys
import os
import json
import time
import ctypes
import random
import threading
import tkinter as tk
from tkinter import ttk, messagebox


# ================= 默认值 =================
DEFAULT_DNS1 = "172.153.64.253"
DEFAULT_DNS2 = ""
DEFAULT_GATEWAY = "172.153.44.1"

def get_app_dir():
    """返回程序所在目录：
    - 源码运行：脚本所在目录
    - PyInstaller 打包：exe 所在目录（而非 _MEIPASS 临时目录）
    """
    if getattr(sys, "frozen", False):
        # 打包后：sys.executable 就是 exe 的完整路径
        return os.path.dirname(os.path.abspath(sys.executable))
    # 源码运行：__file__ 是脚本路径
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(get_app_dir(), "ip_rotate_config.json")

DEFAULT_CONFIG = {
    "interface": "",
    "interval": "20",
    "min_last": "10",
    "max_last": "99",
    "exclude": "69",
    "prefix": "",
    "mask": "",
    "gateway": DEFAULT_GATEWAY,
    "dns1": DEFAULT_DNS1,
    "dns2": DEFAULT_DNS2,
    "auto_fill": True,
    "restore_on_exit": True,
}
# ==========================================


# ================= 配置文件 =================

def load_config():
    """读取配置文件；不存在或损坏时返回默认配置副本"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in DEFAULT_CONFIG:
                    if k in data:
                        cfg[k] = data[k]
    except Exception as e:
        print(f"[!] 读取配置失败，使用默认值: {e}")
    return cfg


def save_config(cfg):
    """保存配置到 JSON 文件"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True, ""
    except Exception as e:
        return False, str(e)


# ================= 底层功能 =================

def run_cmd(cmd):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            encoding="gbk", errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        return subprocess.CompletedProcess(cmd, -1, "", str(e))


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def get_interfaces():
    out = run_cmd("netsh interface show interface").stdout
    result = []
    for line in out.splitlines():
        if any(k in line for k in ("已连接", "Connected")):
            parts = re.split(r"\s{2,}", line.strip())
            if parts:
                result.append(parts[-1])
    return result


def get_interface_config(interface):
    out = run_cmd(f'netsh interface ip show config name="{interface}"').stdout
    info = {"ip": "-", "mask": "-", "gateway": "-", "dns": [], "dhcp": False}
    for line in out.splitlines():
        line = line.strip()
        if ("DHCP 已启用" in line or "DHCP enabled" in line.lower()):
            if "是" in line or "yes" in line.lower():
                info["dhcp"] = True
        if re.search(r"IP 地址|IP Address", line) and ":" in line:
            val = line.split(":", 1)[1].strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+", val) and info["ip"] == "-":
                info["ip"] = val
        if ("子网掩码" in line or "Subnet" in line) and ":" in line:
            val = line.split(":", 1)[1].strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+", val):
                info["mask"] = val
        if ("默认网关" in line or "Default Gateway" in line) and ":" in line:
            val = line.split(":", 1)[1].strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+", val):
                info["gateway"] = val
        if ("DNS 服务器" in line or "DNS Servers" in line) and ":" in line:
            val = line.split(":", 1)[1].strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+", val):
                info["dns"].append(val)
        elif re.match(r"^\d+\.\d+\.\d+\.\d+$", line) and info["dns"]:
            info["dns"].append(line)
    return info


def set_ip(iface, ip, mask, gateway, dns1=None, dns2=None):
    cmd = f'netsh interface ip set address name="{iface}" source=static addr={ip} mask={mask}'
    cmd += f" gateway={gateway} gwmetric=1" if gateway else " gateway=none"
    r = run_cmd(cmd)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    if dns1:
        run_cmd(f'netsh interface ip set dns name="{iface}" source=static addr={dns1} register=primary')
    if dns2:
        run_cmd(f'netsh interface ip add dns name="{iface}" addr={dns2} index=2')
    return True, ""


def restore_config(iface, snapshot):
    if not snapshot:
        return False, "无快照"

    if snapshot.get("dhcp"):
        r = run_cmd(f'netsh interface ip set address name="{iface}" source=dhcp')
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
        run_cmd(f'netsh interface ip set dns name="{iface}" source=dhcp')
        return True, "已恢复为 DHCP"

    ip = snapshot.get("ip")
    mask = snapshot.get("mask")
    gateway = snapshot.get("gateway")
    dns_list = snapshot.get("dns") or []

    if not ip or ip == "-" or not mask or mask == "-":
        return False, "快照缺少 IP/掩码，无法恢复"

    gw = gateway if gateway and gateway != "-" else None
    dns1 = dns_list[0] if len(dns_list) > 0 else None
    dns2 = dns_list[1] if len(dns_list) > 1 else None
    return set_ip(iface, ip, mask, gw, dns1, dns2)


# ================= GUI =================

class RotateIPApp:
    def __init__(self, root):
        self.root = root
        self.root.title("IP 自动轮换工具")
        self.root.geometry("700x820")
        self.root.minsize(620, 740)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.running = False
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.current_last = None
        self.snapshot = None
        self.snapshot_iface = None

        # 读取配置
        self.config = load_config()

        self.restore_on_exit = tk.BooleanVar(
            value=bool(self.config.get("restore_on_exit", True)))
        self.auto_fill = tk.BooleanVar(
            value=bool(self.config.get("auto_fill", True)))
        self._autofill_cache = {}

        # 所有"设置 IP"的操作共用同一把锁，避免循环 & 立即切换同时操作
        self._switch_lock = threading.Lock()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.load_interfaces)

    # ---------- 构建界面 ----------
    def _build_ui(self):
        tk.Label(self.root, text="IP 自动轮换工具",
                 font=("Microsoft YaHei", 16, "bold"),
                 fg="#2c3e50").pack(pady=(12, 2))

        if is_admin():
            admin_txt, admin_color = "✅ 管理员权限", "#27ae60"
        else:
            admin_txt, admin_color = "⚠️ 未获取管理员权限，修改 IP 将失败", "#e67e22"
        tk.Label(self.root, text=admin_txt, fg=admin_color,
                 font=("Microsoft YaHei", 9)).pack(pady=(0, 6))

        # ===== 网络接口 =====
        frame_iface = ttk.LabelFrame(self.root, text=" 网络接口 ", padding=8)
        frame_iface.pack(fill="x", padx=12, pady=5)
        row = ttk.Frame(frame_iface)
        row.pack(fill="x")
        ttk.Label(row, text="接口:").pack(side="left")
        self.iface_var = tk.StringVar(value=self.config.get("interface", ""))
        self.iface_combo = ttk.Combobox(row, textvariable=self.iface_var,
                                        state="readonly", width=24)
        self.iface_combo.pack(side="left", padx=6)
        self.iface_combo.bind("<<ComboboxSelected>>",
                              lambda e: self.on_iface_changed())
        ttk.Button(row, text="🔄 刷新接口", width=11,
                   command=self.load_interfaces).pack(side="left", padx=2)
        ttk.Button(row, text="📥 读取当前配置", width=14,
                   command=self.fill_from_current).pack(side="left", padx=2)

        # ===== 轮换参数 =====
        frame_param = ttk.LabelFrame(self.root, text=" 轮换参数 ", padding=8)
        frame_param.pack(fill="x", padx=12, pady=5)

        self.interval_var = tk.StringVar(value=self.config.get("interval", "20"))
        self.min_var = tk.StringVar(value=self.config.get("min_last", "10"))
        self.max_var = tk.StringVar(value=self.config.get("max_last", "99"))
        self.exclude_var = tk.StringVar(value=self.config.get("exclude", "69"))
        self.prefix_var = tk.StringVar(value=self.config.get("prefix", ""))
        self.mask_var = tk.StringVar(value=self.config.get("mask", ""))
        self.gw_var = tk.StringVar(value=self.config.get("gateway", DEFAULT_GATEWAY))
        self.dns1_var = tk.StringVar(value=self.config.get("dns1", DEFAULT_DNS1))
        self.dns2_var = tk.StringVar(value=self.config.get("dns2", DEFAULT_DNS2))

        fields = [
            ("间隔(秒):", self.interval_var),
            ("最小末位:", self.min_var),
            ("最大末位:", self.max_var),
            ("排除(逗号):", self.exclude_var),
            ("IP前缀:", self.prefix_var),
            ("子网掩码:", self.mask_var),
            ("默认网关:", self.gw_var),
            ("首选DNS:", self.dns1_var),
            ("备用DNS:", self.dns2_var),
        ]
        grid = ttk.Frame(frame_param)
        grid.pack(fill="x")
        for i, (label, var) in enumerate(fields):
            r, c = divmod(i, 3)
            cell = ttk.Frame(grid)
            cell.grid(row=r, column=c, padx=4, pady=3, sticky="w")
            ttk.Label(cell, text=label, width=9, anchor="e").pack(side="left")
            ttk.Entry(cell, textvariable=var, width=15).pack(side="left")

        ttk.Label(frame_param,
                  text="提示：勾选下方『自动读取并填入当前配置』后，切换网卡时会自动刷新下列字段。",
                  foreground="#7f8c8d", font=("Microsoft YaHei", 8)).pack(anchor="w", pady=(4, 0))

        # ===== 选项 =====
        frame_options = ttk.LabelFrame(self.root, text=" 选项 ", padding=8)
        frame_options.pack(fill="x", padx=12, pady=5)

        ttk.Checkbutton(frame_options,
                        text="自动读取并填入当前网卡的 IP/掩码/网关/DNS",
                        variable=self.auto_fill).pack(anchor="w")
        ttk.Checkbutton(frame_options,
                        text="停止/关闭窗口时，自动恢复开始前的原始网络配置",
                        variable=self.restore_on_exit).pack(anchor="w")

        self.snapshot_var = tk.StringVar(value="快照：尚未保存")
        tk.Label(frame_options, textvariable=self.snapshot_var,
                 font=("Consolas", 8), fg="#34495e", anchor="w",
                 justify="left").pack(fill="x", pady=(2, 0))

        # ===== 当前状态 =====
        frame_status = ttk.LabelFrame(self.root, text=" 当前状态 ", padding=8)
        frame_status.pack(fill="x", padx=12, pady=5)

        self.status_var = tk.StringVar(value="未启动")
        tk.Label(frame_status, textvariable=self.status_var,
                 font=("Consolas", 10), anchor="w", justify="left",
                 fg="#2c3e50").pack(fill="x")

        # ===== 控制按钮 =====
        frame_btn = ttk.Frame(self.root)
        frame_btn.pack(fill="x", padx=12, pady=8)

        self.btn_start = ttk.Button(frame_btn, text="▶ 开始轮换",
                                    command=self.start, width=13)
        self.btn_start.pack(side="left", padx=3)

        self.btn_once = ttk.Button(frame_btn, text="⚡ 立即切换一次",
                                   command=self.switch_once, width=13)
        self.btn_once.pack(side="left", padx=3)

        self.btn_stop = ttk.Button(frame_btn, text="■ 停止并恢复",
                                   command=self.stop, width=13, state="disabled")
        self.btn_stop.pack(side="left", padx=3)

        ttk.Button(frame_btn, text="💾 保存配置",
                   command=self.on_save_config, width=11).pack(side="left", padx=3)

        # ===== 日志 =====
        frame_log = ttk.LabelFrame(self.root, text=" 切换日志 ", padding=6)
        frame_log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log_text = tk.Text(frame_log, font=("Consolas", 9),
                                state="disabled", bg="#1e1e1e", fg="#dcdcdc",
                                insertbackground="white")
        scroll = ttk.Scrollbar(frame_log, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ---------- 日志 & 状态 ----------
    def log(self, msg):
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text):
        self.status_var.set(text)

    def update_snapshot_label(self):
        if not self.snapshot:
            self.snapshot_var.set("快照：尚未保存")
            return
        s = self.snapshot
        if s.get("dhcp"):
            self.snapshot_var.set(f"快照：{self.snapshot_iface}  DHCP 自动获取")
        else:
            dns = ", ".join(s.get("dns") or []) or "-"
            self.snapshot_var.set(
                f"快照：{self.snapshot_iface}  IP={s.get('ip')}  "
                f"掩码={s.get('mask')}  网关={s.get('gateway')}  DNS={dns}"
            )

    # ---------- 配置文件 ----------
    def collect_config(self):
        """把界面当前值收集为配置字典"""
        return {
            "interface": self.iface_var.get(),
            "interval": self.interval_var.get(),
            "min_last": self.min_var.get(),
            "max_last": self.max_var.get(),
            "exclude": self.exclude_var.get(),
            "prefix": self.prefix_var.get(),
            "mask": self.mask_var.get(),
            "gateway": self.gw_var.get(),
            "dns1": self.dns1_var.get(),
            "dns2": self.dns2_var.get(),
            "auto_fill": bool(self.auto_fill.get()),
            "restore_on_exit": bool(self.restore_on_exit.get()),
        }

    def on_save_config(self):
        cfg = self.collect_config()
        ok, err = save_config(cfg)
        if ok:
            self.log(f"[*] 配置已保存到 {CONFIG_FILE}")
        else:
            self.log(f"[!] 保存配置失败：{err}")
            messagebox.showerror("保存失败", err)

    def autosave_config(self):
        """静默保存（用于关键操作前后）"""
        ok, err = save_config(self.collect_config())
        if not ok:
            print(f"[!] 自动保存配置失败: {err}")

    # ---------- 自动读取当前配置 ----------
    def fill_from_current(self, silent=False):
        iface = self.iface_var.get()
        if not iface:
            if not silent:
                self.log("[!] 请先选择网络接口")
            return None

        info = get_interface_config(iface)

        prefix = ""
        if info["ip"] != "-":
            m = re.match(r"^(\d+\.\d+\.\d+\.)", info["ip"])
            if m:
                prefix = m.group(1)

        mask = info["mask"] if info["mask"] != "-" else ""
        gateway = info["gateway"] if info["gateway"] != "-" else ""
        dns_list = info["dns"]

        updates = {
            "IP前缀:": (self.prefix_var, prefix),
            "子网掩码:": (self.mask_var, mask),
            "默认网关:": (self.gw_var, gateway),
        }

        def can_set(key, var, new_val):
            if not new_val:
                return False
            old_val = var.get().strip()
            last_auto = self._autofill_cache.get(key)
            if old_val and old_val != last_auto:
                return False
            return True

        for key, (var, val) in updates.items():
            if can_set(key, var, val):
                var.set(val)
                self._autofill_cache[key] = val

        if dns_list:
            dns1_val = dns_list[0]
            if can_set("首选DNS:", self.dns1_var, dns1_val):
                self.dns1_var.set(dns1_val)
                self._autofill_cache["首选DNS:"] = dns1_val
            if len(dns_list) > 1:
                dns2_val = dns_list[1]
                if can_set("备用DNS:", self.dns2_var, dns2_val):
                    self.dns2_var.set(dns2_val)
                    self._autofill_cache["备用DNS:"] = dns2_val

        if not silent:
            self.log(f"[*] 已读取 {iface} 当前配置：IP={info['ip']} "
                     f"掩码={info['mask']} 网关={info['gateway']} "
                     f"DNS={', '.join(dns_list) or '-'}")
        return info

    def on_iface_changed(self):
        self.refresh_status()
        if self.auto_fill.get():
            self.fill_from_current(silent=True)

    # ---------- 接口加载 ----------
    def load_interfaces(self):
        ifaces = get_interfaces()
        self.iface_combo["values"] = ifaces
        if ifaces:
            saved_iface = self.config.get("interface", "")
            current = self.iface_var.get()
            if current in ifaces:
                pass
            elif saved_iface and saved_iface in ifaces:
                self.iface_var.set(saved_iface)
            else:
                self.iface_var.set(ifaces[0])
            self.refresh_status()
            if self.auto_fill.get():
                self.fill_from_current(silent=True)
        else:
            self.iface_var.set("")
            self.set_status("未检测到已连接的网络接口")
            self.log("[!] 未检测到已连接的网络接口")

    def refresh_status(self):
        iface = self.iface_var.get()
        if not iface:
            return
        info = get_interface_config(iface)
        dns = ", ".join(info["dns"]) if info["dns"] else "-"
        mode = "DHCP" if info["dhcp"] else "静态"
        self.set_status(
            f"接口: {iface}  [{mode}]\n"
            f"IP: {info['ip']}    掩码: {info['mask']}    网关: {info['gateway']}\n"
            f"DNS: {dns}"
        )

    # ---------- 参数解析 ----------
    def parse_params(self):
        try:
            interval = float(self.interval_var.get())
            lo = int(self.min_var.get())
            hi = int(self.max_var.get())
        except ValueError:
            raise ValueError("间隔、最小/最大末位必须是数字")

        if interval <= 0:
            raise ValueError("间隔必须大于 0")
        if not (0 <= lo <= 255 and 0 <= hi <= 255):
            raise ValueError("末位范围必须在 0~255 之间")
        if lo > hi:
            raise ValueError("最小末位不能大于最大末位")

        excludes = set()
        for tok in self.exclude_var.get().split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                excludes.add(int(tok))
            except ValueError:
                raise ValueError(f"排除值不合法: {tok}")

        candidates = [n for n in range(lo, hi + 1) if n not in excludes]
        if len(candidates) < 2:
            raise ValueError("可用末位太少，请放宽范围或减少排除值")

        iface = self.iface_var.get()
        info = get_interface_config(iface) if iface else {"ip": "-", "mask": "-", "gateway": "-"}

        prefix = self.prefix_var.get().strip()
        if not prefix:
            if info["ip"] != "-":
                m = re.match(r"^(\d+\.\d+\.\d+\.)", info["ip"])
                if m:
                    prefix = m.group(1)
            if not prefix:
                raise ValueError("无法自动识别 IP 前缀，请手动填写（如 172.153.44.）")
        if not prefix.endswith("."):
            prefix += "."

        mask = self.mask_var.get().strip() or (info["mask"] if info["mask"] != "-" else "255.255.255.0")
        gateway = self.gw_var.get().strip() or None

        return {
            "interval": interval,
            "candidates": candidates,
            "prefix": prefix,
            "mask": mask,
            "gateway": gateway,
            "dns1": self.dns1_var.get().strip() or None,
            "dns2": self.dns2_var.get().strip() or None,
        }

    # ---------- 开始/停止 ----------
    def start(self):
        if self.running:
            return
        if not is_admin():
            if not messagebox.askyesno("权限警告",
                    "当前不是管理员权限，修改 IP 会失败。\n仍然继续吗？"):
                return
        if not self.iface_var.get():
            messagebox.showwarning("提示", "请先选择网络接口")
            return

        try:
            params = self.parse_params()
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return

        iface = self.iface_var.get()

        # 启动时保存当前配置
        self.autosave_config()

        self.snapshot = get_interface_config(iface)
        self.snapshot_iface = iface
        self.update_snapshot_label()
        if self.snapshot["ip"] == "-" and not self.snapshot["dhcp"]:
            self.log("[!] 未能读取原始 IP，自动恢复可能失败")
        else:
            self.log(f"[*] 已保存原始配置快照："
                     f"{'DHCP' if self.snapshot['dhcp'] else self.snapshot['ip']}")

        self.running = True
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.log(f"[*] 启动：间隔 {params['interval']}s，候选末位 "
                 f"{params['candidates'][0]}~{params['candidates'][-1]}"
                 f"（共 {len(params['candidates'])} 个），"
                 f"网关 {params['gateway'] or '-'}，DNS {params['dns1'] or '-'}")

        self.worker_thread = threading.Thread(
            target=self._worker, args=(params,), daemon=True)
        self.worker_thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.stop_event.set()
        self.btn_stop.configure(state="disabled")
        self.log("[*] 正在停止并恢复原始配置...")

    # ---------- 后台循环 ----------
    def _worker(self, params):
        interval = params["interval"]
        candidates = params["candidates"]
        prefix = params["prefix"]
        mask = params["mask"]
        gateway = params["gateway"]
        dns1, dns2 = params["dns1"], params["dns2"]
        iface = self.iface_var.get()

        while self.running:
            # 用锁保护写入，让"立即切换"与循环不会真正并行
            with self._switch_lock:
                if not self.running:
                    break
                last_set = self.current_last
                pool = [n for n in candidates if n != last_set] or candidates
                next_last = random.choice(pool)
                ip = f"{prefix}{next_last}"
                ok, err = set_ip(iface, ip, mask, gateway, dns1, dns2)
                if ok:
                    self.current_last = next_last
                    self.root.after(0, self.log, f"[+] 已切换 -> {ip}")
                    self.root.after(0, self.refresh_status)
                else:
                    self.root.after(0, self.log, f"[!] 切换失败 {ip}: {err}")

            if self.stop_event.wait(interval):
                break

        self.running = False
        self.root.after(0, self._do_restore_then_stop)

    # ---------- 立即切换一次 ----------
    def switch_once(self):
        if not is_admin():
            if not messagebox.askyesno("权限警告",
                    "当前不是管理员权限，修改 IP 会失败。\n仍然继续吗？"):
                return
        if not self.iface_var.get():
            messagebox.showwarning("提示", "请先选择网络接口")
            return

        try:
            params = self.parse_params()
        except ValueError as e:
            messagebox.showerror("参数错误", str(e))
            return

        iface = self.iface_var.get()

        # 保存当前配置
        self.autosave_config()

        # 如果当前没有快照（未启动循环），先保存快照，以便停止/关闭时能恢复
        if self.snapshot is None or self.snapshot_iface != iface:
            self.snapshot = get_interface_config(iface)
            self.snapshot_iface = iface
            self.update_snapshot_label()
            self.log(f"[*] 立即切换前已保存原始配置快照："
                     f"{'DHCP' if self.snapshot['dhcp'] else self.snapshot['ip']}")

        self.btn_once.configure(state="disabled")
        threading.Thread(target=self._do_switch_once, args=(params, iface), daemon=True).start()

    def _do_switch_once(self, params, iface):
        candidates = params["candidates"]
        prefix = params["prefix"]
        mask = params["mask"]
        gateway = params["gateway"]
        dns1, dns2 = params["dns1"], params["dns2"]

        with self._switch_lock:
            last_set = self.current_last
            pool = [n for n in candidates if n != last_set] or candidates
            next_last = random.choice(pool)
            ip = f"{prefix}{next_last}"
            ok, err = set_ip(iface, ip, mask, gateway, dns1, dns2)
            if ok:
                self.current_last = next_last
                self.root.after(0, self.log, f"[+] 立即切换 -> {ip}")
                self.root.after(0, self.refresh_status)
            else:
                self.root.after(0, self.log, f"[!] 立即切换失败 {ip}: {err}")
            self.root.after(0, lambda: self.btn_once.configure(state="normal"))

    def _do_restore_then_stop(self):
        if self.restore_on_exit.get() and self.snapshot and self.snapshot_iface:
            ok, msg = restore_config(self.snapshot_iface, self.snapshot)
            if ok:
                self.log(f"[+] 已恢复原始配置：{msg}")
            else:
                self.log(f"[!] 恢复失败：{msg}")
            self.snapshot = None
            self.snapshot_iface = None
            self.update_snapshot_label()
            self.refresh_status()
            if self.auto_fill.get():
                self.fill_from_current(silent=True)

        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.set_status("已停止")
        self.log("[*] 已停止轮换")

    # ---------- 关窗处理 ----------
    def on_close(self):
        if self.running:
            if not messagebox.askyesno("确认退出",
                    "轮换正在进行中。\n是否停止并恢复原始配置后退出？"):
                return
            self.running = False
            self.stop_event.set()
            if self.worker_thread and self.worker_thread.is_alive():
                self.worker_thread.join(timeout=5)
        if self.restore_on_exit.get() and self.snapshot and self.snapshot_iface:
            ok, msg = restore_config(self.snapshot_iface, self.snapshot)
            print(f"[{'✓' if ok else '!'}] 恢复：{msg}")

        # 关窗前保存配置
        ok, err = save_config(self.collect_config())
        if not ok:
            print(f"[!] 保存配置失败: {err}")

        self.root.destroy()


def main():
    if not is_admin():
        try:
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, f'"{__file__}"', None, 1)
            sys.exit(0)
        except Exception:
            pass

    root = tk.Tk()
    RotateIPApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
