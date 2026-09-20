import configparser
import os
import queue
import sys
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# 便携版 Python 的 _pth 配置可能不包含脚本目录。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from operations import LATEST, Operations

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.ini"


class ComfyUpdaterApp:
    def __init__(self, root):
        self.root = root
        root.title("ComfyUI 版本管理器 · CK")
        root.geometry("760x560")
        root.minsize(700, 480)
        self.events = queue.Queue()
        self.busy = False
        self.items = {}
        self.visible_ids = {}
        self.config = configparser.ConfigParser(interpolation=None)
        self.config_error = ""
        try:
            self.config.read(CONFIG_FILE, encoding="utf-8-sig")
        except (OSError, configparser.Error) as exc:
            self.config_error = f"配置读取失败：{exc}"
        settings = self.config["Settings"] if self.config.has_section("Settings") else {}
        network = self.config["Network"] if self.config.has_section("Network") else {}
        default_root = str(BASE_DIR.parent) if (BASE_DIR.parent / "custom_nodes").is_dir() else ""
        self.root_var = tk.StringVar(value=settings.get("comfyui_root_path", default_root))
        self.python_var = tk.StringVar(value=settings.get("python_path", sys.executable))
        self.git_var = tk.StringVar(value=settings.get("git_path", "git"))
        self.proxy_var = tk.StringVar(value=network.get("https_proxy", ""))
        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="全部状态")
        self.status_var = tk.StringVar(value="就绪")
        self.summary_var = tk.StringVar()
        self.detail_var = tk.StringVar(value="选择插件查看版本与操作")
        self.version_var = tk.StringVar()
        self.setting_controls = []
        self.operation_controls = []
        self.comfy_root = self.resolve_path(self.root_var.get())
        self.nodes_path = os.path.join(self.comfy_root, "custom_nodes") if self.comfy_root else ""
        self.operations = self.make_operations()
        self.build_ui()
        self.search_var.trace_add("write", lambda *_: self.render_plugins())
        self.filter_var.trace_add("write", lambda *_: self.render_plugins())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(80, self.drain_events)
        if self.config_error:
            self.log(self.config_error)
        if self.comfy_root:
            self.refresh(check_remote=True)

    def resolve_path(self, value):
        value = os.path.expandvars(os.path.expanduser(value.strip().strip('"')))
        if not value:
            return ""
        return os.path.abspath(os.path.join(str(BASE_DIR), value))

    def resolve_executable(self, value):
        value = os.path.expandvars(os.path.expanduser(value.strip().strip('"')))
        return self.resolve_path(value) if "/" in value or "\\" in value else value

    def make_operations(self):
        return Operations(self.resolve_executable(self.git_var.get()) or "git",
                          self.resolve_executable(self.python_var.get()) or sys.executable,
                          self.proxy_var.get().strip())

    def build_ui(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=23, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TButton", padding=(4, 2))
        header = ttk.Frame(self.root, padding=(6, 4))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="ComfyUI 版本管理", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        settings_button = ttk.Button(header, text="环境设置", command=self.open_settings)
        settings_button.pack(side="right")
        self.setting_controls.append(settings_button)
        self.settings_window = None

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=6)
        plugins = ttk.Frame(self.notebook, padding=4)
        core = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(plugins, text="插件管理")
        self.notebook.add(core, text="ComfyUI 本体")
        toolbar = ttk.Frame(plugins)
        toolbar.pack(fill="x", pady=(0, 4))
        ttk.Label(toolbar, text="搜索").pack(side="left")
        ttk.Entry(toolbar, textvariable=self.search_var, width=19).pack(side="left", padx=4)
        ttk.Combobox(toolbar, textvariable=self.filter_var, state="readonly", width=10,
                     values=("全部状态", "有更新", "检查失败", "非 Git 仓库")).pack(side="left")
        for text, command in [("刷新", self.refresh), ("检查更新", self.check_updates),
                              ("批量更新", self.update_all)]:
            button = ttk.Button(toolbar, text=text, width=8, command=command)
            button.pack(side="right", padx=(4, 0))
            self.operation_controls.append(button)
        table = ttk.Frame(plugins)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("name", "status", "commit", "req"),
                                 height=10, show="headings", selectmode="browse")
        for key, title, width in [("name", "插件", 235), ("status", "状态", 100),
                                  ("commit", "提交", 200), ("req", "依赖", 46)]:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, minwidth=width if key in ("status", "req") else 90,
                             stretch=key in ("name", "commit"))
        self.tree.tag_configure("update", foreground="#a45b00")
        self.tree.tag_configure("error", foreground="#b42318")
        self.tree.tag_configure("plain", foreground="#667085")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show_selected())
        self.tree.bind("<Double-1>", lambda _: self.open_folder())
        ttk.Label(plugins, textvariable=self.summary_var).pack(anchor="w", pady=(3, 0))

        core_header = ttk.Frame(core)
        core_header.pack(fill="x", pady=(0, 4))
        self.core_label = ttk.Label(core_header, text="请设置 ComfyUI 根目录", wraplength=540)
        self.core_label.pack(side="left", fill="x", expand=True)
        core_check = ttk.Button(core_header, text="检查更新", width=8,
                                command=lambda: self.check_updates(core_only=True))
        core_check.pack(side="right", anchor="n")
        self.operation_controls.append(core_check)
        self.core_history = self.make_text(core, height=10)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _: self.show_selected())

        detail = ttk.Frame(self.root, padding=(6, 4))
        detail.grid(row=2, column=0, sticky="ew")
        ttk.Label(detail, textvariable=self.detail_var, anchor="w").pack(fill="x")
        actions = ttk.Frame(detail)
        actions.pack(fill="x", pady=(3, 0))
        self.versions = ttk.Combobox(actions, textvariable=self.version_var, state="disabled", width=22)
        self.versions.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.execute_button = ttk.Button(actions, text="更新 / 切换", width=10, command=self.execute)
        self.pip_button = ttk.Button(actions, text="安装依赖", width=8, command=self.install)
        self.open_button = ttk.Button(actions, text="打开目录", width=8, command=self.open_folder)
        self.delete_button = ttk.Button(actions, text="删除", width=5, command=self.delete)
        for button in (self.execute_button, self.pip_button, self.open_button, self.delete_button):
            button.pack(side="left", padx=(3, 0))
        self.notes = ttk.Notebook(self.root)
        self.notes.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 3))
        self.history = self.add_text_tab("版本记录", height=4)
        self.logs = self.add_text_tab("操作日志", height=4)
        bottom = ttk.Frame(self.root, padding=(6, 2))
        bottom.grid(row=4, column=0, sticky="ew")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=75)
        self.progress.pack(side="right")
        ttk.Label(bottom, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        self.show_selected()

    def open_settings(self):
        if self.settings_window is not None:
            self.settings_window.deiconify()
            self.settings_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("环境设置")
        window.transient(self.root)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        settings = ttk.Frame(window, padding=8)
        settings.pack(fill="both", expand=True)
        fields = [("ComfyUI 目录", self.root_var, "directory"),
                  ("Python", self.python_var, "file"),
                  ("Git", self.git_var, "file"),
                  ("HTTP 代理", self.proxy_var, None)]
        for index, (label, variable, kind) in enumerate(fields):
            ttk.Label(settings, text=label).grid(row=index, column=0, sticky="e", padx=(0, 5), pady=3)
            entry = ttk.Entry(settings, textvariable=variable, width=40, show="*" if kind is None else "")
            entry.grid(row=index, column=1, sticky="ew", pady=3)
            self.setting_controls.append(entry)
            if kind:
                button = ttk.Button(settings, text="浏览", width=6,
                                    command=lambda v=variable, k=kind: self.browse(v, k))
                button.grid(row=index, column=2, padx=(5, 0))
                self.setting_controls.append(button)
        save = ttk.Button(settings, text="应用配置", command=self.apply_config)
        save.grid(row=4, column=1, sticky="e", pady=(5, 0))
        self.setting_controls.append(save)
        ttk.Button(settings, text="关闭", width=6, command=window.withdraw).grid(row=4, column=2, padx=(5, 0), pady=(5, 0))

    def make_text(self, parent, height):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, height=height, width=1, wrap="word", state="disabled",
                       relief="flat", font=("Microsoft YaHei UI", 9), padx=3, pady=2)
        scrollbar = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return text

    def add_text_tab(self, name, height):
        frame = ttk.Frame(self.notes)
        self.notes.add(frame, text=name)
        return self.make_text(frame, height)

    def log(self, text):
        # 代理可能包含凭据，不写入界面日志。
        proxy = self.operations.proxy_url
        if proxy:
            text = str(text).replace(proxy, "[已配置代理]")
        self.logs.configure(state="normal")
        self.logs.insert("end", f"[{datetime.now():%H:%M:%S}] {text}\n")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def browse(self, variable, kind):
        path = filedialog.askdirectory(parent=self.root) if kind == "directory" else filedialog.askopenfilename(parent=self.root)
        if path:
            variable.set(path)

    def apply_config(self):
        if self.busy:
            return
        root_path = self.resolve_path(self.root_var.get())
        if not os.path.isdir(os.path.join(root_path, "custom_nodes")):
            messagebox.showerror("目录不正确", "请选择包含 custom_nodes 文件夹的 ComfyUI 根目录。", parent=self.root)
            return
        self.config["Settings"] = dict(self.config["Settings"]) if self.config.has_section("Settings") else {}
        self.config["Network"] = dict(self.config["Network"]) if self.config.has_section("Network") else {}
        self.config["Settings"].update(comfyui_root_path=root_path,
                                       python_path=self.python_var.get().strip(), git_path=self.git_var.get().strip())
        self.config["Network"]["https_proxy"] = self.proxy_var.get().strip()
        try:
            with CONFIG_FILE.open("w", encoding="utf-8") as stream:
                self.config.write(stream)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self.comfy_root = root_path
        self.nodes_path = os.path.join(root_path, "custom_nodes")
        self.root_var.set(root_path)
        self.operations = self.make_operations()
        self.log("配置已保存。")
        self.refresh()

    def selected_path(self):
        if self.notebook.index(self.notebook.select()) == 1:
            return self.comfy_root if self.comfy_root in self.items else None
        selected = self.tree.selection()
        return self.visible_ids.get(selected[0]) if selected else None

    def render_plugins(self):
        selected = self.selected_path()
        self.tree.delete(*self.tree.get_children())
        self.visible_ids.clear()
        query = self.search_var.get().strip().casefold()
        filter_name = self.filter_var.get()
        plugin_items = [(path, info) for path, info in self.items.items() if path != self.comfy_root]
        for path, info in sorted(plugin_items, key=lambda pair: os.path.basename(pair[0]).casefold()):
            name = os.path.basename(path)
            if query not in name.casefold():
                continue
            if filter_name == "有更新" and not info["update"]:
                continue
            if filter_name == "检查失败" and "失败" not in info["status"]:
                continue
            if filter_name == "非 Git 仓库" and info["git"]:
                continue
            tag = "error" if "失败" in info["status"] else "update" if info["update"] else "plain" if not info["git"] else ""
            status = {"与本地远端记录一致": "本地记录一致", "有更新（落后远端）": "有更新",
                      "本地领先（远端无新内容）": "本地领先", "游离HEAD（历史版本）": "历史版本",
                      "本地与远端分叉": "分支分叉"}.get(info["status"], info["status"])
            iid = self.tree.insert("", "end", values=(name, status, info["commit"],
                                   "可安装" if info["requirements"] else "无"), tags=(tag,))
            self.visible_ids[iid] = path
            if path == selected:
                self.tree.selection_set(iid)
        if not self.tree.selection() and self.visible_ids:
            self.tree.selection_set(next(iter(self.visible_ids)))
        count = len(plugin_items)
        updates = sum(info["update"] for _, info in plugin_items)
        self.summary_var.set(f"共 {count} 个插件 · 显示 {len(self.visible_ids)} 个 · {updates} 个有更新")
        self.show_selected()

    def show_selected(self):
        path = self.selected_path()
        info = self.items.get(path)
        self.detail_var.set(f"{os.path.basename(path)} · {info['status']}" if info else "选择插件后操作 · 双击打开目录")
        old_version = self.version_var.get()
        versions = info["versions"] if info else []
        self.versions.configure(values=versions, state="readonly" if versions and not self.busy else "disabled")
        self.version_var.set(old_version if old_version in versions else versions[0] if versions else "")
        for button, enabled in [(self.execute_button, info and info["git"] and versions),
                                (self.pip_button, info and info["requirements"]),
                                (self.delete_button, info and path != self.comfy_root)]:
            button.configure(state="normal" if enabled and not self.busy else "disabled")
        self.open_button.configure(state="normal" if info else "disabled")
        content = "\n\n".join(part for part in (info["detail"], path, info["history"]) if part) if info else ""
        core_active = self.notebook.index(self.notebook.select()) == 1
        for widget in (self.history, self.core_history):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("end", content if widget == self.history or core_active else "")
            widget.configure(state="disabled")
        # 本体历史在页内展开，底部保留日志，不重复占用一块版本面板。
        self.notes.tab(0, state="hidden" if core_active else "normal")
        core = self.items.get(self.comfy_root)
        if core:
            self.core_label.configure(text=f"{core['status']} · {core['commit'].split(' - ', 1)[0]}")

    def set_busy(self, value, text):
        self.busy = value
        self.status_var.set(text)
        for control in self.setting_controls + self.operation_controls:
            control.configure(state="disabled" if value else "normal")
        if value:
            self.progress.start(12)
        else:
            self.progress.stop()
        self.show_selected()

    def start_task(self, label, worker, done):
        if self.busy:
            return
        self.set_busy(True, label)
        self.log(label)
        def run():
            try:
                result = worker()
            except Exception as exc:
                self.events.put(("error", str(exc)))
            else:
                self.events.put(("done", (done, result)))
        threading.Thread(target=run, daemon=True).start()

    def drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "progress":
                    self.status_var.set(payload)
                elif kind == "error":
                    self.set_busy(False, "操作失败，详情见日志")
                    self.log(payload)
                    self.notes.select(1)
                    messagebox.showerror("操作失败", payload, parent=self.root)
                elif kind == "done":
                    callback, result = payload
                    self.set_busy(False, "就绪")
                    callback(result)
        except queue.Empty:
            pass
        self.root.after(80, self.drain_events)

    def inspect_paths(self, paths, fetch=False):
        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self.operations.inspect, path, fetch): path for path in paths}
            for future in as_completed(futures):
                path = futures[future]
                try:
                    results[path] = future.result()
                except Exception as exc:
                    results[path] = dict(status="检查失败", update=False,
                                         git=os.path.exists(os.path.join(path, ".git")),
                                         requirements=os.path.isfile(os.path.join(path, "requirements.txt")),
                                         versions=[], commit="", history="", detail=str(exc))
                info = results[path]
                if "失败" in info["status"]:
                    self.events.put(("log", f"{os.path.basename(path)}：{info['detail']}"))
                self.events.put(("progress", f"{'检查更新' if fetch else '读取本地信息'} {len(results)}/{len(paths)}"))
        return results

    def refresh(self, check_remote=False):
        if self.busy:
            return
        if not self.nodes_path or not os.path.isdir(self.nodes_path):
            self.status_var.set("请配置包含 custom_nodes 的 ComfyUI 根目录，并应用配置")
            return
        def scan():
            with os.scandir(self.nodes_path) as entries:
                paths = [entry.path for entry in entries if not entry.name.startswith((".", "__"))
                         and entry.is_dir()]
            return self.inspect_paths([self.comfy_root] + paths)
        def done(result):
            self.items = result
            self.render_plugins()
            self.status_var.set("本地列表已刷新；点击“检查更新”获取远端状态")
            if check_remote:
                self.check_updates()
        self.start_task("正在读取本地插件信息…", scan, done)

    def check_updates(self, core_only=False):
        if self.busy:
            return
        paths = [self.comfy_root] if core_only and self.comfy_root in self.items else list(self.items) if not core_only else []
        if not paths:
            return
        def done(result):
            self.items.update(result)
            self.render_plugins()
            failures = sum("失败" in info["status"] for info in result.values())
            text = f"检查完成：{len(result)} 项，失败 {failures} 项"
            self.status_var.set(text)
            self.log(text)
        self.start_task("正在检查远端更新…", lambda: self.inspect_paths(paths, True), done)

    def perform(self, label, path, action, delete=False):
        def worker():
            message = action()
            info = None if delete else self.inspect_paths([path])[path]
            return message, info
        def done(result):
            message, info = result
            if delete:
                self.items.pop(path, None)
            else:
                self.items[path] = info
            self.render_plugins()
            self.log(f"{os.path.basename(path)}：{message}")
            self.status_var.set(message)
            messagebox.showinfo("操作完成", message + "\n\n插件或版本变更后，请重启 ComfyUI 生效。", parent=self.root)
        self.start_task(label, worker, done)

    def execute(self):
        path = self.selected_path()
        selection = self.version_var.get()
        if not path or not selection or self.busy:
            return
        if messagebox.askyesno("确认版本操作", f"{os.path.basename(path)}\n\n目标：{selection}\n\n继续执行？", parent=self.root):
            self.perform("正在更新 / 切换版本…", path, lambda: self.operations.update(path, selection))

    def install(self):
        path = self.selected_path()
        if not path or self.busy:
            return
        if messagebox.askyesno("安装依赖", f"为 {os.path.basename(path)} 安装 requirements.txt 中的依赖。\n\nPython：{self.operations.python_exe}\n安装进度将在独立终端中显示。\n\n继续？", parent=self.root):
            self.perform("正在安装依赖，请查看终端…", path, lambda: self.operations.install_requirements(path))

    def delete(self):
        path = self.selected_path()
        if not path or path == self.comfy_root or self.busy:
            return
        if messagebox.askyesno("确认删除插件", f"将永久删除此插件目录及其中的文件：\n{path}\n\n确定删除？", parent=self.root):
            self.perform("正在删除插件…", path, lambda: self.operations.remove_plugin(path, self.nodes_path), delete=True)

    def update_all(self):
        if self.busy:
            return
        paths = [path for path, info in self.items.items() if path != self.comfy_root and info["update"]]
        if not paths:
            messagebox.showinfo("批量更新", "当前没有已检测到更新的插件。可先点击“检查更新”。", parent=self.root)
            return
        if not messagebox.askyesno("批量更新", f"更新 {len(paths)} 个有新版的插件？\n结果将在操作日志中逐项显示。", parent=self.root):
            return
        def worker():
            failures = []
            for index, path in enumerate(paths, 1):
                name = os.path.basename(path)
                self.events.put(("progress", f"正在更新 {index}/{len(paths)}：{name}"))
                try:
                    message = self.operations.update(path, LATEST)
                    self.events.put(("log", f"成功 · {name}：{message}"))
                except Exception as exc:
                    failures.append(name)
                    self.events.put(("log", f"失败 · {name}：{exc}"))
            return failures, self.inspect_paths(paths)
        def done(result):
            failures, infos = result
            self.items.update(infos)
            self.render_plugins()
            self.notes.select(1)
            text = f"批量更新完成：成功 {len(paths) - len(failures)} 项，失败 {len(failures)} 项。"
            self.log(text)
            self.status_var.set(text)
            if failures:
                messagebox.showwarning("批量更新结果", text + "\n\n失败项：\n" + "\n".join(failures) + "\n\n具体原因见操作日志。", parent=self.root)
            else:
                messagebox.showinfo("批量更新结果", text + "\n\n请重启 ComfyUI 生效。", parent=self.root)
        self.start_task("开始批量更新…", worker, done)

    def open_folder(self):
        path = self.selected_path()
        if path:
            try:
                os.startfile(path)
            except OSError as exc:
                messagebox.showerror("打开目录失败", str(exc), parent=self.root)

    def on_close(self):
        if self.busy:
            self.status_var.set("任务正在执行，请等待完成后关闭；依赖安装请先关闭安装终端。")
            return
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ComfyUpdaterApp(root)
    root.mainloop()
