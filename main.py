import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import subprocess
import threading
import configparser
import sys
from concurrent.futures import ThreadPoolExecutor

# 配置文件名
CONFIG_FILE = "config.ini"

# --- 辅助类：滚动框架 ---
class ScrollableFrame(ttk.Frame):
    def __init__(self, container, *args, **kwargs):
        super().__init__(container, *args, **kwargs)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        
        # 鼠标滚轮支持
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")

# --- 核心类：单个插件行的UI逻辑 ---
class PluginRow:
    def __init__(self, parent_frame, app, folder_name):
        self.app = app
        self.folder_name = folder_name
        self.full_path = os.path.join(app.nodes_path, folder_name)
        
        # 状态标志
        self.is_update_available = False
        
        # UI 容器
        self.frame = tk.Frame(parent_frame, bd=1, relief=tk.RIDGE, bg="white")
        self.frame.pack(fill="x", pady=2, padx=5)
        
        # 1. 插件名称
        self.lbl_name = tk.Label(self.frame, text=folder_name, width=30, anchor="w", font=("Arial", 9, "bold"), bg="white")
        self.lbl_name.pack(side="left", padx=5)

        # 2. 状态标签
        self.lbl_status = tk.Label(self.frame, text="等待检查...", width=15, fg="gray", bg="white")
        self.lbl_status.pack(side="left", padx=5)

        # 3. 版本选择下拉框 (Combobox)
        self.var_version = tk.StringVar()
        self.combo_versions = ttk.Combobox(self.frame, textvariable=self.var_version, width=30, state="readonly")
        self.combo_versions.set("加载版本中...")
        self.combo_versions.pack(side="left", padx=5)

        # 4. 执行按钮
        self.btn_action = tk.Button(self.frame, text="执行操作", command=self.on_action_click, bg="#f0f0f0", state="disabled")
        self.btn_action.pack(side="right", padx=5)

        # 启动异步检查
        threading.Thread(target=self.init_data, daemon=True).start()

    def run_git(self, args):
        return self.app.run_git_cmd(self.full_path, args)

    def init_data(self):
        """ 初始化：获取状态和版本列表 """
        # 1. 获取基本状态 (是否需要更新)
        status_text, status_color = self.check_status()
        self.update_ui_status(status_text, status_color)

        # 2. 获取版本列表 (Tags 和 最近 Commits)
        versions = self.fetch_versions()
        
        def update_combo():
            self.combo_versions['values'] = versions
            if versions:
                self.combo_versions.current(0) # 默认选第一个（通常是最新）
            else:
                self.combo_versions.set("无版本记录")
            self.btn_action.config(state="normal")
        
        self.app.root.after(0, update_combo)

    def check_status(self):
        if not os.path.exists(os.path.join(self.full_path, ".git")):
            return "非Git仓库", "gray"
        
        # Fetch 更新
        self.run_git(["fetch"]) 
        
        code, out, _ = self.run_git(["status", "-uno"])
        if "behind" in out or "落后" in out:
            self.is_update_available = True
            return "检测到新版本", "red"
        elif "detached" in out:
             return "处于历史版本", "orange"
        
        self.is_update_available = False
        return "最新版本", "green"

    def fetch_versions(self):
        """ 获取 Git Tag 和 Commit 列表供用户选择 """
        versions = ["最新版本 (Latest)"]
        
        if not os.path.exists(os.path.join(self.full_path, ".git")):
            return []

        # 获取 Tags (最近5个)
        code, out, _ = self.run_git(["tag", "--sort=-creatordate"])
        if code == 0 and out:
            tags = out.split('\n')[:5]
            for t in tags:
                if t.strip(): versions.append(f"Tag: {t.strip()}")

        # 获取 Commits (最近10个)
        code, out, _ = self.run_git(["log", "--pretty=format:%h - %s", "-n", "10"])
        if code == 0 and out:
            commits = out.split('\n')
            for c in commits:
                if c.strip(): versions.append(f"Commit: {c.strip()}")
        
        return versions

    def update_ui_status(self, text, color):
        self.app.root.after(0, lambda: self.lbl_status.config(text=text, fg=color))

    def on_action_click(self):
        selection = self.var_version.get()
        if not selection: return

        if messagebox.askyesno("确认", f"对插件 {self.folder_name} 执行:\n{selection}?"):
            self.btn_action.config(state="disabled", text="执行中...")
            # 手动点击 silent=False
            threading.Thread(target=self.do_update, args=(selection, False), daemon=True).start()

    def do_update(self, selection, silent=False):
        """ 执行更新逻辑 
        :param silent: 是否静默模式（不弹成功窗，主要用于批量更新）
        """
        try:
            # 内部函数：检测冲突并尝试强制重置
            def try_force_reset(err_msg):
                keywords = ["overwritten by merge", "stash them", "local changes", "aborted"]
                if any(k in err_msg for k in keywords):
                    # 冲突时无论是否 silent 都需要弹窗询问，因为涉及数据丢失风险
                    if messagebox.askyesno("冲突解决", 
                        f"检测到插件 {self.folder_name} 有本地修改，导致更新失败。\n\nGit报错片段:\n{err_msg[:200]}...\n\n是否【丢弃本地修改】并强制更新？\n(警告：您的修改将无法恢复！)"):
                        
                        r_code, _, r_err = self.run_git(["reset", "--hard", "HEAD"])
                        if r_code == 0:
                            return True 
                        else:
                            messagebox.showerror("重置失败", f"无法自动修复，请手动删除该插件文件夹重新安装。\n\n错误: {r_err}")
                            return False
                return False

            if "最新版本" in selection:
                # 逻辑：切回主分支并 Pull
                code, out, _ = self.run_git(["remote", "show", "origin"])
                head_branch = "master" 
                if "HEAD branch" in out:
                    for line in out.splitlines():
                        if "HEAD branch" in line:
                            head_branch = line.split(":")[-1].strip()
                            break
                
                self.run_git(["checkout", head_branch])
                code, out, err = self.run_git(["pull"])
                
                # 冲突自动处理逻辑
                if code != 0:
                    if try_force_reset(err):
                        code, out, err = self.run_git(["pull"])

                if code == 0:
                    self.update_ui_status("更新成功", "green")
                    self.is_update_available = False # 更新后重置标志
                    if not silent:
                        messagebox.showinfo("成功", f"{self.folder_name} 已更新到最新。")
                else:
                    self.update_ui_status("更新失败", "red")
                    # 失败即使在 silent 模式下也可以考虑记录日志，这里暂保留弹窗或控制台输出
                    if not silent:
                        messagebox.showerror("失败", f"{self.folder_name} 更新失败:\n{err}")
                    else:
                        print(f"[Error] {self.folder_name} 更新失败: {err}")

            elif "Tag:" in selection or "Commit:" in selection:
                target = selection.replace("Tag: ", "").strip() if "Tag:" in selection else selection.split(" ")[1].strip()
                code, _, err = self.run_git(["checkout", target])
                
                if code != 0:
                    if try_force_reset(err):
                        code, _, err = self.run_git(["checkout", target])

                if code == 0:
                    self.update_ui_status(f"已回退: {target}", "orange")
                else:
                    messagebox.showerror("错误", f"切换失败: {err}")

        except Exception as e:
            messagebox.showerror("异常", str(e))
        finally:
            self.app.root.after(0, lambda: self.btn_action.config(state="normal", text="执行操作"))


# --- 主程序类 ---
class ComfyUpdaterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ComfyUI 插件管理器 (增强版)")
        self.root.geometry("1000x700")

        self.config = configparser.ConfigParser()
        self.git_exe = "git"
        self.nodes_path = ""
        self.proxy_url = "" # 新增：代理地址
        self.load_config()

        # 列表引用，用于批量操作
        self.plugin_rows = []

        # 1. 顶部控制栏
        top_frame = tk.Frame(root, pady=10)
        top_frame.pack(fill="x")
        
        tk.Button(top_frame, text="选择目录", command=self.select_directory).pack(side="left", padx=10)
        self.path_label = tk.Label(top_frame, text=self.nodes_path or "未选择", fg="blue")
        self.path_label.pack(side="left")
        
        # 右侧按钮组
        tk.Button(top_frame, text="刷新列表", command=self.refresh_list, bg="#dddddd").pack(side="right", padx=5)
        
        # 新增：一键更新按钮
        self.btn_update_all = tk.Button(top_frame, text="一键更新可更新项", command=self.update_all_plugins, bg="#c8e6c9", fg="black")
        self.btn_update_all.pack(side="right", padx=5)

        # 2. 列表区域
        self.list_container = ScrollableFrame(root)
        self.list_container.pack(fill="both", expand=True, padx=10, pady=5)

        # 3. 底部状态栏
        self.status_bar = tk.Label(root, text=f"就绪 (代理: {self.proxy_url if self.proxy_url else '无'})", bd=1, relief=tk.SUNKEN, anchor="w")
        self.status_bar.pack(side="bottom", fill="x")

        if self.nodes_path and os.path.exists(self.nodes_path):
            self.refresh_list()

    def load_config(self):
        if not os.path.exists(CONFIG_FILE): return
        try:
            self.config.read(CONFIG_FILE, encoding='utf-8')
            if 'Settings' in self.config:
                self.git_exe = self.config['Settings'].get('git_path', 'git').strip()
                p = self.config['Settings'].get('custom_nodes_path', '').strip()
                if p:
                    self.nodes_path = p if os.path.isabs(p) else os.path.abspath(os.path.join(os.getcwd(), p))
            
            # 读取代理设置
            if 'Network' in self.config:
                self.proxy_url = self.config['Network'].get('https_proxy', '').strip()
        except: pass

    def select_directory(self):
        path = filedialog.askdirectory(initialdir=self.nodes_path)
        if path:
            self.nodes_path = path
            self.path_label.config(text=path)
            self.refresh_list()

    def run_git_cmd(self, folder_path, args):
        try:
            cmd = [self.git_exe] + args
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            # 设置环境变量
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GCM_INTERACTIVE"] = "never"
            
            # 应用代理
            if self.proxy_url:
                env["http_proxy"] = self.proxy_url
                env["https_proxy"] = self.proxy_url

            result = subprocess.run(
                cmd, cwd=folder_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='ignore', 
                startupinfo=startupinfo, env=env, timeout=60 # 增加超时时间以适应代理延迟
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return -1, "", str(e)

    def refresh_list(self):
        # 清空旧列表
        for widget in self.list_container.scrollable_frame.winfo_children():
            widget.destroy()
        self.plugin_rows.clear()

        if not os.path.exists(self.nodes_path): return

        folders = [f for f in os.listdir(self.nodes_path) if os.path.isdir(os.path.join(self.nodes_path, f))]
        self.status_bar.config(text=f"发现 {len(folders)} 个插件 | 代理: {self.proxy_url if self.proxy_url else '无'}")

        # 创建行
        for folder in folders:
            row = PluginRow(self.list_container.scrollable_frame, self, folder)
            self.plugin_rows.append(row)

    def update_all_plugins(self):
        """ 批量更新逻辑 """
        targets = [row for row in self.plugin_rows if row.is_update_available]
        
        if not targets:
            messagebox.showinfo("提示", "当前没有检测到需要更新的插件。\n(请确保列表已刷新且显示为红色状态)")
            return

        if not messagebox.askyesno("批量更新", f"检测到 {len(targets)} 个插件有新版本。\n\n是否开始批量更新？"):
            return

        self.btn_update_all.config(state="disabled", text="正在更新...")
        
        def run_batch():
            # 使用线程池并发更新，最大5个并发，避免卡死或被封锁
            with ThreadPoolExecutor(max_workers=5) as executor:
                for row in targets:
                    # 这里的 UI 状态更新需要在 do_update 内部处理，或者这里不做处理
                    # 调用 silent=True 模式
                    row.btn_action.config(state="disabled", text="队列中...")
                    executor.submit(row.do_update, "最新版本 (Latest)", True)
            
            # 完成后恢复按钮
            self.root.after(0, lambda: self.btn_update_all.config(state="normal", text="一键更新可更新项"))
            self.root.after(0, lambda: messagebox.showinfo("完成", "批量更新流程已结束，请查看各插件状态。"))

        threading.Thread(target=run_batch, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = ComfyUpdaterApp(root)
    root.mainloop()