import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import subprocess
import threading
import configparser
from concurrent.futures import ThreadPoolExecutor

# 配置文件名
CONFIG_FILE = "config.ini"

# --- 辅助类：滚动框架 (保持不变) ---
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
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")

# --- 核心类：Git 操作及依赖管理基类 ---
class GitItemBase:
    def __init__(self, app, path, display_name):
        self.app = app
        self.full_path = path
        self.display_name = display_name
        self.is_update_available = False
        self.has_requirements = False

    def check_requirements(self):
        """检查是否存在 requirements.txt"""
        req_path = os.path.join(self.full_path, "requirements.txt")
        self.has_requirements = os.path.exists(req_path)
        return self.has_requirements

    def run_cmd_generic(self, cmd_args, cwd=None):
        """通用的命令行执行方法 (用于 git 和 pip)"""
        target_cwd = cwd if cwd else self.full_path
        return self.app.run_cmd(cmd_args, target_cwd)

    def run_git(self, args):
        cmd = [self.app.git_exe] + args
        return self.run_cmd_generic(cmd)
    
    def run_pip_install(self):
        """执行 pip install -r requirements.txt"""
        if not self.has_requirements:
            return False, "未找到 requirements.txt"
        
        # 使用配置中的 python 路径
        python_exe = self.app.python_exe
        if not python_exe:
            return False, "未配置 Python 路径"

        cmd = [python_exe, "-m", "pip", "install", "-r", "requirements.txt"]
        
        # pip 可能需要较长时间，这里返回的是 subprocess 的结果
        code, out, err = self.run_cmd_generic(cmd)
        if code == 0:
            return True, out
        else:
            return False, f"{err}\n{out}"

    def check_status_base(self):
        if not os.path.exists(os.path.join(self.full_path, ".git")):
            return "非Git仓库", "gray", False
        
        self.run_git(["fetch"]) 
        code, out, _ = self.run_git(["status", "-uno"])
        
        if "behind" in out or "落后" in out:
            return "检测到新版本", "red", True
        elif "detached" in out:
             return "处于历史版本", "orange", False
        
        return "最新版本", "green", False

    def fetch_versions_base(self):
        versions = ["最新版本 (Latest)"]
        if not os.path.exists(os.path.join(self.full_path, ".git")):
            return []
        
        # Tags
        code, out, _ = self.run_git(["tag", "--sort=-creatordate"])
        if code == 0 and out:
            tags = out.split('\n')[:8]
            for t in tags:
                if t.strip(): versions.append(f"Tag: {t.strip()}")

        # Commits
        code, out, _ = self.run_git(["log", "--pretty=format:%h - %s", "-n", "15"])
        if code == 0 and out:
            commits = out.split('\n')
            for c in commits:
                if c.strip(): versions.append(f"Commit: {c.strip()}")
        return versions

    def do_update_logic(self, selection, silent=False):
        try:
            def try_force_reset(err_msg):
                keywords = ["overwritten by merge", "stash them", "local changes", "aborted"]
                if any(k in err_msg for k in keywords):
                    if messagebox.askyesno("冲突解决", 
                        f"检测到 {self.display_name} 有本地修改导致更新失败。\n\n是否【丢弃本地修改】并强制更新？"):
                        r_code, _, r_err = self.run_git(["reset", "--hard", "HEAD"])
                        return r_code == 0
                return False

            if "最新版本" in selection:
                code, out, _ = self.run_git(["remote", "show", "origin"])
                head_branch = "master" 
                if "HEAD branch" in out:
                    for line in out.splitlines():
                        if "HEAD branch" in line:
                            head_branch = line.split(":")[-1].strip()
                            break
                
                self.run_git(["checkout", head_branch])
                code, out, err = self.run_git(["pull"])
                if code != 0:
                    if try_force_reset(err):
                        code, out, err = self.run_git(["pull"])

                if code == 0:
                    return True, "更新成功"
                else:
                    return False, f"更新失败: {err}"

            elif "Tag:" in selection or "Commit:" in selection:
                target = selection.replace("Tag: ", "").strip() if "Tag:" in selection else selection.split(" ")[1].strip()
                code, _, err = self.run_git(["checkout", target])
                if code != 0:
                    if try_force_reset(err):
                        code, _, err = self.run_git(["checkout", target])
                
                if code == 0:
                    return True, f"已回退: {target}"
                else:
                    return False, f"切换失败: {err}"
            return False, "未选择操作"

        except Exception as e:
            return False, str(e)

# --- 插件行UI (继承自 GitItemBase) ---
class PluginRow(GitItemBase):
    def __init__(self, parent_frame, app, folder_name):
        full_path = os.path.join(app.nodes_path, folder_name)
        super().__init__(app, full_path, folder_name)
        
        self.frame = tk.Frame(parent_frame, bd=1, relief=tk.RIDGE, bg="white")
        self.frame.pack(fill="x", pady=2, padx=5)
        
        # 1. 名字
        self.lbl_name = tk.Label(self.frame, text=folder_name, width=28, anchor="w", font=("Arial", 9, "bold"), bg="white")
        self.lbl_name.pack(side="left", padx=5)

        # 2. 状态
        self.lbl_status = tk.Label(self.frame, text="等待检查...", width=12, fg="gray", bg="white")
        self.lbl_status.pack(side="left", padx=5)

        # 3. 版本下拉
        self.var_version = tk.StringVar()
        self.combo_versions = ttk.Combobox(self.frame, textvariable=self.var_version, width=25, state="readonly")
        self.combo_versions.set("加载中...")
        self.combo_versions.pack(side="left", padx=5)

        # 4. 执行操作按钮
        self.btn_action = tk.Button(self.frame, text="执行操作", command=self.on_action_click, bg="#f0f0f0", state="disabled", width=8)
        self.btn_action.pack(side="left", padx=5)

        # 5. 依赖修复按钮 (新增)
        self.btn_pip = tk.Button(self.frame, text="安装依赖", command=self.on_pip_click, bg="#e3f2fd", state="disabled", width=8)
        self.btn_pip.pack(side="right", padx=5)

        threading.Thread(target=self.init_data, daemon=True).start()

    def init_data(self):
        text, color, is_update = self.check_status_base()
        self.is_update_available = is_update
        
        # 检查依赖文件
        has_req = self.check_requirements()

        versions = self.fetch_versions_base()

        def update_ui():
            self.lbl_status.config(text=text, fg=color)
            self._update_combo(versions)
            if has_req:
                self.btn_pip.config(state="normal")
            else:
                self.btn_pip.config(state="disabled", text="无依赖")

        self.app.root.after(0, update_ui)

    def _update_combo(self, versions):
        self.combo_versions['values'] = versions
        if versions: self.combo_versions.current(0)
        else: self.combo_versions.set("无版本记录")
        self.btn_action.config(state="normal")

    def on_action_click(self):
        selection = self.var_version.get()
        if not selection: return
        if messagebox.askyesno("确认", f"对插件 {self.display_name} 执行:\n{selection}?"):
            self.btn_action.config(state="disabled", text="执行中...")
            threading.Thread(target=self.do_update, args=(selection, False), daemon=True).start()

    def do_update(self, selection, silent=False):
        success, msg = self.do_update_logic(selection, silent)
        def post_ui():
            self.btn_action.config(state="normal", text="执行操作")
            if success:
                self.lbl_status.config(text="操作成功", fg="green")
                self.is_update_available = False
                if not silent: messagebox.showinfo("成功", f"{self.display_name}: {msg}")
            else:
                self.lbl_status.config(text="操作失败", fg="red")
                if not silent: messagebox.showerror("失败", f"{self.display_name}: {msg}")
        self.app.root.after(0, post_ui)
    
    def on_pip_click(self):
        if messagebox.askyesno("安装依赖", f"即将为 {self.display_name} 执行 pip install。\n请确保网络通畅（代理已配置）。\n\n继续吗？"):
            self.btn_pip.config(state="disabled", text="安装中...")
            threading.Thread(target=self.do_pip, daemon=True).start()

    def do_pip(self):
        success, msg = self.run_pip_install()
        def post_ui():
            self.btn_pip.config(state="normal", text="安装依赖")
            if success:
                messagebox.showinfo("Pip 安装成功", f"{self.display_name} 依赖安装完成。\n\n日志片段:\n{msg[-500:]}")
            else:
                messagebox.showerror("Pip 安装失败", f"{self.display_name} 依赖安装出错。\n\n错误信息:\n{msg}")
        self.app.root.after(0, post_ui)

# --- ComfyUI 本体管理 UI ---
class CoreManagerFrame(tk.Frame, GitItemBase):
    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent)
        self.app = app
        GitItemBase.__init__(self, app, "", "ComfyUI 本体")
        
        self.create_widgets()
    
    def create_widgets(self):
        tk.Label(self, text="ComfyUI 本体版本管理", font=("Arial", 14, "bold"), pady=10).pack()
        self.lbl_path = tk.Label(self, text="当前路径: 未设置", fg="gray")
        self.lbl_path.pack()

        # 状态显示
        status_frame = tk.LabelFrame(self, text="当前状态", padx=20, pady=20)
        status_frame.pack(fill="x", padx=20, pady=10)
        self.lbl_status_large = tk.Label(status_frame, text="未知", font=("Arial", 12))
        self.lbl_status_large.pack()
        self.lbl_commit_info = tk.Label(status_frame, text="", fg="#555")
        self.lbl_commit_info.pack(pady=5)

        # 操作区域
        action_frame = tk.LabelFrame(self, text="维护操作", padx=20, pady=20)
        action_frame.pack(fill="x", padx=20, pady=10)

        # 版本更新行
        row1 = tk.Frame(action_frame)
        row1.pack(fill="x", pady=5)
        tk.Label(row1, text="版本切换: ").pack(side="left")
        self.var_version = tk.StringVar()
        self.combo_versions = ttk.Combobox(row1, textvariable=self.var_version, width=40, state="readonly")
        self.combo_versions.pack(side="left", padx=5)
        self.btn_check = tk.Button(row1, text="刷新/检查", command=self.refresh_data)
        self.btn_check.pack(side="left", padx=5)
        self.btn_execute = tk.Button(row1, text="执行更新/回退", bg="#c8e6c9", command=self.on_execute)
        self.btn_execute.pack(side="left", padx=5)

        # 依赖修复行
        row2 = tk.Frame(action_frame)
        row2.pack(fill="x", pady=15)
        tk.Label(row2, text="环境维护: ").pack(side="left")
        self.btn_core_pip = tk.Button(row2, text="安装/修复依赖 (pip install -r requirements.txt)", command=self.on_core_pip, state="disabled")
        self.btn_core_pip.pack(side="left")

    def set_path(self, path):
        self.full_path = path
        self.lbl_path.config(text=f"位置: {path}")
        self.refresh_data()

    def refresh_data(self):
        if not self.full_path or not os.path.exists(self.full_path):
            return
        
        self.btn_check.config(state="disabled")
        threading.Thread(target=self._async_check, daemon=True).start()

    def _async_check(self):
        text, color, is_update = self.check_status_base()
        _, current_commit, _ = self.run_git(["log", "-1", "--format=%h - %s (%cd)", "--date=short"])
        versions = self.fetch_versions_base()
        has_req = self.check_requirements()

        def update_ui():
            self.lbl_status_large.config(text=text, fg=color)
            self.lbl_commit_info.config(text=f"当前Commit: {current_commit}")
            self.combo_versions['values'] = versions
            if versions: self.combo_versions.current(0)
            self.btn_check.config(state="normal")
            
            if has_req:
                self.btn_core_pip.config(state="normal")
            else:
                self.btn_core_pip.config(state="disabled", text="根目录无 requirements.txt")
        
        self.app.root.after(0, update_ui)

    def on_execute(self):
        selection = self.var_version.get()
        if not selection: return
        if messagebox.askyesno("风险提示", f"即将对 ComfyUI 本体执行:\n{selection}\n\n注意：如果要更新本体，最好先备份。确定继续吗？"):
            self.btn_execute.config(state="disabled", text="执行中...")
            threading.Thread(target=self._async_execute, args=(selection,), daemon=True).start()

    def _async_execute(self, selection):
        success, msg = self.do_update_logic(selection)
        def post():
            self.btn_execute.config(state="normal", text="执行更新/回退")
            if success:
                messagebox.showinfo("成功", f"本体操作完成: {msg}")
                self.refresh_data()
            else:
                messagebox.showerror("失败", msg)
        self.app.root.after(0, post)

    def on_core_pip(self):
        if messagebox.askyesno("依赖修复", "即将对 ComfyUI 根目录执行 pip install -r requirements.txt。\n\n这可能需要一些时间，请耐心等待。"):
            self.btn_core_pip.config(state="disabled", text="安装中...")
            threading.Thread(target=self._async_pip, daemon=True).start()

    def _async_pip(self):
        success, msg = self.run_pip_install()
        def post():
            self.btn_core_pip.config(state="normal", text="安装/修复依赖")
            if success:
                messagebox.showinfo("成功", "本体依赖安装完成。")
            else:
                messagebox.showerror("失败", f"安装出错:\n{msg}")
        self.app.root.after(0, post)


# --- 主程序类 ---
class ComfyUpdaterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ComfyUI 版本管理器 👻CK👻 (Pro)")
        self.root.geometry("1150x800")

        self.config = configparser.ConfigParser()
        
        # 默认值
        self.git_exe = "git"
        self.python_exe = "python"
        self.comfyui_root = ""
        self.nodes_path = ""
        self.proxy_url = "" 
        
        self.plugin_rows = []
        
        # 加载配置
        self.load_config()

        # 1. 顶部设置面板 (重写，支持输入框和选择)
        self.setup_settings_ui()

        # 2. 选项卡
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)

        # Tab 1: 插件管理
        self.tab_plugins = tk.Frame(self.notebook)
        self.notebook.add(self.tab_plugins, text=" 🧩 插件管理 (Custom Nodes) ")
        
        plugin_toolbar = tk.Frame(self.tab_plugins)
        plugin_toolbar.pack(fill="x", pady=5)
        tk.Button(plugin_toolbar, text="刷新列表", command=self.refresh_plugin_list).pack(side="right", padx=5)
        self.btn_update_all = tk.Button(plugin_toolbar, text="一键更新所有插件", command=self.update_all_plugins, bg="#c8e6c9")
        self.btn_update_all.pack(side="right", padx=5)

        self.list_container = ScrollableFrame(self.tab_plugins)
        self.list_container.pack(fill="both", expand=True, padx=10, pady=5)

        # Tab 2: 本体管理
        self.tab_core = tk.Frame(self.notebook)
        self.notebook.add(self.tab_core, text=" ⚙️ ComfyUI 本体管理 ")
        
        self.core_manager = CoreManagerFrame(self.tab_core, self)
        self.core_manager.pack(fill="both", expand=True)

        # 3. 底部状态栏
        self.status_bar = tk.Label(root, text="就绪", bd=1, relief=tk.SUNKEN, anchor="w")
        self.status_bar.pack(side="bottom", fill="x")

        # 初始化路径检查
        if self.comfyui_root:
            self.set_root_path(self.comfyui_root, update_ui=False)
        self.update_status_bar()

    def setup_settings_ui(self):
        """创建顶部的设置区域"""
        top_frame = tk.LabelFrame(self.root, text="全局设置 (修改后自动保存)", padx=5, pady=5, bg="#f5f5f5")
        top_frame.pack(fill="x", padx=5, pady=5)

        # Grid 布局
        # Row 0: ComfyUI 目录
        tk.Label(top_frame, text="ComfyUI 根目录:", bg="#f5f5f5").grid(row=0, column=0, sticky="e", padx=5)
        self.entry_root = tk.Entry(top_frame, width=80)
        self.entry_root.insert(0, self.comfyui_root)
        self.entry_root.grid(row=0, column=1, padx=5, pady=2)
        tk.Button(top_frame, text="浏览...", command=self.browse_root).grid(row=0, column=2, padx=5)

        # Row 1: Python 路径
        tk.Label(top_frame, text="Python 路径:", bg="#f5f5f5").grid(row=1, column=0, sticky="e", padx=5)
        self.entry_python = tk.Entry(top_frame, width=80)
        self.entry_python.insert(0, self.python_exe)
        self.entry_python.grid(row=1, column=1, padx=5, pady=2)
        tk.Button(top_frame, text="选择文件...", command=self.browse_python).grid(row=1, column=2, padx=5)

        # Row 2: Git 路径
        tk.Label(top_frame, text="Git 路径:", bg="#f5f5f5").grid(row=2, column=0, sticky="e", padx=5)
        self.entry_git = tk.Entry(top_frame, width=80)
        self.entry_git.insert(0, self.git_exe)
        self.entry_git.grid(row=2, column=1, padx=5, pady=2)
        tk.Button(top_frame, text="选择文件...", command=self.browse_git).grid(row=2, column=2, padx=5)

        # Row 3: 代理
        tk.Label(top_frame, text="HTTP代理 (Git/Pip):", bg="#f5f5f5").grid(row=3, column=0, sticky="e", padx=5)
        self.entry_proxy = tk.Entry(top_frame, width=80)
        self.entry_proxy.insert(0, self.proxy_url)
        self.entry_proxy.grid(row=3, column=1, padx=5, pady=2)
        tk.Button(top_frame, text="应用配置", bg="#ffecb3", command=self.apply_config_from_ui).grid(row=3, column=2, padx=5)

    def load_config(self):
        if not os.path.exists(CONFIG_FILE): return
        try:
            self.config.read(CONFIG_FILE, encoding='utf-8')
            if 'Settings' in self.config:
                self.git_exe = self.config['Settings'].get('git_path', 'git').strip()
                self.python_exe = self.config['Settings'].get('python_path', 'python').strip()
                
                p = self.config['Settings'].get('comfyui_root_path', '').strip()
                if p:
                    # 如果是相对路径，转为绝对路径
                    if not os.path.isabs(p):
                        p = os.path.abspath(os.path.join(os.getcwd(), p))
                    self.comfyui_root = p
            
            if 'Network' in self.config:
                self.proxy_url = self.config['Network'].get('https_proxy', '').strip()
        except Exception as e:
            print(f"Load config error: {e}")

    def save_config(self):
        """将当前内存中的变量写入 config.ini"""
        if 'Settings' not in self.config: self.config['Settings'] = {}
        if 'Network' not in self.config: self.config['Network'] = {}

        self.config['Settings']['git_path'] = self.git_exe
        self.config['Settings']['python_path'] = self.python_exe
        
        # 尝试存相对路径以便便携，如果不在同级目录则存绝对路径
        try:
            rel_path = os.path.relpath(self.comfyui_root, os.getcwd())
            if ".." in rel_path and not rel_path.startswith(".."): # 简单的判断
                 self.config['Settings']['comfyui_root_path'] = self.comfyui_root
            else:
                 self.config['Settings']['comfyui_root_path'] = rel_path
        except:
            self.config['Settings']['comfyui_root_path'] = self.comfyui_root

        self.config['Network']['https_proxy'] = self.proxy_url

        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                self.config.write(f)
        except Exception as e:
            messagebox.showerror("错误", f"保存配置文件失败: {e}")

    # --- UI 事件处理 ---
    def browse_root(self):
        path = filedialog.askdirectory(initialdir=self.comfyui_root)
        if path:
            self.entry_root.delete(0, tk.END)
            self.entry_root.insert(0, path)
            self.apply_config_from_ui()

    def browse_python(self):
        path = filedialog.askopenfilename(title="选择 Python 可执行文件", filetypes=[("Executables", "*.exe"), ("All Files", "*.*")])
        if path:
            self.entry_python.delete(0, tk.END)
            self.entry_python.insert(0, path)
            self.apply_config_from_ui()
    
    def browse_git(self):
        path = filedialog.askopenfilename(title="选择 Git 可执行文件", filetypes=[("Executables", "*.exe"), ("All Files", "*.*")])
        if path:
            self.entry_git.delete(0, tk.END)
            self.entry_git.insert(0, path)
            self.apply_config_from_ui()

    def apply_config_from_ui(self):
        """从输入框读取并应用设置，然后保存"""
        self.comfyui_root = self.entry_root.get().strip()
        self.python_exe = self.entry_python.get().strip()
        self.git_exe = self.entry_git.get().strip()
        self.proxy_url = self.entry_proxy.get().strip()
        
        self.save_config()
        self.set_root_path(self.comfyui_root) # 刷新界面
        self.update_status_bar()
        messagebox.showinfo("保存", "配置已更新并保存。")

    def update_status_bar(self):
        msg = f"Git: {self.git_exe} | Python: {self.python_exe} | 代理: {self.proxy_url if self.proxy_url else '无'}"
        self.status_bar.config(text=msg)

    def set_root_path(self, root_path, update_ui=True):
        if not root_path: return
        self.comfyui_root = root_path
        self.nodes_path = os.path.join(root_path, "custom_nodes")
        
        # 1. 刷新本体 Tab
        self.core_manager.set_path(self.comfyui_root)

        # 2. 刷新插件 Tab
        if os.path.exists(self.nodes_path):
            self.refresh_plugin_list()
        else:
            if update_ui: # 避免初始化时弹窗
                pass 

    def run_cmd(self, cmd_args, cwd):
        """执行命令，统一处理代理和环境"""
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GCM_INTERACTIVE"] = "never"
            if self.proxy_url:
                env["http_proxy"] = self.proxy_url
                env["https_proxy"] = self.proxy_url

            result = subprocess.run(
                cmd_args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='ignore', 
                startupinfo=startupinfo, env=env, timeout=300 # pip 可能比较慢，超时设长一点
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return -1, "", str(e)

    def refresh_plugin_list(self):
        for widget in self.list_container.scrollable_frame.winfo_children():
            widget.destroy()
        self.plugin_rows.clear()

        if not os.path.exists(self.nodes_path): return

        folders = [f for f in os.listdir(self.nodes_path) if os.path.isdir(os.path.join(self.nodes_path, f))]
        
        for folder in folders:
            if folder.startswith("__") or folder.startswith("."): continue
            row = PluginRow(self.list_container.scrollable_frame, self, folder)
            self.plugin_rows.append(row)

    def update_all_plugins(self):
        targets = [row for row in self.plugin_rows if row.is_update_available]
        if not targets:
            messagebox.showinfo("提示", "当前没有检测到需要更新的插件。")
            return

        if not messagebox.askyesno("批量更新", f"检测到 {len(targets)} 个插件有新版本。\n是否开始批量更新？"):
            return

        self.btn_update_all.config(state="disabled", text="正在更新...")
        
        def run_batch():
            with ThreadPoolExecutor(max_workers=5) as executor:
                for row in targets:
                    row.btn_action.config(state="disabled", text="队列中...")
                    executor.submit(row.do_update, "最新版本 (Latest)", True)
            
            self.root.after(0, lambda: self.btn_update_all.config(state="normal", text="一键更新所有插件"))
            self.root.after(0, lambda: messagebox.showinfo("完成", "批量更新流程已结束。"))

        threading.Thread(target=run_batch, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = ComfyUpdaterApp(root)
    root.mainloop()