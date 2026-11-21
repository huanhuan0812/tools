import os
import sys
import json
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path

# ===== 隐藏命令行窗口的关键配置 =====
IS_WINDOWS = sys.platform == "win32"
# Windows 下 CREATE_NO_WINDOW 标志 (0x08000000)
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# subprocess 通用参数: 隐藏窗口 + 不弹控制台
def _sp_kwargs():
    kw = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT if False else subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,   # 防止子进程等待输入
    }
    if IS_WINDOWS:
        kw["creationflags"] = CREATE_NO_WINDOW
        # 部分场景还需 STARTUPINFO
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kw["startupinfo"] = si
    return kw


class YTDLP_M3U8DL_App:
    def __init__(self, root):
        self.root = root
        self.root.title("视频下载器 - yt-dlp + N_m3u8DL-RE")
        self.root.geometry("900x650")
        
        self.video_info = None
        self.download_process = None
        self.is_downloading = False
        
        self.ytdlp_path = "yt-dlp"
        self.nm3u8dl_path = "N_m3u8DL-RE"
        
        self._build_ui()
    
    def _build_ui(self):
        top_frame = ttk.LabelFrame(self.root, text="视频地址", padding=8)
        top_frame.pack(fill="x", padx=10, pady=5)
        
        self.url_var = tk.StringVar()
        ttk.Entry(top_frame, textvariable=self.url_var).pack(
            side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(top_frame, text="解析", command=self.parse_url).pack(side="left", padx=2)
        ttk.Button(top_frame, text="粘贴", command=self.paste_url).pack(side="left", padx=2)
        
        mid_frame = ttk.Frame(self.root)
        mid_frame.pack(fill="both", expand=False, padx=10, pady=5)
        
        info_frame = ttk.LabelFrame(mid_frame, text="视频信息", padding=8)
        info_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
        
        self.info_text = scrolledtext.ScrolledText(info_frame, height=10, wrap="word")
        self.info_text.pack(fill="both", expand=True)
        
        opt_frame = ttk.LabelFrame(mid_frame, text="下载选项", padding=8)
        opt_frame.pack(side="left", fill="both", padx=(5, 0))
        
        ttk.Label(opt_frame, text="保存目录:").grid(row=0, column=0, sticky="w", pady=2)
        self.save_dir_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        ttk.Entry(opt_frame, textvariable=self.save_dir_var, width=25).grid(
            row=1, column=0, sticky="we", pady=2)
        ttk.Button(opt_frame, text="选择...", command=self.choose_dir).grid(
            row=1, column=1, padx=2)
        
        ttk.Label(opt_frame, text="文件名:").grid(row=2, column=0, sticky="w", pady=2)
        self.filename_var = tk.StringVar()
        ttk.Entry(opt_frame, textvariable=self.filename_var, width=25).grid(
            row=3, column=0, sticky="we", pady=2)
        
        self.resume_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="启用断点续传 (保留分片临时目录)",
                        variable=self.resume_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=5)
        
        ttk.Label(opt_frame, text="下载线程数:").grid(row=5, column=0, sticky="w", pady=2)
        self.threads_var = tk.IntVar(value=16)
        ttk.Spinbox(opt_frame, from_=1, to=64, textvariable=self.threads_var,
                    width=10).grid(row=5, column=1, sticky="w", pady=2)
        
        ttk.Label(opt_frame, text="清晰度:").grid(row=6, column=0, sticky="w", pady=2)
        self.quality_var = tk.StringVar(value="best")
        ttk.Combobox(opt_frame, textvariable=self.quality_var,
                     values=["best", "1080p", "720p", "480p", "audio only"],
                     state="readonly", width=15).grid(row=6, column=1, sticky="w", pady=2)
        
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=5)
        
        self.download_btn = ttk.Button(btn_frame, text="开始下载",
                                        command=self.start_download, state="disabled")
        self.download_btn.pack(side="left", padx=2)
        
        self.stop_btn = ttk.Button(btn_frame, text="停止",
                                    command=self.stop_download, state="disabled")
        self.stop_btn.pack(side="left", padx=2)
        
        self.open_dir_btn = ttk.Button(btn_frame, text="打开目录",
                                        command=self.open_save_dir)
        self.open_dir_btn.pack(side="left", padx=2)
        
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=2)
        
        log_frame = ttk.LabelFrame(self.root, text="日志", padding=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        
        self._load_settings()
    
    # ============ 工具方法 ============
    def log(self, msg):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
    
    def paste_url(self):
        try:
            self.url_var.set(self.root.clipboard_get().strip())
        except Exception:
            pass
    
    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.save_dir_var.get())
        if d:
            self.save_dir_var.set(d)
    
    def open_save_dir(self):
        d = self.save_dir_var.get()
        if os.path.isdir(d):
            kw = {}
            if IS_WINDOWS:
                kw["creationflags"] = CREATE_NO_WINDOW
            if sys.platform == "win32":
                os.startfile(d)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d], **kw)
            else:
                subprocess.Popen(["xdg-open", d], **kw)
    
    # ============ 配置文件路径 ============
    def _app_dir(self):
        """返回程序所在目录（打包后为 exe 所在目录，而非临时解压目录）"""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def _config_candidates(self):
        """配置文件候选路径列表：优先 exe 同级目录，其次用户 AppData 目录"""
        candidates = [self._app_dir() / "config.json"]
        appdata = os.getenv("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "VideoDownloader" / "config.json")
        # 非 Windows 的兜底：用户主目录下的隐藏配置
        candidates.append(Path.home() / ".video_downloader" / "config.json")
        return candidates

    def _load_settings(self):
        for cfg in self._config_candidates():
            if cfg.exists():
                try:
                    data = json.loads(cfg.read_text(encoding="utf-8"))
                    self.ytdlp_path = data.get("ytdlp", self.ytdlp_path)
                    self.nm3u8dl_path = data.get("nm3u8dl", self.nm3u8dl_path)
                    if data.get("save_dir"):
                        self.save_dir_var.set(data["save_dir"])
                    self.log(f"已加载配置: {cfg}")
                    return
                except Exception as e:
                    self.log(f"读取配置失败 {cfg}: {e}")
                    continue

    def _save_settings(self):
        payload = json.dumps({
            "ytdlp": self.ytdlp_path,
            "nm3u8dl": self.nm3u8dl_path,
            "save_dir": self.save_dir_var.get(),
        }, ensure_ascii=False, indent=2)

        # 依次尝试写入候选位置，成功即止
        for cfg in self._config_candidates():
            try:
                cfg.parent.mkdir(parents=True, exist_ok=True)
                cfg.write_text(payload, encoding="utf-8")
                return
            except Exception as e:
                self.log(f"写入配置失败 {cfg}: {e}")
                continue
    
    # ============ 解析 ============
    def parse_url(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入视频地址")
            return
        
        self.info_text.delete("1.0", "end")
        self.log(f"正在解析: {url}")
        self.progress.start(10)
        
        def worker():
            try:
                cmd = [self.ytdlp_path, "-J", "--no-warnings", url]
                # 隐藏窗口 + 捕获输出
                result = subprocess.run(cmd, **_sp_kwargs())
                if result.returncode != 0:
                    raise RuntimeError(result.stderr or "yt-dlp 解析失败")
                data = json.loads(result.stdout)
                self.video_info = data
                self.root.after(0, self._show_info, data)
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: messagebox.showerror("解析失败", err))
                self.root.after(0, lambda: self.log(f"解析错误: {err}"))
            finally:
                self.root.after(0, self.progress.stop)
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _show_info(self, data):
        title = data.get("title", "未知标题")
        duration = data.get("duration", 0) or 0
        uploader = data.get("uploader", "未知")
        webpage = data.get("webpage_url", "")
        
        info = (
            f"标题: {title}\n"
            f"作者: {uploader}\n"
            f"时长: {self._fmt_duration(duration)}\n"
            f"来源: {webpage}\n\n"
        )
        
        formats = data.get("formats", [])
        if formats:
            info += "可用格式:\n"
            shown = 0
            for f in reversed(formats):
                vcodec = f.get("vcodec", "none")
                acodec = f.get("acodec", "none")
                if vcodec == "none" and acodec == "none":
                    continue
                height = f.get("height")
                ext = f.get("ext", "?")
                fid = f.get("format_id", "?")
                tbr = f.get("tbr")
                size = f.get("filesize") or f.get("filesize_approx")
                size_str = f"{size/1024/1024:.1f}MB" if size else "?"
                if height:
                    info += f"  [{fid}] {height}p {ext} {tbr and f'{tbr:.0f}k' or ''} {size_str}\n"
                else:
                    info += f"  [{fid}] 音频 {ext} {size_str}\n"
                shown += 1
                if shown >= 40:
                    info += "  ...\n"
                    break
        
        self.info_text.delete("1.0", "end")
        self.info_text.insert("1.0", info)
        
        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|')[:100]
        self.filename_var.set(safe_title or "video")
        
        self.download_btn.config(state="normal")
        self.log("解析成功 ✓")
    
    @staticmethod
    def _fmt_duration(sec):
        sec = int(sec)
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    
    # ============ 下载 ============
    def start_download(self):
        if not self.video_info:
            messagebox.showwarning("提示", "请先解析视频")
            return
        
        url = self.url_var.get().strip()
        save_dir = self.save_dir_var.get().strip()
        filename = self.filename_var.get().strip() or "video"
        threads = self.threads_var.get()
        quality = self.quality_var.get()
        
        os.makedirs(save_dir, exist_ok=True)
        
        self.is_downloading = True
        self.download_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.progress.config(mode="indeterminate")
        self.progress.start(10)
        
        def worker():
            try:
                self.log("第一步: 解析视频真实地址 (yt-dlp -g)...")
                fmt_expr = self._quality_to_format(quality)
                
                cmd = [self.ytdlp_path, "-g", "-f", fmt_expr, url]
                result = subprocess.run(cmd, **_sp_kwargs())
                if result.returncode != 0:
                    raise RuntimeError(f"获取直链失败:\n{result.stderr}")
                
                urls = [u.strip() for u in result.stdout.strip().splitlines() if u.strip()]
                if not urls:
                    raise RuntimeError("未获取到可下载地址")
                
                direct_url = urls[0]
                self.log(f"获取到地址: {direct_url[:120]}...")
                
                if any(x in direct_url for x in (".m3u8", ".mpd", "m3u8", "mpd")):
                    self._download_with_nm3u8dl(direct_url, save_dir, filename, threads, url)
                else:
                    self.log("检测到直链,使用 yt-dlp 下载...")
                    self._download_direct(url, save_dir, filename, fmt_expr)
                
                if self.is_downloading:
                    self.log("✓ 下载完成")
                    self.root.after(0, lambda: messagebox.showinfo("完成", "下载完成!"))
            except Exception as e:
                err = str(e)
                self.log(f"✗ 下载失败: {err}")
                if self.is_downloading:
                    self.root.after(0, lambda: messagebox.showerror("下载失败", err))
            finally:
                self.is_downloading = False
                self.root.after(0, self._reset_ui)
        
        threading.Thread(target=worker, daemon=True).start()
    
    def _quality_to_format(self, quality):
        return {
            "best": "bv*+ba/b",
            "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
            "720p": "bv*[height<=720]+ba/b[height<=720]",
            "480p": "bv*[height<=480]+ba/b[height<=480]",
            "audio only": "ba/b",
        }.get(quality, "bv*+ba/b")
    
    def _download_with_nm3u8dl(self, m3u8_url, save_dir, filename, threads, original_url):
        """使用 N_m3u8DL-RE 下载 m3u8/mpd,支持断点续传"""
        self.log("第二步: 使用 N_m3u8DL-RE 下载...")
        
        save_name = filename
        cmd = [
            self.nm3u8dl_path,
            m3u8_url,
            "--save-dir", save_dir,
            "--save-name", save_name,
            "--thread-count", str(threads),
            "--auto-select",
            "--check-segments-count",
            "--tmp-dir", os.path.join(save_dir, ".tmp_" + save_name),
            "--no-log",
            "--del-after-done",
        ]
        
        if not self.resume_var.get():
            cmd += ["--no-date-info"]
        
        cmd += ["-H", f"Referer: {original_url}"]
        cmd += ["-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"]
        
        self.log("命令: " + " ".join(cmd))
        
        # 隐藏窗口 + 流式读取
        kw = _sp_kwargs()
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.STDOUT
        kw["bufsize"] = 1
        
        self.download_process = subprocess.Popen(cmd, **kw)
        
        for line in self.download_process.stdout:
            if not self.is_downloading:
                break
            line = line.rstrip()
            if line:
                self.root.after(0, lambda l=line: self.log(l))
        
        ret = self.download_process.wait()
        self.download_process = None
        if ret != 0 and self.is_downloading:
            raise RuntimeError(f"N_m3u8DL-RE 退出码: {ret}")
    
    def _download_direct(self, url, save_dir, filename, fmt_expr):
        """普通直链使用 yt-dlp 下载(自带断点续传 --continue)"""
        out_tpl = os.path.join(save_dir, filename + ".%(ext)s")
        cmd = [
            self.ytdlp_path,
            "-f", fmt_expr,
            "-o", out_tpl,
            "--continue",
            "--no-part",
            "--newline",
            url,
        ]
        self.log("命令: " + " ".join(cmd))
        
        kw = _sp_kwargs()
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.STDOUT
        kw["bufsize"] = 1
        
        self.download_process = subprocess.Popen(cmd, **kw)
        
        for line in self.download_process.stdout:
            if not self.is_downloading:
                break
            line = line.rstrip()
            if line:
                self.root.after(0, lambda l=line: self.log(l))
        
        ret = self.download_process.wait()
        self.download_process = None
        if ret != 0 and self.is_downloading:
            raise RuntimeError(f"yt-dlp 退出码: {ret}")
    
    def stop_download(self):
        self.is_downloading = False
        if self.download_process:
            try:
                self.download_process.terminate()
                self.log("已发送停止信号 (分片进度已保存,可重新开始续传)")
            except Exception:
                pass
        self.stop_btn.config(state="disabled")
    
    def _reset_ui(self):
        self.progress.stop()
        self.progress.config(mode="indeterminate")
        self.download_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._save_settings()


if __name__ == "__main__":
    root = tk.Tk()
    app = YTDLP_M3U8DL_App(root)
    root.mainloop()
