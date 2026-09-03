# -*- coding: utf-8 -*-
"""
本地模型路由网关 · 图形界面
==================================================================
四个区块（选项卡）：
  · 供应商：增删改（名称/上游地址/API Key/并发/重试/退避），空白自加
  · 模型：本地名 → 供应商 → 上游模型ID → 自定义并发 → 失败后流转链（≤3跳，兜底自定义）
  · 网关：端口/本地key/超时/流转等待阈值/最大跳数 + 启动/停止 + 开机自启
  · 运行：日志与统计（成功/重试/流转/失败/在途）

保留：托盘驻留、测试连接后台线程不卡界面、密钥脱敏显示、配置持久化。
"""

import json
import os
import queue
import socket
import sys
import threading
import time
import webbrowser

try:
    import ctypes
    HAS_CTYPES = True
except Exception:
    HAS_CTYPES = False

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from gateway_core import (ModelGateway, default_config, validate_config,
                          coerce_int, coerce_float)

APP_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(APP_DIR, "gateway_config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ModelProxy"

try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False
    Image = None
    ImageDraw = None
    ImageTk = None

try:
    import winreg
    HAS_WINREG = True
except Exception:
    HAS_WINREG = False

APP_TITLE = "ModelProxy"


def _resource_paths(fname):
    """返回某资源文件的候选绝对路径：
    exe 同目录 → 项目目录 → PyInstaller 解压目录(_MEIPASS，内置资源)。"""
    dirs = [APP_DIR, os.path.dirname(APP_DIR)]
    if hasattr(sys, "_MEIPASS"):
        dirs.append(sys._MEIPASS)
    return [os.path.join(d, fname) for d in dirs]


def load_config():
    cfg = default_config()
    cfg.setdefault("ui", {})  # 界面偏好（列宽等），不属于网关核心配置
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("gateway"), dict):
                cfg["gateway"].update(data["gateway"])
            cfg["providers"] = data.get("providers", []) or []
            # 清理 v0.8 时代供应商的手工分组字段残留（早已改为按名称自动归堆）
            for p in cfg["providers"]:
                if isinstance(p, dict):
                    p.pop("group", None)
            cfg["models"] = data.get("models", []) or []
            if isinstance(data.get("ui"), dict):
                cfg["ui"] = data["ui"]
            if isinstance(data.get("token_stats"), dict):
                cfg["token_stats"] = data["token_stats"]
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return CONFIG_PATH


def mask_key(k):
    k = str(k or "")
    if len(k) <= 8:
        return "*" * len(k)
    return k[:4] + "****" + k[-4:]


def _is_hhmm(v):
    """校验 HH:MM 时间格式（如 14:00、06:30）。"""
    import re
    return bool(re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(v or "").strip()))


def re_fullmatch_geom(geo):
    """解析 Tk geometry 字符串 "WxH+X+Y"，返回 Match（含 w/h/x/y 四组）；不匹配返回 None。"""
    import re
    return re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", str(geo or "").strip())


def _upstream_url(base_url, path):
    """智能拼接上游 URL（与网关核心同规则）：
    base_url 已以 /v1 结尾（如阿里云 compatible-mode/v1）则直接拼 path，否则补 /v1。
    供「测试模型」「获取模型列表」等直连供应商的功能使用。"""
    base = str(base_url or "").rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1"):
        return base + "/" + path
    return base + "/v1/" + path


def autostart_enabled():
    if not HAS_WINREG:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable):
    if not HAS_WINREG:
        return False
    exe = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, '"%s" --autostart' % exe)
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except OSError:
                    pass
        return True
    except OSError:
        return False


# ---------------- 通用编辑对话框 ----------------


class FormDialog(tk.Toplevel):
    """通用字段编辑对话框。fields = [(key,label,default),...]，返回 dict 或 None。"""

    def __init__(self, parent, title, fields, initial=None):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self.vars = {}
        initial = initial or {}
        body = ttk.Frame(self)
        body.pack(padx=16, pady=14)
        for i, (key, label, default) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="w",
                                             padx=(0, 10), pady=4)
            var = tk.StringVar(value=str(initial.get(key, default)))
            ent = ttk.Entry(body, textvariable=var, width=42)
            ent.grid(row=i, column=1, sticky="ew", pady=4)
            self.vars[key] = var
        btns = ttk.Frame(self)
        btns.pack(pady=(0, 14))
        ttk.Button(btns, text="确定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 80
        self.geometry("+%d+%d" % (x, y))
        self.wait_window()

    def _ok(self):
        self.result = {k: v.get().strip() for k, v in self.vars.items()}
        self.destroy()


# ---------------- 主窗口 ----------------


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.gw = ModelGateway(self.cfg, logger=self._enqueue_log)
        self.log_q = queue.Queue()
        self.icon = None
        self._test_running = False
        self._spin_idx = 0
        self._test_name = ""

        root.title(APP_TITLE)
        # 恢复上次保存的窗口尺寸（无记录则用默认值）
        ui_geom = (self.cfg.get("ui", {}).get("window") or {})
        w = coerce_int(ui_geom.get("width"), 760)
        h = coerce_int(ui_geom.get("height"), 780)
        root.geometry("%dx%d" % (max(w, 400), max(h, 300)))
        root.minsize(640, 640)
        self._apply_window_icon()
        self._build_ui()
        self._apply_gateway_form()
        self._poll()
        # 窗口尺寸变化防抖保存（拖拽/最大化后 800ms 无变化才写盘）
        root.bind("<Configure>", self._on_window_configure)
        self._geom_save_job = None
        self._geom_last = None
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self._ensure_tray()
        if "--autostart" in sys.argv:
            self.root.after(400, self.start_gateway)
        # 上次关闭时是最大化 → 窗口就绪后恢复最大化
        if (self.cfg.get("ui", {}).get("window") or {}).get("maximized"):
            self.root.after(300, lambda: self.root.state("zoomed"))

    # ---------------- 界面 ----------------
    def _on_window_configure(self, event):
        """主窗口大小/位置变化：防抖 800ms 后保存尺寸（含最大化状态）。"""
        if event.widget is not self.root:
            return  # 忽略子控件的事件
        if self._geom_save_job is not None:
            try:
                self.root.after_cancel(self._geom_save_job)
            except Exception:
                pass
        self._geom_save_job = self.root.after(800, self._save_window_geom)

    def _save_window_geom(self):
        self._geom_save_job = None
        try:
            geo = self.root.geometry()  # 形如 "WxH+X+Y"
            m = re_fullmatch_geom(geo)
            if not m:
                return
            w, h = int(m.group(1)), int(m.group(2))
            if (w, h) == self._geom_last:
                return  # 尺寸没变不重复写盘
            self._geom_last = (w, h)
            win = self.cfg.setdefault("ui", {}).setdefault("window", {})
            win["width"], win["height"] = w, h
            win["maximized"] = bool(self.root.state() == "zoomed")
            save_config(self.cfg)
        except Exception:
            pass


    def _apply_window_icon(self):
        """设置窗口标题栏 + 任务栏图标（用 Logo 生成的文件）。"""
        candidates_ico = (_resource_paths("ModelProxy.ico")
                          + _resource_paths("icon.ico"))
        for p in candidates_ico:
            if os.path.exists(p):
                try:
                    self.root.iconbitmap(p)
                    break
                except Exception:
                    continue
        # 任务栏/跨平台用 PNG 更可靠
        candidates_png = (_resource_paths("tray_icon.png")
                          + _resource_paths("Logo.png"))
        for p in candidates_png:
            if os.path.exists(p):
                try:
                    img = Image.open(p).convert("RGBA")
                    img.thumbnail((64, 64), Image.LANCZOS)
                    self._photo = ImageTk.PhotoImage(img)
                    self.root.iconphoto(True, self._photo)
                    break
                except Exception:
                    continue

    def _build_ui(self):
        # 顶栏：状态 + 启停
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 0))
        self.status_var = tk.StringVar(value="未运行")
        self.status_lbl = tk.Label(top, textvariable=self.status_var,
                                   font=("Microsoft YaHei", 11, "bold"), fg="#666")
        self.status_lbl.pack(side="left")
        self.btn_start = ttk.Button(top, text="启动网关", command=self.start_gateway)
        self.btn_start.pack(side="right", padx=4)
        self.btn_stop = ttk.Button(top, text="停止网关", command=self.stop_gateway,
                                   state="disabled")
        self.btn_stop.pack(side="right", padx=4)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=8)

        self._build_providers_tab()
        self._build_models_tab()
        self._build_gateway_tab()
        self._build_log_tab()
        self._build_about_tab()

    # ---- 供应商 ----
    def _build_providers_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text=" 供应商 ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="新增供应商", command=self.add_provider).pack(side="left")
        ttk.Button(bar, text="编辑", command=self.edit_provider).pack(side="left", padx=6)
        ttk.Button(bar, text="删除", command=self.del_provider).pack(side="left")

        cols = ("id", "base_url", "key", "conc", "retry")
        self.prov_tree = ttk.Treeview(tab, columns=cols, show="tree headings", height=14)
        # 第一列（树形列）即名称分组列：父节点=名称（N），子节点=供应商条目
        self.prov_tree.heading("#0", text="名称")
        heads = [("id", "ID", 90), ("base_url", "上游地址", 230),
                 ("key", "API Key", 110), ("conc", "并发", 50), ("retry", "重试", 50)]
        for c, t, w in heads:
            self.prov_tree.heading(c, text=t)
            self.prov_tree.column(c, width=w, anchor="w")
        self._setup_tree_cols(self.prov_tree, "providers", heads, 120)
        self._bind_tree_open(self.prov_tree, "providers")
        sb = ttk.Scrollbar(tab, orient="vertical", command=self.prov_tree.yview)
        self.prov_tree.configure(yscrollcommand=sb.set)
        self.prov_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        sb.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))
        self.prov_tree.bind("<Button-3>", self._show_provider_menu)
        self._refresh_providers()

    def _setup_tree_cols(self, tree, key, heads, tree_col_width):
        """树形列表通用：恢复记住的列宽，并在拖动后自动保存。"""
        saved = (self.cfg.get("ui", {}).get("col_widths", {}).get(key, {}))
        if saved:
            try:
                tree.column("#0", width=coerce_int(saved.get("#0"), tree_col_width))
            except Exception:
                pass
            for c, _t, w in heads:
                tree.column(c, width=coerce_int(saved.get(c), w))
        else:
            tree.column("#0", width=tree_col_width)
        tree.bind("<ButtonRelease-1>",
                  lambda e, t=tree, k=key, h=heads: self._save_tree_cols(t, k, h))

    def _save_tree_cols(self, tree, key, heads):
        """拖动列分隔结束后，把 #0 与各数据列的当前宽度写回配置。"""
        try:
            widths = {"#0": tree.column("#0", "width")}
            for c, _t, _w in heads:
                widths[c] = tree.column(c, "width")
            ui = self.cfg.setdefault("ui", {})
            ui.setdefault("col_widths", {})[key] = widths
            save_config(self.cfg)
        except Exception:
            pass

    def _tree_open_state(self, key, iid, default=True):
        """读取某树形列表父节点上次保存的展开/折叠状态。"""
        st = self.cfg.get("ui", {}).get("tree_open", {}).get(key, {})
        if iid in st:
            return bool(st[iid])
        return default

    def _save_tree_open(self, tree, key):
        """记录当前所有父节点的展开/折叠状态并写盘（绑定展开/折叠事件触发）。"""
        try:
            st = {}
            for iid in tree.get_children():
                st[iid] = bool(tree.item(iid, "open"))
            self.cfg.setdefault("ui", {}).setdefault("tree_open", {})[key] = st
            save_config(self.cfg)
        except Exception:
            pass

    def _bind_tree_open(self, tree, key):
        tree.bind("<<TreeviewOpen>>",
                  lambda e: self._save_tree_open(tree, key))
        tree.bind("<<TreeviewClose>>",
                  lambda e: self._save_tree_open(tree, key))

    def _show_provider_menu(self, event=None):
        """供应商右键菜单：打开 API 文档。"""
        iid = self.prov_tree.identify_row(event.y)
        if not iid:
            return
        self.prov_tree.selection_set(iid)
        prov = self._prov_by_id(iid)
        if prov is None:
            return
        menu = tk.Menu(self.root, tearoff=0)
        doc_url = prov.get("api_doc_url", "")
        if doc_url:
            menu.add_command(label="打开 API 文档", command=lambda: self._open_provider_doc(iid))
        else:
            menu.add_command(label="打开 API 文档", state="disabled")
        menu.add_separator()
        menu.add_command(label="编辑供应商", command=self.edit_provider)
        menu.add_command(label="删除供应商", command=self.del_provider)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_provider_doc(self, pid):
        """调用系统默认浏览器打开供应商的 API 文档网址。"""
        prov = self._prov_by_id(pid)
        if prov is None:
            return
        url = str(prov.get("api_doc_url", "")).strip()
        if not url:
            messagebox.showinfo("提示", "该供应商未填写 API 文档网址")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            webbrowser.open(url)
            self._log("已打开 %s 的 API 文档：%s" % (prov.get("name", pid), url))
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _refresh_providers(self):
        for i in self.prov_tree.get_children():
            self.prov_tree.delete(i)
        provs = sorted(self.cfg.get("providers", []),
                       key=lambda p: self._sort_key_models(p.get("id", "")))
        self._prov_group_nodes = set()
        # 按名称归堆（同名供应商折叠到同一父节点），并恢复上次的折叠状态
        by_name = {}
        for p in provs:
            by_name.setdefault(str(p.get("name") or p.get("id") or ""), []).append(p)
        for name in sorted(by_name, key=lambda x: self._sort_key_models(x)):
            gid = "grp:" + name
            self.prov_tree.insert("", "end", iid=gid, open=self._tree_open_state("providers", gid),
                                  text="%s（%d）" % (name, len(by_name[name])))
            self._prov_group_nodes.add(gid)
            for p in by_name[name]:
                self.prov_tree.insert(gid, "end", iid=p["id"], text="", values=(
                    p.get("id"), p.get("base_url"),
                    mask_key(p.get("api_key")), p.get("max_concurrency", 16),
                    p.get("max_retries", 4)))
        self._refresh_model_provider_opts()

    def _sel_provider(self):
        sel = self.prov_tree.selection()
        if not sel:
            return None
        # 选中分组父节点时视为未选中供应商
        if sel[0] in getattr(self, "_prov_group_nodes", ()):
            return None
        return sel[0]

    def _provider_fields(self):
        return [("id", "ID（英文，唯一）", ""),
                ("name", "名称（同名的自动归堆折叠）", ""),
                ("base_url", "上游地址", "https://"),
                ("api_key", "API Key", ""),
                ("api_doc_url", "API 文档网址", "https://"),
                ("max_concurrency", "并发上限（账号级）", "16"),
                ("max_retries", "重试次数", "4"),
                ("base_delay", "退避基数(秒)", "8")]

    def add_provider(self):
        d = FormDialog(self.root, "新增供应商", self._provider_fields())
        if not d.result:
            return
        r = d.result
        if not r["id"]:
            messagebox.showerror("提示", "供应商 ID 不能为空")
            return
        if any(p["id"] == r["id"] for p in self.cfg["providers"]):
            messagebox.showerror("提示", "ID %s 已存在" % r["id"])
            return
        self.cfg["providers"].append({
            "id": r["id"], "name": r["name"] or r["id"],
            "base_url": r["base_url"], "api_key": r["api_key"],
            "api_doc_url": r.get("api_doc_url", ""),
            "max_concurrency": coerce_int(r["max_concurrency"], 16),
            "max_retries": coerce_int(r["max_retries"], 4),
            "base_delay": coerce_float(r["base_delay"], 8)})
        self._persist_and_refresh("已添加供应商 %s" % r["id"])

    def edit_provider(self):
        pid = self._sel_provider()
        if not pid:
            messagebox.showwarning("提示", "请先选中要编辑的供应商")
            return
        p = next((x for x in self.cfg["providers"] if x["id"] == pid), None)
        if p is None:
            return
        d = FormDialog(self.root, "编辑供应商", self._provider_fields(), initial=p)
        if not d.result:
            return
        r = d.result
        new_id = r["id"].strip()
        if not new_id:
            messagebox.showerror("提示", "供应商 ID 不能为空")
            return
        # 改 ID：检查冲突，并同步修正引用该供应商的模型（含失败后流转链）
        if new_id != pid and any(x["id"] == new_id for x in self.cfg["providers"]):
            messagebox.showerror("提示", "ID %s 已存在" % new_id)
            return
        old_id = pid
        p["id"] = new_id
        p.update({"name": r["name"] or new_id,
                  "base_url": r["base_url"],
                  "api_key": r["api_key"],
                  "api_doc_url": r.get("api_doc_url", ""),
                  "max_concurrency": coerce_int(r["max_concurrency"], 16),
                  "max_retries": coerce_int(r["max_retries"], 4),
                  "base_delay": coerce_float(r["base_delay"], 8)})
        # 同步模型引用
        if new_id != old_id:
            for m in self.cfg["models"]:
                if m.get("provider") == old_id:
                    m["provider"] = new_id
                for fb in (m.get("fallback") or []):
                    if fb.get("provider") == old_id:
                        fb["provider"] = new_id
        self._persist_and_refresh("已更新供应商 %s" % new_id)

    def del_provider(self):
        pid = self._sel_provider()
        if not pid:
            return
        used = [m["id"] for m in self.cfg["models"] if m.get("provider") == pid]
        if used:
            messagebox.showwarning("提示", "供应商 %s 正被模型 %s 使用，请先处理这些模型"
                                   % (pid, "、".join(used)))
            return
        if messagebox.askyesno("确认", "删除供应商 %s？" % pid):
            self.cfg["providers"] = [p for p in self.cfg["providers"] if p["id"] != pid]
            self._persist_and_refresh("已删除供应商 %s" % pid)

    # ---- 模型 ----
    def _build_models_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text=" 模型 ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="新增模型", command=self.add_model).pack(side="left")
        ttk.Button(bar, text="编辑", command=self.edit_model).pack(side="left", padx=6)
        ttk.Button(bar, text="删除", command=self.del_model).pack(side="left")
        ttk.Button(bar, text="测试选中模型", command=self.test_model).pack(side="left", padx=12)

        cols = ("id", "upstream", "conc", "fb")
        self.model_tree = ttk.Treeview(tab, columns=cols, show="tree headings", height=14)
        # 第一列（树形列）即供应商分组列：父节点=供应商（N），子节点=模型
        self.model_tree.heading("#0", text="供应商")
        heads = [("id", "本地模型名（自定义）", 180),
                 ("upstream", "上游真实模型 ID", 170), ("conc", "并发", 50),
                 ("fb", "失败后流转", 100)]
        for c, t, w in heads:
            self.model_tree.heading(c, text=t)
            self.model_tree.column(c, width=w, anchor="w")
        self._setup_tree_cols(self.model_tree, "models", heads, 150)
        self._bind_tree_open(self.model_tree, "models")
        sb = ttk.Scrollbar(tab, orient="vertical", command=self.model_tree.yview)
        self.model_tree.configure(yscrollcommand=sb.set)
        self.model_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        sb.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))
        self.model_tree.bind("<Double-1>", self._copy_model_name)
        self._refresh_models()

    def _copy_model_name(self, event=None):
        """双击模型行，复制其本地模型名到剪贴板。"""
        sel = self.model_tree.selection()
        if not sel:
            return
        mid = sel[0]
        if not mid or mid in getattr(self, "_model_parent_nodes", ()):
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(mid)
        self.root.update()
        self._log("已复制本地模型名：%s" % mid)
        self._show_toast("已复制：%s" % mid)

    def _show_toast(self, text, duration=2500):
        """在窗口右下角显示一个自动消失的轻提示（非弹窗）。"""
        try:
            # 关闭上一个未消失的 toast
            if getattr(self, "_toast", None) is not None:
                try:
                    self._toast.destroy()
                except Exception:
                    pass
            toast = tk.Toplevel(self.root)
            self._toast = toast
            toast.overrideredirect(True)  # 无边框
            toast.attributes("-topmost", True)
            lbl = tk.Label(toast, text=text, bg="#2E7D32", fg="#ffffff",
                           font=("Microsoft YaHei", 10),
                           padx=18, pady=10)
            lbl.pack()
            toast.update_idletasks()
            w = toast.winfo_width()
            h = toast.winfo_height()
            # 定位到主窗口右下角内侧
            rx = self.root.winfo_rootx()
            ry = self.root.winfo_rooty()
            rw = self.root.winfo_width()
            rh = self.root.winfo_height()
            x = rx + rw - w - 24
            y = ry + rh - h - 40
            toast.geometry("+%d+%d" % (x, y))
            # 定时自动销毁
            toast.after(duration, toast.destroy)
        except Exception:
            pass

    def _sort_key_models(self, name):
        """模型名排序键：字母按字典序升序，数字按数值降序（9→0）。
        拆分字母段与数字段，字母段返回小写串（升序），数字段返回取负值（降序）。"""
        import re
        parts = re.findall(r'(\d+)|([^\d]+)', str(name))
        key = []
        for digits, alpha in parts:
            if digits:
                # 数字段：降序 → 取负值，且用零填充保证同位数可比
                key.append((1, -int(digits)))
            else:
                # 字母段：升序（不区分大小写）
                key.append((0, alpha.lower()))
        return key

    def _refresh_models(self):
        for i in self.model_tree.get_children():
            self.model_tree.delete(i)
        self._model_parent_nodes = set()
        models = sorted(self.cfg.get("models", []),
                        key=lambda m: self._sort_key_models(m.get("id", "")))
        # 按供应商分组（树形纯展示）：父节点 = 供应商 ID，子节点 = 模型
        by_prov = {}
        for m in models:
            by_prov.setdefault(m.get("provider", ""), []).append(m)
        for pid in sorted(by_prov, key=lambda x: self._sort_key_models(x)):
            parent = "prov:" + str(pid)
            self.model_tree.insert("", "end", iid=parent,
                                   open=self._tree_open_state("models", parent),
                                   text="%s（%d）" % (pid, len(by_prov[pid])))
            self._model_parent_nodes.add(parent)
            for m in by_prov[pid]:
                prov = self._prov_by_id(m.get("provider"))
                conc = m.get("max_concurrency") or (prov.get("max_concurrency") if prov else 16)
                fb = m.get("fallback") or []
                fbtxt = "%d 跳" % len(fb) if fb else "无"
                self.model_tree.insert(parent, "end", iid=m["id"], text="", values=(
                    m.get("id"), m.get("upstream_model"), conc, fbtxt))

    def _refresh_model_provider_opts(self):
        pass  # 模型对话框动态读取供应商，无需缓存

    def _prov_by_id(self, pid):
        return next((p for p in self.cfg["providers"] if p["id"] == pid), None)

    def _sel_model(self):
        sel = self.model_tree.selection()
        if not sel:
            return None
        # 选中供应商父节点时视为未选中模型
        if sel[0] in getattr(self, "_model_parent_nodes", ()):
            return None
        return sel[0]

    def _model_dialog(self, title, initial=None):
        """模型编辑对话框，含失败后流转链编辑 + 批量拉取入库（多模型同屏编辑）。
        返回 (result, batch_done) 或 None。"""
        initial = initial or {}
        provs = self.cfg.get("providers", [])
        if not provs:
            messagebox.showwarning("提示", "请先在「供应商」区添加供应商，再来注册模型")
            return None

        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        result = {}
        cancelled = {"v": True}
        # 批量入库完成后置 True：外层 add_model 据此跳过单条添加
        batch_done = {"v": False}

        body = ttk.Frame(dlg)
        body.pack(padx=16, pady=12, fill="both", expand=True)
        prov_ids_sorted = sorted([p["id"] for p in provs],
                                 key=lambda x: self._sort_key_models(x))

        # ---- 顶部：供应商（两种模式共用） ----
        top = ttk.Frame(body)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="供应商", width=22).pack(side="left")
        default_prov = str(initial.get("provider", prov_ids_sorted[0]))
        v_prov = tk.StringVar(value=default_prov)
        prov_cb = ttk.Combobox(top, textvariable=v_prov, state="readonly",
                               values=prov_ids_sorted, width=28)
        prov_cb.pack(side="left", padx=(6, 0))

        # ---- 单条表单区 ----
        single = ttk.Frame(body)
        single.pack(fill="both", expand=True)

        row = ttk.Frame(single)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="本地模型名（自定义，唯一）", width=22).pack(side="left")
        default_id = str(initial.get("id", ""))
        if not default_id:
            default_id = default_prov + "/"
        v_id = tk.StringVar(value=default_id)
        ttk.Entry(row, textvariable=v_id, width=30).pack(side="left", padx=(6, 0))

        # 切换供应商时，若本地模型名仍是"纯前缀"（未被用户自定义），则联动更新前缀
        def _on_prov_change(*_):
            cur_id = v_id.get()
            if cur_id in ([p["id"] + "/" for p in provs] + [p["id"] for p in provs] + [""]):
                v_id.set(v_prov.get() + "/")
        prov_cb.bind("<<ComboboxSelected>>", _on_prov_change)

        row = ttk.Frame(single)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="上游真实模型 ID", width=22).pack(side="left")
        v_up = tk.StringVar(value=str(initial.get("upstream_model", "")))
        ttk.Entry(row, textvariable=v_up, width=30).pack(side="left", padx=(6, 0))
        ttk.Label(row, text="（配置了时段规则时可留空）", foreground="#888"
                  ).pack(side="left", padx=(4, 0))
        # 新增时提供「从供应商拉取模型列表」入口（编辑对话框不显示）
        if not initial.get("id"):
            ttk.Button(row, text="从供应商拉取模型列表…",
                       command=lambda: self._batch_pick_models(dlg, v_prov, _show_batch)
                       ).pack(side="left", padx=(8, 0))

        row = ttk.Frame(single)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="并发数（留空=继承供应商）", width=22).pack(side="left")
        v_conc = tk.StringVar(value=str(initial.get("max_concurrency", "")))
        ttk.Entry(row, textvariable=v_conc, width=10).pack(side="left", padx=(6, 0))

        # 失败后流转链编辑
        ttk.Separator(single).pack(fill="x", pady=8)
        ttk.Label(single, text="失败后流转链（按顺序尝试，最后一个即兜底，最多 %d 跳）"
                  % coerce_int(self.cfg["gateway"].get("fallback_max_hops"), 3),
                  font=("", 9, "bold")).pack(anchor="w")
        fb_frame = ttk.Frame(single)
        fb_frame.pack(fill="x")
        fb_rows = []

        def add_fb_row(prov="", up=""):
            maxh = coerce_int(self.cfg["gateway"].get("fallback_max_hops"), 3)
            if len(fb_rows) >= maxh:
                messagebox.showinfo("提示", "最多 %d 跳" % maxh)
                return
            row = ttk.Frame(fb_frame)
            row.pack(fill="x", pady=2)
            vp = tk.StringVar(value=prov or prov_ids_sorted[0])
            cb = ttk.Combobox(row, textvariable=vp, state="readonly",
                              values=prov_ids_sorted, width=16)
            cb.pack(side="left")
            vu = tk.StringVar(value=up)
            ttk.Entry(row, textvariable=vu, width=22).pack(side="left", padx=4)
            entry = {"frame": row, "prov": vp, "up": vu}

            def remove():
                fb_rows.remove(entry)
                row.destroy()
            ttk.Button(row, text="✕", width=3, command=remove).pack(side="left")
            fb_rows.append(entry)

        for fb in (initial.get("fallback") or []):
            add_fb_row(fb.get("provider", ""), fb.get("upstream_model", ""))
        add_fb_holder = ttk.Frame(single)
        add_fb_holder.pack(fill="x", pady=4)
        ttk.Button(add_fb_holder, text="+ 添加一跳", command=lambda: add_fb_row()
                   ).pack(side="left")

        # 按时间段自动换上游（本机时间；未命中任何时段用下方兜底模型）
        # 可选模型 = 已保存且有上游 ID 的本地模型（排除自己），选中即引用其 供应商+上游
        model_opts = []
        for mm in sorted(self.cfg.get("models", []),
                         key=lambda x: self._sort_key_models(x.get("id", ""))):
            if initial.get("id") and mm.get("id") == initial.get("id"):
                continue  # 不能引用自己
            up = str(mm.get("upstream_model") or "").strip()
            if not up:
                continue  # 无上游的（纯时段）模型不列入
            model_opts.append({"id": mm["id"], "provider": mm.get("provider"),
                               "upstream": up})
        model_opt_ids = [o["id"] for o in model_opts]
        model_opt_map = {o["id"]: o for o in model_opts}
        hours = ["%02d" % h for h in range(24)]
        minutes = ["%02d" % m for m in range(60)]

        def _model_for(prov, up):
            """回填：按 供应商+上游 反查已保存模型 id（找不到返回空）。"""
            for o in model_opts:
                if o["provider"] == prov and o["upstream"] == up:
                    return o["id"]
            return ""

        ttk.Separator(single).pack(fill="x", pady=8)
        ttk.Label(single, text="按时间段自动换上游（本机时间；未命中任何时段用下方兜底）",
                  font=("", 9, "bold")).pack(anchor="w")
        # 兜底模型：未命中任何时段规则时使用（可留空=未命中时段该模型不可用）
        fb_row = ttk.Frame(single)
        fb_row.pack(fill="x", pady=2)
        dr = initial.get("default_route") or {}
        v_fb_model = tk.StringVar(value=_model_for(dr.get("provider"), dr.get("upstream_model")))
        ttk.Label(fb_row, text="兜底（未命中时段用）").pack(side="left")
        ttk.Combobox(fb_row, textvariable=v_fb_model, values=model_opt_ids,
                     state="readonly", width=20).pack(side="left", padx=(6, 0))
        tr_frame = ttk.Frame(single)
        tr_frame.pack(fill="x")
        tr_rows = []  # [{sh,sm,eh,em,hint,model}]

        def _split_hhmm(v, dh="00", dm="00"):
            s = str(v or "").strip()
            if _is_hhmm(s):
                h, m_ = s.split(":")
                return h.zfill(2), m_.zfill(2)
            return dh, dm

        def add_tr_row(start="", end="", prov="", up=""):
            row = ttk.Frame(tr_frame)
            row.pack(fill="x", pady=2)
            sh, sm = _split_hhmm(start)
            eh, em = _split_hhmm(end)
            v_sh, v_sm = tk.StringVar(value=sh), tk.StringVar(value=sm)
            v_eh, v_em = tk.StringVar(value=eh), tk.StringVar(value=em)
            v_model = tk.StringVar(value=_model_for(prov, up))
            ttk.Label(row, text="每天").pack(side="left")
            ttk.Combobox(row, textvariable=v_sh, values=hours, state="readonly",
                         width=3).pack(side="left", padx=(2, 0))
            ttk.Label(row, text=":").pack(side="left")
            ttk.Combobox(row, textvariable=v_sm, values=minutes, state="readonly",
                         width=3).pack(side="left")
            ttk.Label(row, text="到").pack(side="left", padx=(4, 4))
            ttk.Combobox(row, textvariable=v_eh, values=hours, state="readonly",
                         width=3).pack(side="left")
            ttk.Label(row, text=":").pack(side="left")
            ttk.Combobox(row, textvariable=v_em, values=minutes, state="readonly",
                         width=3).pack(side="left")
            ttk.Label(row, text="用").pack(side="left", padx=(4, 2))
            ttk.Combobox(row, textvariable=v_model, values=model_opt_ids,
                         state="readonly", width=20).pack(side="left")
            # 语义提示：自动判断当天/跨夜并展示，替代人工勾选「第二天」
            hint = ttk.Label(row, text="", width=26, foreground="#888")
            hint.pack(side="left", padx=(6, 0))
            entry = {"sh": v_sh, "sm": v_sm, "eh": v_eh, "em": v_em,
                     "hint": hint, "model": v_model, "frame": row}

            def _update_hint(*_):
                s = "%s:%s" % (v_sh.get(), v_sm.get())
                e = "%s:%s" % (v_eh.get(), v_em.get())
                if s < e:
                    hint.configure(text="（当天 %s 到 %s）" % (s, e), foreground="#888")
                elif s > e:
                    hint.configure(text="（%s 跨夜到次日 %s）" % (s, e), foreground="#2E7D32")
                else:
                    hint.configure(text="（起止相同，无有效时段！）", foreground="#C62828")

            for v in (v_sh, v_sm, v_eh, v_em):
                v.trace_add("write", _update_hint)
            _update_hint()

            def remove():
                tr_rows.remove(entry)
                row.destroy()
            ttk.Button(row, text="✕", width=3, command=remove).pack(side="left", padx=4)
            tr_rows.append(entry)

        for tr in (initial.get("time_routes") or []):
            add_tr_row(tr.get("start", ""), tr.get("end", ""),
                       tr.get("provider", ""), tr.get("upstream_model", ""))
        add_tr_holder = ttk.Frame(single)
        add_tr_holder.pack(fill="x", pady=4)
        ttk.Button(add_tr_holder, text="+ 添加时间段规则", command=lambda: add_tr_row()
                   ).pack(side="left")
        if not model_opt_ids:
            ttk.Label(single, text="（还没有可引用的模型：请先保存至少一个带上游 ID 的模型）",
                      foreground="#C67C00").pack(anchor="w")

        # ---- 批量列表区（初始隐藏，选定模型后切换显示） ----
        batch = ttk.Frame(body)
        batch_vars = []  # [{up, var}] 批量模式的行数据

        # ---- 底部按钮 ----
        btns = ttk.Frame(dlg)
        btns.pack(pady=(0, 12))

        def ok():
            # 批量模式：多个模型同屏，自定义本地名后一次性入库
            if batch_vars:
                pid = v_prov.get()
                locals_ = [(b["var"].get().strip() or ("%s/%s" % (pid, b["up"])))
                           for b in batch_vars]
                seen, dups = set(), []
                for l in locals_:
                    if l in seen:
                        dups.append(l)
                    seen.add(l)
                if dups:
                    messagebox.showwarning("有重复", "以下本地名重复，请修改：\n" + "、".join(dups))
                    return
                conflict = [l for l in locals_ if any(m["id"] == l for m in self.cfg["models"])]
                if conflict:
                    messagebox.showerror("已存在", "以下本地名已存在，请修改：\n" + "、".join(conflict))
                    return
                for b, l in zip(batch_vars, locals_):
                    self.cfg["models"].append({"id": l, "provider": pid,
                                               "upstream_model": b["up"], "fallback": []})
                self._persist_and_refresh("批量添加模型 %d 个（供应商 %s）"
                                          % (len(batch_vars), pid))
                self._show_toast("已添加 %d 个模型" % len(batch_vars))
                self._log("批量添加：%s" % "、".join(locals_))
                cancelled["v"] = False
                batch_done["v"] = True
                dlg.destroy()
                return
            # ---- 单条模式 ----
            mid = v_id.get().strip()
            if not mid:
                messagebox.showerror("提示", "本地模型名不能为空")
                return
            if not initial.get("id") and any(m["id"] == mid for m in self.cfg["models"]):
                messagebox.showerror("提示", "本地模型名 %s 已存在" % mid)
                return
            # 时间段规则校验（引用已保存模型；未选模型的空行忽略）
            # 跨夜不再由用户勾选：start>end 自动按「跨夜到次日」处理
            time_routes = []
            for tr in tr_rows:
                s = "%s:%s" % (tr["sh"].get(), tr["sm"].get())
                e = "%s:%s" % (tr["eh"].get(), tr["em"].get())
                mid_sel = tr["model"].get().strip()
                if not mid_sel:
                    continue  # 未选模型，忽略该行
                o = model_opt_map.get(mid_sel)
                if o is None or self._prov_by_id(o["provider"]) is None:
                    messagebox.showerror("提示", "时间段 %s-%s 引用的模型无效，请重新选择" % (s, e))
                    return
                if s == e:
                    messagebox.showerror("提示", "时间段起止相同：%s，请调整" % s)
                    return
                time_routes.append({"start": s, "end": e,
                                    "provider": o["provider"],
                                    "upstream_model": o["upstream"]})
            # 兜底模型校验（未命中任何时段时使用；可留空）
            default_route = None
            fb_sel = v_fb_model.get().strip()
            if fb_sel:
                o = model_opt_map.get(fb_sel)
                if o is None or self._prov_by_id(o["provider"]) is None:
                    messagebox.showerror("提示", "兜底模型引用无效，请重新选择")
                    return
                default_route = {"provider": o["provider"],
                                 "upstream_model": o["upstream"]}
            if not v_up.get().strip() and not time_routes and not default_route:
                messagebox.showerror(
                    "提示", "上游真实模型 ID 不能为空（留空请至少配置一条时间段规则或兜底模型）")
                return
            result.update({
                "id": mid, "provider": v_prov.get(),
                "upstream_model": v_up.get().strip(),
                "fallback": [{"provider": e["prov"].get(), "upstream_model": e["up"].get().strip()}
                             for e in fb_rows if e["up"].get().strip()]})
            if time_routes:
                result["time_routes"] = time_routes
            if default_route:
                result["default_route"] = default_route
            c = v_conc.get().strip()
            result["max_concurrency"] = coerce_int(c, 0) if c else None
            cancelled["v"] = False
            dlg.destroy()

        def _show_batch(up_streams):
            """多选确认后回调：把选中模型回填到本对话框，切换到批量模式。"""
            pid = v_prov.get()
            # 批量模式下供应商由拉取决定，锁定避免与模型错配
            prov_cb.configure(state="disabled")
            single.pack_forget()
            batch.pack(fill="both", expand=True)
            for w in batch.winfo_children():
                w.destroy()
            batch_vars.clear()
            head = ttk.Frame(batch)
            head.pack(fill="x", pady=(0, 4))
            ttk.Label(head, text="已选 %d 个模型，请在上游 ID 后自定义本地模型名："
                      % len(up_streams), font=("", 9, "bold")).pack(side="left")
            wrap = ttk.Frame(batch)
            wrap.pack(fill="both", expand=True)
            cvs = tk.Canvas(wrap, highlightthickness=0)
            inner = ttk.Frame(cvs)
            inner.bind("<Configure>", lambda e: cvs.configure(scrollregion=cvs.bbox("all")))
            cvs.create_window((0, 0), window=inner, anchor="nw")
            vsb = ttk.Scrollbar(wrap, orient="vertical", command=cvs.yview)
            cvs.configure(yscrollcommand=vsb.set, width=500)
            cvs.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            hd = ttk.Frame(inner)
            hd.pack(fill="x", pady=(0, 4))
            ttk.Label(hd, text="上游模型 ID（只读）", width=26,
                      font=("", 9, "bold")).pack(side="left", padx=(0, 8))
            ttk.Label(hd, text="本地模型名（可编辑）", width=30,
                      font=("", 9, "bold")).pack(side="left")
            for up in up_streams:
                rw = ttk.Frame(inner)
                rw.pack(fill="x", pady=2)
                ttk.Label(rw, text=up, width=26, foreground="#333",
                          wraplength=180).pack(side="left", padx=(0, 8))
                var = tk.StringVar(value="%s/%s" % (pid, up))
                ttk.Entry(rw, textvariable=var, width=30).pack(side="left")
                batch_vars.append({"up": up, "var": var})
            dlg.grab_set()

        ttk.Button(btns, text="确定", command=ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left")
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + 60
        y = self.root.winfo_rooty() + 40
        dlg.geometry("+%d+%d" % (x, y))
        dlg.wait_window()
        return (None, False) if cancelled["v"] else (result, batch_done["v"])

    def add_model(self):
        ret = self._model_dialog("新增模型")
        if not ret:
            return
        res, batch_done = ret
        if not res:
            return
        if batch_done:
            return  # 批量拉取流程已完成入库，跳过单条添加
        if res["max_concurrency"] is None:
            res.pop("max_concurrency")
        self.cfg["models"].append(res)
        self._persist_and_refresh("已添加模型 %s" % res["id"])

    # ---- 批量拉取供应商模型列表 ----
    def _batch_pick_models(self, parent_dlg, v_prov, on_picked):
        """「新增模型」对话框中的批量拉取入口：
        ① 后台线程 GET /v1/models → ② 多选弹窗（搜索 + 已添加置灰）→
        ③ 把选中模型回调给 on_picked（由 _model_dialog 的 _show_batch 回填、切换批量模式）。"""
        pid = v_prov.get()
        prov = self._prov_by_id(pid)
        if prov is None:
            messagebox.showerror("提示", "供应商不存在")
            return
        if not str(prov.get("base_url", "")).startswith("http"):
            messagebox.showerror("提示", "该供应商未填写有效的上游地址")
            return
        try:
            if not parent_dlg.winfo_exists():
                return
            parent_dlg.grab_release()
        except Exception:
            pass
        # 后台线程拉取，界面不卡
        worker = threading.Thread(target=self._fetch_models_worker,
                                  args=(prov, lambda ms: self._after_models_fetched(
                                      prov, ms, on_picked, parent_dlg)),
                                  daemon=True)
        worker.start()

    def _fetch_models_worker(self, prov, on_done):
        """后台线程：向供应商发 GET /v1/models，返回模型 ID 列表或 (None, 错误信息)。"""
        import urllib.request as u
        import urllib.error
        url = _upstream_url(prov["base_url"], "models")
        headers = {"Content-Type": "application/json"}
        if prov.get("api_key"):
            headers["Authorization"] = "Bearer " + prov["api_key"]
        try:
            req = u.Request(url, headers=headers, method="GET")
            resp = u.urlopen(req, timeout=30)
            data = json.loads(resp.read().decode("utf-8", "ignore") or "{}")
            resp.close()
            ids = [str(x.get("id")) for x in data.get("data", []) if x.get("id")]
            ids = sorted(set(ids), key=lambda x: self._sort_key_models(x))
            on_done(ids or None)
            return
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                err_body = ""
            info = "HTTP %d %s" % (e.code, err_body)
        except Exception as e:
            info = str(e)
        # 回到主线程报错并恢复对话框焦点
        self.root.after(0, lambda: self._fetch_failed(prov, info, None))

    def _fetch_failed(self, prov, info, _unused):
        messagebox.showerror(
            "获取失败",
            "无法从 %s 获取模型列表：\n%s\n\n"
            "该供应商可能未开放模型列表接口，请改用下方手动填写。" % (prov.get("id"), info))
        self._log("拉取 %s 模型列表失败：%s" % (prov.get("id"), info))

    def _after_models_fetched(self, prov, model_ids, on_picked, parent_dlg):
        """拉取成功后（主线程）：弹多选窗口 → 把选中模型回调给 on_picked。"""
        if not model_ids:
            self._fetch_failed(prov, "返回列表为空", None)
            return
        existing = {m.get("upstream_model") for m in self.cfg["models"]
                    if m.get("provider") == prov["id"]}
        picked = self._select_models_dialog(prov, model_ids, existing)
        if not picked:
            return
        # 把选中模型交给 _model_dialog 的批量模式回填
        on_picked(picked)

    def _select_models_dialog(self, prov, model_ids, existing):
        """多选弹窗：复选框列表 + 搜索过滤 + 已添加置灰不可选。返回勾选列表。
        性能优化：所有行一次性预创建，搜索只做 pack/pack_forget 显隐切换；
        输入加 120ms 去抖，避免每敲一键就整屏重建导致卡顿。"""
        win = tk.Toplevel(self.root)
        win.title("选择要添加的模型 · %s（共 %d 个，已添加 %d 个）"
                  % (prov.get("id"), len(model_ids), len(existing & set(model_ids))))
        win.transient(self.root)
        win.grab_set()
        picked, cancelled = [], {"v": True}

        top = ttk.Frame(win)
        top.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(top, text="搜索过滤：").pack(side="left")
        v_filter = tk.StringVar()
        ent = ttk.Entry(top, textvariable=v_filter, width=36)
        ent.pack(side="left", padx=4, ipady=2)
        v_match = ttk.Label(top, text="匹配 %d / %d 个" % (len(model_ids), len(model_ids)))
        v_match.pack(side="left", padx=(10, 0))
        ttk.Label(top, text="全选/全不选").pack(side="left", padx=(16, 4))
        v_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, variable=v_all, text="",
                        command=lambda: _toggle_all()).pack(side="left")

        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=12, pady=6)
        cvs = tk.Canvas(wrap, highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=cvs.yview)
        inner = ttk.Frame(cvs)
        inner.bind("<Configure>", lambda e: cvs.configure(
            scrollregion=cvs.bbox("all")))
        cvs.create_window((0, 0), window=inner, anchor="nw")
        cvs.configure(yscrollcommand=vsb.set)
        cvs.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        cvs.configure(width=420)

        # 一次性预创建所有行（含勾选状态），搜索时只显隐
        rows = []  # [{var, up, added, row, cb, lbl}]
        for up in model_ids:
            row = ttk.Frame(inner)
            is_added = up in existing
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(row, variable=var)
            cb.pack(side="left")
            if is_added:
                cb.configure(state="disabled")
                lbl = ttk.Label(row, text=up, foreground="#999")
                tag = ttk.Label(row, text="（已添加）", foreground="#999")
            else:
                lbl = ttk.Label(row, text=up)
                tag = None
            lbl.pack(side="left")
            if tag:
                tag.pack(side="left")
            rows.append({"var": var, "up": up, "added": is_added,
                         "row": row})

        def _apply_filter(*_):
            ft = v_filter.get().strip().lower()
            shown = 0
            for r_ in rows:
                hit = (not ft) or (ft in r_["up"].lower())
                if hit:
                    r_["row"].pack(fill="x", pady=1)
                    shown += 1
                else:
                    r_["row"].pack_forget()
            v_match.configure(text="匹配 %d / %d 个" % (shown, len(rows)))
            cvs.configure(scrollregion=cvs.bbox("all"))
            cvs.yview_moveto(0)

        def _toggle_all():
            target = v_all.get()
            ft = v_filter.get().strip().lower()
            for r_ in rows:
                if r_["added"]:
                    continue
                # 全选只作用于当前可见（未过滤掉）的项
                if ft and ft not in r_["up"].lower():
                    continue
                r_["var"].set(target)

        _debounce = {"id": None}

        def _debounced_filter(*_):
            if _debounce["id"] is not None:
                self.root.after_cancel(_debounce["id"])
            _debounce["id"] = self.root.after(120, _apply_filter)

        v_filter.trace_add("write", _debounced_filter)
        # 初始渲染：全部显示
        _apply_filter()

        def _ok():
            picked.extend([r_["up"] for r_ in rows if r_["var"].get()])
            cancelled["v"] = False
            win.destroy()

        btns = ttk.Frame(win)
        btns.pack(pady=(4, 12))
        ttk.Button(btns, text="下一步", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left")
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + 80
        win.geometry("+%d+%d" % (x, y))
        ent.focus_set()
        win.wait_window()
        return [] if cancelled["v"] else picked

    def edit_model(self):
        mid = self._sel_model()
        if not mid:
            messagebox.showwarning("提示", "请先选中要编辑的模型")
            return
        m = next((x for x in self.cfg["models"] if x["id"] == mid), None)
        if m is None:
            return
        ret = self._model_dialog("编辑模型", initial=m)
        if not ret:
            return
        res, _ = ret
        if not res:
            return
        if res.get("max_concurrency") is None:
            res.pop("max_concurrency", None)
            m.pop("max_concurrency", None)
        if not res.get("time_routes"):
            res.pop("time_routes", None)
            m.pop("time_routes", None)  # 编辑时删光时间段规则 → 同步移除
        if not res.get("default_route"):
            res.pop("default_route", None)
            m.pop("default_route", None)  # 编辑时删掉兜底模型 → 同步移除
        m.update(res)
        self._persist_and_refresh("已更新模型 %s" % mid)

    def del_model(self):
        mid = self._sel_model()
        if not mid:
            return
        if messagebox.askyesno("确认", "删除模型 %s？" % mid):
            self.cfg["models"] = [m for m in self.cfg["models"] if m["id"] != mid]
            self._persist_and_refresh("已删除模型 %s" % mid)

    # ---- 网关设置 ----
    def _build_gateway_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text=" 网关设置 ")
        lf = ttk.LabelFrame(tab, text="本地网关")
        lf.pack(fill="x", padx=12, pady=12)
        self.gvars = {}
        rows = [("port", "本地监听端口"),
                ("api_key", "网关 API Key（客户端统一填这个，留空=不鉴权）"),
                ("request_timeout", "单请求超时(秒)"),
                ("queue_timeout", "排队超时(秒)"),
                ("transfer_wait_seconds", "流转等待阈值(秒)"),
                ("fallback_max_hops", "失败后流转最大跳数")]
        for i, (key, label) in enumerate(rows):
            ttk.Label(lf, text=label).grid(row=i, column=0, sticky="w", padx=12, pady=5)
            var = tk.StringVar()
            ttk.Entry(lf, textvariable=var, width=40).grid(row=i, column=1, sticky="w",
                                                           padx=12, pady=5)
            self.gvars[key] = var
        r = len(rows)
        self.auto_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(lf, text="开机自动启动（并最小化到托盘）", variable=self.auto_var,
                        command=self._toggle_autostart).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 2))
        r += 1
        self.lan_var = tk.BooleanVar(
            value=bool(self.cfg["gateway"].get("lan_access", False)))
        ttk.Checkbutton(lf, text="允许局域网设备访问（手机/其他电脑可连本网关）",
                        variable=self.lan_var).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=2)
        r += 1
        self.lan_hint_var = tk.StringVar(value="")
        ttk.Label(lf, textvariable=self.lan_hint_var, foreground="#185FA5",
                  wraplength=560, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(2, 4))
        r += 1
        ttk.Button(lf, text="放行防火墙 8787（首次局域网访问需执行一次）",
                   command=self._open_firewall).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(2, 8))
        ttk.Button(tab, text="保存网关设置", command=self.save_gateway_form).pack(
            anchor="w", padx=12, pady=6)
        hint = ("本机客户端（WorkBuddy 等）配置：\n"
                "① 接口地址填  http://127.0.0.1:<端口>/v1\n"
                "② 模型名填「模型」区注册的本地别名\n"
                "③ API Key 填上面设置的「网关 API Key」（留空则随便填即可）\n"
                "真实供应商密钥只保存在本网关，客户端无需知道。")
        ttk.Label(tab, text=hint, foreground="#666", justify="left").pack(
            anchor="w", padx=12, pady=4)

    def _get_lan_ip(self):
        """探测本机局域网 IP。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        try:
            ips = socket.gethostbyname_ex(socket.gethostname())[2]
            for ip in ips:
                if not ip.startswith("127."):
                    return ip
        except Exception:
            pass
        return "127.0.0.1"

    def _update_lan_hint(self):
        """根据勾选状态刷新局域网地址提示。"""
        if self.lan_var.get():
            ip = self._get_lan_ip()
            port = coerce_int(self.gvars["port"].get(), 8787)
            if ip and not ip.startswith("127."):
                self.lan_hint_var.set(
                    "局域网访问地址：http://%s:%d/v1  （手机 ChatBox / 其他电脑 Cherry "
                    "Studio 的接口地址填这个，API Key 填上面的网关 Key）" % (ip, port))
            else:
                self.lan_hint_var.set("未检测到局域网 IP，请确认电脑已连接网络。")
        else:
            self.lan_hint_var.set("仅本机可访问（127.0.0.1）。")

    def _open_firewall(self):
        """请求放行防火墙端口（触发 Windows UAC 提权）。"""
        port = coerce_int(self.gvars["port"].get(), 8787)
        try:
            res = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "netsh",
                'advfirewall firewall add rule name="ModelProxy" dir=in '
                'action=allow protocol=TCP localport=%d' % port,
                None, 1)
            if res <= 32:
                messagebox.showwarning(
                    "提示",
                    "可能未授权或失败。请手动放行：\n"
                    "控制面板 → Windows Defender 防火墙 → 高级设置 → 入站规则\n"
                    "→ 新建规则 → 端口 → TCP %d → 允许连接。" % port)
            else:
                self._log("已请求放行防火墙 TCP %d（请在 UAC 弹窗点“是”）" % port)
                self._show_toast("已请求放行防火墙 %d" % port)
        except Exception as e:
            messagebox.showerror("提示", "操作失败：%s" % e)

    def _apply_gateway_form(self):
        gw = self.cfg["gateway"]
        for k, var in self.gvars.items():
            var.set(str(gw.get(k, "")))
        self.lan_var.set(bool(gw.get("lan_access", False)))
        self._update_lan_hint()

    def save_gateway_form(self, silent=False):
        gw = self.cfg["gateway"]
        for k, var in self.gvars.items():
            gw[k] = var.get().strip()
        for k in ("port", "request_timeout", "queue_timeout",
                  "transfer_wait_seconds", "fallback_max_hops"):
            gw[k] = coerce_int(gw[k], default_gateway_default(k))
        gw["lan_access"] = bool(self.lan_var.get())
        errs = validate_config(self.cfg)
        if errs:
            messagebox.showerror("配置有误", "\n".join(errs))
            return False
        path = save_config(self.cfg)
        self.gw.reload_config(self.cfg)
        self._update_lan_hint()
        self._status("网关设置已保存到 " + path, "#2E7D32")
        if not silent:
            need_restart = self.gw.running
            tips = []
            if gw["lan_access"]:
                tips.append("局域网访问需重启网关后生效，且需放行防火墙端口。")
                if self.gw.running:
                    tips.append("请先点「停止网关」再「启动网关」。")
            elif need_restart:
                tips.append("修改端口需停止并重新启动网关才生效。")
            if tips:
                messagebox.showinfo("提示", "\n".join(tips))
        return True

    # ---- 运行日志 ----
    def _build_log_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text=" 运行 ")
        self.stat_var = tk.StringVar(value="成功 0 · 重试 0 · 流转 0 · 失败 0 · 在途 0")
        ttk.Label(tab, textvariable=self.stat_var).pack(fill="x", padx=8, pady=(8, 2))
        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_txt = tk.Text(wrap, font=("Consolas", 9), state="disabled", wrap="word")
        sb = ttk.Scrollbar(wrap, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        ttk.Button(tab, text="清空日志", command=self._clear_log).pack(anchor="e",
                                                                      padx=8, pady=(0, 8))

    def _clear_log(self):
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")

    # ---- 关于 ----
    def _build_about_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text=" 关于 ")
        # Logo
        logo = None
        for p in (_resource_paths("Logo.png") + _resource_paths("tray_icon.png")):
            if os.path.exists(p):
                try:
                    logo = Image.open(p).convert("RGBA")
                    logo.thumbnail((96, 96), Image.LANCZOS)
                    break
                except Exception:
                    logo = None
        if logo is not None:
            self._about_photo = ImageTk.PhotoImage(logo)
            ttk.Label(tab, image=self._about_photo).pack(pady=(20, 4))
        ttk.Label(tab, text="ModelProxy", font=("Microsoft YaHei", 16, "bold")
                  ).pack(pady=(8, 2))
        ttk.Label(tab, text="本地多平台模型路由网关",
                  font=("Microsoft YaHei", 10), foreground="#666").pack()
        sep = ttk.Separator(tab)
        sep.pack(fill="x", padx=40, pady=10)

        def _row(text, font_opt=("Microsoft YaHei", 10), wrap=520):
            return ttk.Label(tab, text=text, font=font_opt, wraplength=wrap,
                             justify="center").pack(pady=2)

        _row("软件版本：v1.93", ("Microsoft YaHei", 11, "bold"))
        _row("")
        # 关于页文案：断行点在顿号后（实测行1=616px/行2=519px，均<660），完整模型名不截断
        _row("本软件由 Zhiyu 许愿式开发，由 WorkBuddy 驱动 Claude Fable 5.1、"
             "DeepSeek v4 Flash、DeepSeek v4 Pro、\n"
             "GLM 5.3、GLM 5.3 Flash、GPT 5.6 Sol、GPT Image 2、"
             "Hy4 Preview、Kimi k3 等模型构成。",
             ("Microsoft YaHei", 9), wrap=660)

    # ---------------- 启停 ----------------
    def start_gateway(self):
        if not self.save_gateway_form(silent=True):
            return
        ok, msg = self.gw.start()
        if ok:
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self._status("运行中 · " + msg, "#2E7D32")
            self._log("=== 网关已启动 ===")
        else:
            self._status("启动失败", "#C62828")
            self._log("启动失败：" + msg)
            messagebox.showerror("启动失败", msg)

    def stop_gateway(self):
        self.gw.stop()
        save_config(self.cfg)  # 停止时把累计 token 落盘
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._status("未运行", "#666666")
        self._log("=== 网关已停止 ===")

    # ---------------- 测试（后台线程） ----------------
    def test_model(self):
        if self._test_running:
            return
        mid = self._sel_model()
        if not mid:
            messagebox.showwarning("提示", "请先选中要测试的模型")
            return
        m = next((x for x in self.cfg["models"] if x["id"] == mid), None)
        prov = self._prov_by_id(m.get("provider"))
        if not prov:
            messagebox.showerror("提示", "该模型的供应商不存在")
            return
        self._test_running = True
        self._test_name = mid
        self._spin()
        threading.Thread(target=self._test_worker, args=(m, prov), daemon=True).start()

    def _spin(self):
        if not self._test_running:
            return
        self._spin_idx = (self._spin_idx + 1) % 4
        self.status_var.set("测试中 %s · %s" % ("|/-\\"[self._spin_idx], self._test_name))
        self.status_lbl.configure(fg="#185FA5")
        self.root.after(150, self._spin)

    def _test_worker(self, m, prov):
        import urllib.request as u
        import urllib.error
        url = _upstream_url(prov["base_url"], "chat/completions")
        body = json.dumps({"model": m["upstream_model"],
                           "messages": [{"role": "user", "content": "好"}],
                           "max_tokens": 1}).encode()
        headers = {"Content-Type": "application/json"}
        if prov.get("api_key"):
            headers["Authorization"] = "Bearer " + prov["api_key"]
        req = u.Request(url, data=body, headers=headers)
        try:
            r = u.urlopen(req, timeout=60)
            result = ("ok", r.status)
        except urllib.error.HTTPError as e:
            result = ("http", e.code)
        except Exception as e:
            result = ("err", str(e))
        self.root.after(0, lambda: self._test_done(m, prov, result))

    def _test_done(self, m, prov, result):
        self._test_running = False
        # 立即恢复顶部状态栏（不再依赖 _poll 轮询，避免"测试中"残留）
        self._refresh_statusbar()
        kind, info = result
        if kind == "ok":
            self._log("测试 %s → %s/%s：HTTP %d ✓" % (m["id"], prov["id"],
                                                      m["upstream_model"], info))
            messagebox.showinfo("测试结果", "模型 %s 连接成功（HTTP %d）。" % (m["id"], info))
        elif kind == "http":
            self._log("测试 %s：HTTP %d ✗" % (m["id"], info))
            messagebox.showwarning("测试结果", "上游返回 HTTP %d。" % info)
        else:
            self._log("测试 %s：失败 %s" % (m["id"], info))
            messagebox.showerror("测试结果", "连接失败：%s" % info)

    def _refresh_statusbar(self):
        """根据网关运行状态刷新顶部状态栏。"""
        try:
            s = self.gw.snapshot()
            if s["running"]:
                self.status_var.set("运行中 · 127.0.0.1:%d" % s["port"])
                self.status_lbl.configure(fg="#2E7D32")
            else:
                self.status_var.set("未运行")
                self.status_lbl.configure(fg="#666666")
        except Exception:
            self.status_var.set("未运行")
            self.status_lbl.configure(fg="#666666")

    # ---------------- 持久化 ----------------
    def _persist_and_refresh(self, logmsg):
        save_config(self.cfg)
        self.gw.reload_config(self.cfg)
        self._refresh_providers()
        self._refresh_models()
        self._log(logmsg + "（已写入 config.json）")

    # ---------------- 托盘 ----------------
    def _toggle_autostart(self):
        if set_autostart(self.auto_var.get()):
            self._log("开机自启：" + ("已开启" if self.auto_var.get() else "已关闭"))
        else:
            self.auto_var.set(autostart_enabled())
            messagebox.showwarning("提示", "设置开机自启失败")

    def _ensure_tray(self):
        if not HAS_TRAY:
            self._log("未安装 pystray/pillow，托盘不可用（不影响运行）")
            return
        try:
            image = self._make_icon()
            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", self._show_from_tray, default=True),
                pystray.MenuItem("启动网关", self._tray_start),
                pystray.MenuItem("停止网关", self._tray_stop),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self.quit_app))
            self.icon = pystray.Icon("model_proxy", image, APP_TITLE, menu)
            threading.Thread(target=self.icon.run, daemon=True).start()
        except Exception as e:
            self._log("托盘启动失败：%s" % e)

    def _make_icon(self):
        # 优先使用 Logo 生成的托盘图标，失败则回退到代码画。
        # 注意：不能在此函数内 from PIL import Image（会把 Image 变成函数局部变量，
        # 图标文件不存在时走到 fallback 会 UnboundLocalError），统一用模块级导入。
        for p in (_resource_paths("tray_icon.png") + _resource_paths("Logo.png")):
            if os.path.exists(p):
                try:
                    img = Image.open(p).convert("RGBA")
                    img.thumbnail((64, 64), Image.LANCZOS)
                    return img
                except Exception as e:
                    self._log("加载 Logo 失败：%s" % e)
        try:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((2, 2, 62, 62), fill="#534AB7", outline="#3C3489", width=2)
            d.ellipse((14, 14, 50, 50), fill="white")
            d.text((32, 32), "GW", fill="#534AB7", font=None, anchor="mm")
            return img
        except Exception:
            from PIL import Image as _ImgFallback  # 极低概率：模块级导入被污染时的最后兜底
            img = _ImgFallback.new("RGBA", (64, 64), (0, 0, 0, 0))
            return img

    def _hide_to_tray(self):
        if self.icon is not None:
            self.root.withdraw()
            self._log("已最小化到后台（托盘）。点托盘图标唤回。")
        else:
            self.root.iconify()

    def _show_from_tray(self, icon=None, item=None):
        self.root.after(0, self._show_win)

    def _show_win(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_start(self, icon=None, item=None):
        self.root.after(0, self.start_gateway)

    def _tray_stop(self, icon=None, item=None):
        self.root.after(0, self.stop_gateway)

    def quit_app(self, icon=None, item=None):
        try:
            self.gw.stop()
        except Exception:
            pass
        if self.icon is not None:
            self.icon.stop()
        self.root.after(100, self.root.destroy)

    # ---------------- 日志 / 状态 ----------------
    def _enqueue_log(self, msg):
        self.log_q.put("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

    def _log(self, msg):
        self._enqueue_log(msg)

    def _poll(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log_txt.configure(state="normal")
                self.log_txt.insert("end", msg + "\n")
                self.log_txt.see("end")
                self.log_txt.configure(state="disabled")
        except queue.Empty:
            pass
        if not self._test_running:
            try:
                s = self.gw.snapshot()
                if s["running"]:
                    self.stat_var.set(
                        "成功 %d · 重试 %d · 流转 %d · 失败 %d · 在途 %d"
                        % (s["done"], s["retried"], s["fallback"], s["failed"], s["inflight"]))
                    if s.get("lan"):
                        ip = self._get_lan_ip()
                        disp = "运行中 · 局域网 %s:%d" % (ip, s["port"]) \
                            if ip and not ip.startswith("127.") \
                            else "运行中 · 127.0.0.1:%d" % s["port"]
                    else:
                        disp = "运行中 · 127.0.0.1:%d" % s["port"]
                    self.status_var.set(disp)
                    self.status_lbl.configure(fg="#2E7D32")
                else:
                    self.stat_var.set(
                        "成功 %d · 重试 %d · 流转 %d · 失败 %d · 未运行"
                        % (s["done"], s["retried"], s["fallback"], s["failed"]))
                    self.status_var.set("未运行")
                    self.status_lbl.configure(fg="#666666")
            except Exception:
                pass
        # token 累计统计落盘（UI 已不展示 token；core 置脏标记，每 5 秒至多写一次盘）
        try:
            if self.gw.token_flush_pending() and \
               time.time() - getattr(self, "_token_save_time", 0) >= 5:
                self.gw.token_flush_done()
                self._token_save_time = time.time()
                save_config(self.cfg)
        except Exception:
            pass
        self.root.after(1000, self._poll)

    def _status(self, text, color="#666666"):
        self.status_var.set(text)
        self.status_lbl.configure(fg=color)


def default_gateway_default(key):
    return {"port": 8787, "request_timeout": 600, "queue_timeout": 600,
            "transfer_wait_seconds": 60, "fallback_max_hops": 3}.get(key, 0)


_MUTEX_NAME = "Global\\ModelProxy_SingleInstance_Mutex"


def _already_running():
    """用命名互斥量判断是否已有实例在运行。返回 True 表示已在运行。"""
    if not HAS_CTYPES:
        return False
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return False
        err = ctypes.windll.kernel32.GetLastError()
        if err == 183:  # ERROR_ALREADY_EXISTS
            return True
        # 拿到互斥量，需要保持引用防止被 GC 释放
        global _mutex_handle
        _mutex_handle = handle
        return False
    except Exception:
        return False


def main():
    if _already_running():
        # 已有一个实例在运行：弹出提示框，点确认后关闭本进程
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("ModelProxy", "ModelProxy 已经在运行中，无需重复打开。")
        root.destroy()
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    def _log_crash(exc_type, exc, tb):
        try:
            import traceback
            path = os.path.join(APP_DIR, "error.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write("=" * 60 + "\n")
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:
            pass
    try:
        main()
    except Exception:
        _log_crash(*sys.exc_info())
