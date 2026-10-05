# -*- coding: utf-8 -*-
"""游戏汉化工具 - 界面。双击「启动.bat」打开。"""
import os
import queue
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import cn_core as core  # noqa: E402
    import cn_unity as unity  # noqa: E402
except Exception:
    # 用 pythonw 启动没有黑框，代码出错的话窗口直接不出来、什么提示都没有：弹个框，再写进错误日志
    _err = traceback.format_exc()
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "错误日志.txt"), "a", encoding="utf-8") as _f:
            _f.write(_err + "\n")
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _w = _tk.Tk()
        _w.withdraw()
        _mb.showerror("游戏汉化工具启动失败", _err[-1500:])
    except Exception:
        pass
    raise SystemExit(1)

PROVIDERS = {
    "DeepSeek": ("https://api.deepseek.com", ["deepseek-flash", "deepseek-v4-pro"]),
    "其他（兼容 OpenAI 接口）": ("", []),
}
UNCHECKED, CHECKED = "☐", "☑"
ALL_MODELS = "全部（不分模型）"


# ============================================================== 后台任务

class Worker(object):
    def __init__(self, post):
        self.post = post  # post(kind, *args) 把消息丢回界面线程
        self.stop = threading.Event()
        self.thread = None
        self.usage = core.Usage()
        self.last_roots = []
        self.current = None
        self.logged = (0, 0, 0)   # 已经记进用量记录的 tokens
        self.last_game = ""
        self.last_cfg = None

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, fn, *args):
        if self.busy:
            return False
        self.stop = threading.Event()
        core.PAUSE.clear()
        self.thread = threading.Thread(target=self._run, args=(fn,) + args, daemon=True)
        self.thread.start()
        return True

    def _run(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            self.post("log", "出错了：\n" + traceback.format_exc())
        finally:
            core.PAUSE.clear()
            self.current = None
            self.post("idle")

    def log(self, s):
        self.post("log", s)

    def flush_late_usage(self):
        """停止后才回来的请求产生的 tokens，补记到上一个游戏名下"""
        now = (self.usage.hit, self.usage.miss, self.usage.out)
        if now != self.logged and self.last_game and self.last_cfg:
            core.log_usage(self.last_game + "（停止后回来的请求）", self.last_cfg, self.logged, now)
        self.logged = now

    # ---------------------------------------------------------- 状态
    def refresh(self, roots, cfg):
        for r in roots:
            if self.stop.is_set():
                break
            self.post("status", r, self.status_of(r))

    def status_of(self, r):
        kind = core.game_kind(r) if os.path.isdir(r) else None
        if kind == "unity":
            try:
                return unity.status(r)
            except Exception as e:
                return {"error": "读取失败：%s" % type(e).__name__}
        if kind != "renpy":
            return {"error": "找不到这个游戏了（被移动或删除）"}
        try:
            st = core.game_status(r)
        except core.DataFileError as e:
            self.log("%s：%s" % (os.path.basename(r), e))
            return {"error": "翻译记录文件坏了，详情看下面日志"}
        except Exception as e:
            return {"error": "读取失败：%s" % type(e).__name__}
        st["kind"] = "renpy"
        st["version"] = "Ren'Py " + st["version"]
        return st

    # ---------------------------------------------------------- 翻译 / 安装
    def translate(self, roots, cfg):
        self.flush_late_usage()
        self.last_roots = list(roots)
        for r in roots:
            self.post("live", r, "排队中")
        for r in roots:
            if self.stop.is_set():
                break
            self.current = r
            kind = core.game_kind(r) if os.path.isdir(r) else None
            title = core.game_title(r) if kind == "renpy" else (unity.unity_info(r) or {}).get("title", os.path.basename(r))
            self.log("")
            self.log("==== %s ====" % title)
            if kind == "unity":
                self._install_unity(r, cfg)
            elif kind == "renpy":
                ok = self._translate_renpy(r, title, cfg)
                self.post("status", r, self.status_of(r))
                if not ok:
                    break
            else:
                self.log("  找不到这个游戏了，跳过")
            self.post("status", r, self.status_of(r))
        for r in roots:
            self.post("live", r, None)
        self.log("")
        self.log("本次用量：" + self.usage.text())

    def _install_unity(self, r, cfg):
        self.post("live", r, "安装插件中")
        try:
            unity.install(r, cfg, self.log, self.stop)
        except core.StopRequested:
            self.log("  已停止。")
            return
        except Exception as e:
            self.log("  安装失败：%s" % e)
            return
        self.log("  翻译插件装好了。Unity 游戏是边玩边翻：点这一行的「启动」开始玩，玩的时候这个工具别关。")
        self.log("  游戏里按 Alt+T 可以在中文和原文之间切换。")

    def _translate_renpy(self, r, title, cfg):
        self.log("  读取游戏文本……")
        self.post("live", r, "读取中")
        before = (self.usage.hit, self.usage.miss, self.usage.out)
        keep_going, chars, disk_bad = True, 0, False
        try:
            n, total, failed, chars = core.translate_game(
                cfg, r, self.usage, self.stop, self.log,
                lambda a, b, rr=r, t=title: self.post("progress", rr, t, a, b))
        except core.AuthError as e:
            self.log("  接口拒绝了请求：%s" % e)
            self.post("error", "接口拒绝了请求：\n%s\n\n检查一下 key、余额、模型名。已翻好的部分都保存了。" % e)
            self.stop.set()
            keep_going = False
        except core.StopRequested:
            self.log("  已停止。翻好的部分都保存了，点「重启任务」或再点「翻译勾选的游戏」接着翻。")
            keep_going = False
        except core.ApiResponseError as e:
            self.log("  接口返回的格式不对，已停下（再试也一样，免得白花钱）：%s" % e)
            self.post("error", "接口返回的格式不对，已停下：\n%s\n\n多半是接口地址填错了，或者服务商那边出问题。已翻好的部分都保存了。" % e)
            self.stop.set()
            keep_going = False
        except core.SaveError as e:
            self.log("  %s。已经停下，没再发新请求。" % e)
            self.post("error", "%s\n\n已经停下，没再发新请求。已经翻回来的结果留在工具里（别关工具），"
                               "腾出硬盘空间 / 关掉占用文件的程序后点「重启任务」，会先把它们存上，不用重新花钱。" % e)
            self.stop.set()
            keep_going = False
            disk_bad = True
        except core.DataFileError as e:
            self.log("  %s" % e)
            self.post("error", str(e))
            self.stop.set()
            keep_going = False
        else:
            self.log("  翻好 %d/%d 条%s" % (n, total, "，%d 条始终没翻好，保留原文" % failed if failed else ""))
        after = (self.usage.hit, self.usage.miss, self.usage.out)
        try:
            core.log_usage(title, cfg, before, after)
        except Exception as e:
            self.log("  用量记录写不进去（%s）：%s" % (e, self.usage.text()))
        self.logged, self.last_game, self.last_cfg = after, title, dict(cfg)
        if chars:
            try:
                core.record_stats(cfg, chars, after[0] - before[0], after[1] - before[1], after[2] - before[2])
            except Exception:
                pass  # 估价统计写不进去无所谓
        if disk_bad:
            self.log("  硬盘写不进去，这次先不动游戏里的补丁（游戏里还是上次的版本，照样能玩）。")
            return keep_going
        try:
            m = core.install_patch(r, cfg, self.log)
            self.log("  中文补丁已装好（%d 条，字体：%s）。直接打开游戏就是中文。" % (m, core.font_display(cfg)))
        except Exception as e:
            self.log("  装补丁失败：%s" % e)
        return keep_going

    def restore(self, roots, cfg):
        for r in roots:
            kind = core.game_kind(r) if os.path.isdir(r) else None
            name = os.path.basename(r)
            try:
                if kind == "unity":
                    res = unity.remove(r, self.log)
                    self.log("%s：%s" % (name, {
                        None: "不是这个工具装的插件，没动它",
                        "removed": "已拿掉翻译插件，恢复原版（翻过的句子工具里还存着，以后再装不用重新花钱）",
                        "kept_loader": "已拿掉翻译插件；BepInEx 加载器因为还有别的 mod 在用，留着没删",
                        "partial": "有文件删不掉，游戏多半还开着。关掉游戏再点一次「还原」就行（没删掉的都记着）",
                    }.get(res, str(res))))
                elif kind == "renpy":
                    ok = core.remove_patch(r)
                    self.log("%s：%s" % (core.game_title(r), "已还原成原版（翻译记录还留着，以后再装不用重新花钱）" if ok else "本来就没装中文补丁"))
            except Exception as e:
                self.log("%s：还原失败：%s" % (name, e))
            self.post("status", r, self.status_of(r))

    def delete(self, roots, model, cfg, service):
        total = 0
        for r in roots:
            kind = core.game_kind(r) if os.path.isdir(r) else None
            try:
                if kind == "renpy":
                    n = core.delete_translations(r, None if model == ALL_MODELS else model)
                    if n and core.patch_installed(r):
                        left = core.install_patch(r, cfg, self.log)
                        if not left:
                            core.remove_patch(r)
                    self.log("%s：删掉 %d 条" % (core.game_title(r), n))
                elif kind == "unity":
                    # 在服务的内存缓存里删，不再整份重新读盘（重新读会丢掉还没自动保存的新译文）
                    n = service.delete_translations(r, None if model == ALL_MODELS else model)
                    self.log("%s：删掉 %d 句" % (os.path.basename(r), n))
                else:
                    n = 0
                total += n
            except Exception as e:
                self.log("%s：删除失败：%s" % (os.path.basename(r), e))
            self.post("status", r, self.status_of(r))
        self.log("一共删掉 %d 条译文。这些句子现在显示原文，想重翻就勾上游戏点「翻译勾选的游戏」。" % total)


# ============================================================== 界面

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from tkinter.scrolledtext import ScrolledText

    cfg = core.load_config()
    cfg.setdefault("context", True)
    cfg.setdefault("thinking", False)
    cfg.setdefault("scan_folders", [])
    auto = os.path.dirname(core.TOOL_DIR)
    if not cfg.get("folders_v2"):
        # 之前版本自动塞了一个默认文件夹；你自己加过文件夹的话，把那个默认的拿掉
        if len(cfg["scan_folders"]) > 1 and cfg["scan_folders"][0] == auto:
            cfg["scan_folders"] = cfg["scan_folders"][1:]
        cfg["folders_v2"] = True
    if not cfg["scan_folders"]:
        cfg["scan_folders"] = [auto]
    cfg.pop("ignored_games", None)  # 旧版「移除后刷新也不回来」的记录，不再用
    core.save_config(cfg)

    def _key(p):
        return os.path.normcase(os.path.abspath(p))

    games, _seen = [], set()
    for g in core.load_games():
        if _key(g) not in _seen:
            _seen.add(_key(g))
            games.append(g)

    root = tk.Tk()
    root.title("游戏汉化工具（Ren'Py / Unity）")
    root.geometry("1220x800")
    root.minsize(1000, 680)
    style = ttk.Style()
    for name in ("vista", "winnative", "clam"):
        if name in style.theme_names():
            style.theme_use(name)
            break
    style.configure("Treeview", rowheight=28)

    q = queue.Queue()
    worker = Worker(lambda *a: q.put(a))
    service = unity.TranslateService(cfg, lambda s: q.put(("log", s)))
    checked = set()
    statuses = {}
    live = {}

    # ---------------------------------------------------------- 设置
    box = ttk.LabelFrame(root, text="设置", padding=8)
    box.pack(fill="x", padx=10, pady=(10, 4))

    v_provider = tk.StringVar(value=cfg.get("provider") if cfg.get("provider") in PROVIDERS else "DeepSeek")
    v_url = tk.StringVar(value=cfg.get("base_url", ""))
    v_model = tk.StringVar(value=cfg.get("model", ""))
    v_key = tk.StringVar(value=cfg.get("api_key", ""))
    v_show = tk.BooleanVar(value=False)
    v_think = tk.BooleanVar(value=bool(cfg.get("thinking")))
    v_ctx = tk.BooleanVar(value=bool(cfg.get("context", True)))
    v_full = tk.BooleanVar(value=bool(cfg.get("game_fullscreen", True)))

    ttk.Label(box, text="服务").grid(row=0, column=0, sticky="w")
    cb_provider = ttk.Combobox(box, textvariable=v_provider, values=list(PROVIDERS), state="readonly", width=24)
    cb_provider.grid(row=0, column=1, sticky="w", padx=(4, 16))
    ttk.Label(box, text="模型").grid(row=0, column=2, sticky="w")
    cb_model = ttk.Combobox(box, textvariable=v_model, width=22)
    cb_model.grid(row=0, column=3, sticky="w", padx=(4, 16))
    ttk.Label(box, text="接口地址").grid(row=0, column=4, sticky="w")
    e_url = ttk.Entry(box, textvariable=v_url, width=34)
    e_url.grid(row=0, column=5, columnspan=2, sticky="we", padx=(4, 0))

    ttk.Label(box, text="API key").grid(row=1, column=0, sticky="w", pady=(8, 0))
    e_key = ttk.Entry(box, textvariable=v_key, show="•", width=60)
    e_key.grid(row=1, column=1, columnspan=3, sticky="we", padx=(4, 16), pady=(8, 0))
    ttk.Checkbutton(box, text="显示", variable=v_show,
                    command=lambda: e_key.configure(show="" if v_show.get() else "•")).grid(row=1, column=4, sticky="w", pady=(8, 0))
    opt = ttk.Frame(box)
    opt.grid(row=1, column=5, columnspan=2, sticky="w", pady=(8, 0))
    ttk.Checkbutton(opt, text="根据语境翻译", variable=v_ctx).pack(side="left")
    ttk.Checkbutton(opt, text="深度思考（贵好几倍）", variable=v_think,
                    command=lambda: on_settings_changed()).pack(side="left", padx=(12, 0))
    ttk.Checkbutton(opt, text="游戏全屏打开", variable=v_full).pack(side="left", padx=(12, 0))

    fonts = core.list_cn_fonts()
    font_paths = {f[0]: f[1] for f in fonts}
    v_font = tk.StringVar(value=core.font_display(cfg))
    ttk.Label(box, text="中文字体").grid(row=2, column=0, sticky="w", pady=(8, 0))
    cb_font = ttk.Combobox(box, textvariable=v_font, values=[f[0] for f in fonts], state="readonly", width=24)
    cb_font.grid(row=2, column=1, sticky="w", padx=(4, 16), pady=(8, 0))

    def pick_font_file():
        p = filedialog.askopenfilename(title="选一个字体文件", filetypes=[("字体文件", "*.ttf *.otf *.ttc"), ("所有文件", "*.*")])
        if p:
            cfg["font"] = os.path.normpath(p)
            v_font.set(os.path.basename(p))
            core.save_config(cfg)
            append_log("字体换成了 %s。已汉化的 Ren'Py 游戏再点一次「翻译勾选的游戏」就会换上（不花钱）。Unity 游戏用不了字体文件，会继续用微软雅黑。" % os.path.basename(p))

    ttk.Button(box, text="选字体文件…", command=pick_font_file).grid(row=2, column=2, columnspan=2, sticky="w", pady=(8, 0))

    def on_font(*_):
        name = v_font.get()
        if name in font_paths:
            cfg["font"] = font_paths[name]
            core.save_config(cfg)
            append_log("字体换成了 %s。已汉化的游戏再点一次「翻译勾选的游戏」就会换上（不花钱）。" % name)

    cb_font.bind("<<ComboboxSelected>>", on_font)

    lang_codes = dict(unity.FROM_LANGS)
    cur_code = cfg.get("unity_from_lang") or "en"
    v_ulang = tk.StringVar(value=next((n for n, c in unity.FROM_LANGS if c == cur_code), "英语"))
    v_uforce = tk.BooleanVar(value=bool(cfg.get("unity_force_font")))
    ttk.Label(box, text="Unity 原文").grid(row=2, column=4, sticky="w", pady=(8, 0))
    uopt = ttk.Frame(box)
    uopt.grid(row=2, column=5, columnspan=2, sticky="w", pady=(8, 0))
    ttk.Combobox(uopt, textvariable=v_ulang, values=[n for n, c in unity.FROM_LANGS], state="readonly", width=14).pack(side="left", padx=(4, 12))
    ttk.Checkbutton(uopt, text="Unity 强制换字体（出现方块时勾）", variable=v_uforce).pack(side="left")

    lbl_price = ttk.Label(box, text="", foreground="#555")
    lbl_price.grid(row=3, column=0, columnspan=7, sticky="w", pady=(8, 0))
    box.columnconfigure(6, weight=1)

    def current_cfg():
        c = dict(cfg)
        c["provider"] = v_provider.get()
        c["base_url"] = v_url.get().strip()
        c["model"] = v_model.get().strip()
        c["api_key"] = v_key.get().strip()
        c["thinking"] = bool(v_think.get())
        c["context"] = bool(v_ctx.get())
        c["game_fullscreen"] = bool(v_full.get())
        c["unity_from_lang"] = lang_codes.get(v_ulang.get(), "en")
        c["unity_force_font"] = bool(v_uforce.get())
        return c

    def collect_cfg():
        cfg.update(current_cfg())
        core.save_config(cfg)
        return dict(cfg)

    def update_price_label():
        c = current_cfg()
        p = core.price_of(c)
        peak = core.is_peak()
        when = "现在是高峰价时段（工作日 9-12、14-18 点）" if peak else "现在是闲时价时段"
        if p:
            i = 1 if peak else 0
            lbl_price.configure(text="%s 价格：输入 ¥%g / 输出 ¥%g 每百万 tokens（缓存命中的输入 ¥%g）。%s%s" % (
                c["model"], p["miss"][i], p["out"][i], p["hit"][i], when,
                "。深度思考开着：输出会多好几倍" if c["thinking"] else ""))
        else:
            lbl_price.configure(text="这个模型没有内置价格，只统计 tokens 不算钱。（价格可以在 config.json 的 prices 里加）")

    def on_settings_changed(*_):
        update_price_label()
        for r in games:
            update_row(r)

    def on_provider(*_):
        url, models = PROVIDERS[v_provider.get()]
        cb_model.configure(values=models)
        if url:
            v_url.set(url)
            e_url.configure(state="disabled")
            if v_model.get() not in models:
                v_model.set(models[0])
        else:
            e_url.configure(state="normal")
        on_settings_changed()

    cb_provider.bind("<<ComboboxSelected>>", on_provider)
    v_model.trace_add("write", lambda *a: on_settings_changed())

    # ---------------------------------------------------------- 游戏列表
    mid = ttk.LabelFrame(root, text="游戏（点一行勾选 / 取消；点「启动」直接开游戏）", padding=8)
    mid.pack(fill="both", expand=True, padx=10, pady=4)

    tools = ttk.Frame(mid)
    tools.pack(fill="x", pady=(0, 6))
    ttk.Label(tools, text="游戏文件夹").pack(side="left")
    v_folder = tk.StringVar()
    cb_folder = ttk.Combobox(tools, textvariable=v_folder, state="readonly", width=34)
    cb_folder.pack(side="left", padx=(4, 6))

    # 游戏列表：每行一个勾选框 + 一个真正的「启动」按钮
    COLS = [("sel", "选", 3), ("title", "游戏", 22), ("ver", "引擎", 17), ("launch", "", 8),
            ("status", "状态", 24), ("texts", "还要翻", 16), ("cost", "预计花费", 9), ("path", "位置", 1)]
    BG, BG_ALT, BG_SEL = "#ffffff", "#f5f7fa", "#e3eefc"
    COLOR = {"done": "#1a7f37", "todo": "#57606a", "busy": "#0969da", "warn": "#9a6700", "err": "#cf222e"}

    listbox = ttk.Frame(mid)
    listbox.pack(fill="both", expand=True)
    canvas = tk.Canvas(listbox, highlightthickness=1, highlightbackground="#d0d7de", bg=BG)
    vsb = ttk.Scrollbar(listbox, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    vsb.pack(side="left", fill="y")
    table = tk.Frame(canvas, bg=BG)
    table_id = canvas.create_window((0, 0), window=table, anchor="nw")
    table.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(table_id, width=e.width))

    def _wheel(ev):
        canvas.yview_scroll(int(-ev.delta / 120) or (-1 if ev.delta > 0 else 1), "units")

    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
    for i, (key, head, w) in enumerate(COLS):
        table.grid_columnconfigure(i, weight=1 if key == "path" else 0)

    rows = {}  # 游戏路径 -> {控件}

    def row_values(r):
        """返回 (标题, 引擎, 状态, 状态颜色, 还要翻, 预计花费)"""
        st = statuses.get(r)
        if st is None:
            return (os.path.basename(r), "", live.get(r) or "读取中……", "todo", "", "")
        if "error" in st:
            return (os.path.basename(r), "", st["error"], "err", "", "")
        if r in live:
            color = "warn" if live[r].startswith("已暂停") else "busy"
        else:
            color = None
        if st.get("kind") == "unity":
            status = ("已装翻译插件，已翻 %d 句" % st["done"]) if st["installed"] else "未装翻译插件"
            return (st["title"], st["version"], live.get(r) or status, color or ("done" if st["installed"] else "todo"), "边玩边翻", "按量")
        if st["total"] == 0:
            status, c0 = "没读到文本（可能加密了）", "err"
        elif st["left"] == 0:
            status, c0 = ("已汉化", "done") if st["installed"] else ("已翻完，补丁未装", "warn")
        elif st["left"] == st["total"]:
            status, c0 = ("已装补丁，但还没翻", "warn") if st["installed"] else ("未汉化", "todo")
        else:
            status, c0 = "翻了 %d/%d 条" % (st["total"] - st["left"], st["total"]) + ("（已装补丁）" if st["installed"] else ""), "warn"
        texts = "%d 条 / %d 字" % (st["left"], st["chars_left"]) if st["left"] else "-"
        cost = "-"
        if st["left"]:
            c = core.estimate_cost(current_cfg(), st["chars_left"])
            cost = "¥%.2f" % c if c is not None else "?"
        return (st["title"], st["version"], live.get(r) or status, color or c0, texts, cost)

    def toggle(r):
        if r in checked:
            checked.discard(r)
        else:
            checked.add(r)
        update_row(r)

    def _under(g, folder):
        if not folder:
            return True
        return _key(g).startswith(_key(folder).rstrip("\\/") + os.sep)

    def visible():
        """列表只显示当前选中的那个文件夹（包括它所有子文件夹）里的游戏"""
        return [g for g in games if _under(g, v_folder.get())]

    def redraw():
        for w in table.winfo_children():
            w.destroy()
        rows.clear()
        for i, (key, head, w) in enumerate(COLS):
            h = tk.Label(table, text=head, bg="#eaeef2", fg="#24292f", anchor="w" if key in ("title", "ver", "status", "texts", "path") else "center",
                         font=("Microsoft YaHei UI", 9, "bold"), padx=6, pady=5, width=w)
            h.grid(row=0, column=i, sticky="nsew")
            if key == "sel":
                h.configure(cursor="hand2")
                h.bind("<Button-1>", lambda e: set_all(any(g not in checked for g in visible())))
        for n, r in enumerate(visible(), start=1):
            bg = BG if n % 2 else BG_ALT
            var = tk.BooleanVar(value=r in checked)
            cb = tk.Checkbutton(table, variable=var, bg=bg, activebackground=bg, command=lambda rr=r: toggle(rr))
            cb.grid(row=n, column=0, sticky="nsew")
            cells = {}
            for i, (key, head, w) in enumerate(COLS):
                if key in ("sel", "launch"):
                    continue
                lab = tk.Label(table, text="", bg=bg, anchor="center" if key == "cost" else "w", padx=6, pady=7, width=w)
                lab.grid(row=n, column=i, sticky="nsew")
                lab.bind("<Button-1>", lambda e, rr=r: toggle(rr))
                cells[key] = lab
            btn_cell = tk.Frame(table, bg=bg)
            btn_cell.grid(row=n, column=COLS.index(("launch", "", 8)), sticky="nsew")
            btn = tk.Button(btn_cell, text="启动", command=lambda rr=r: launch_game(rr), bg="#1f883d", fg="white",
                            activebackground="#2da44e", activeforeground="white", relief="flat", bd=0,
                            font=("Microsoft YaHei UI", 9, "bold"), padx=14, pady=2, cursor="hand2")
            btn.pack(expand=True, pady=3)
            rows[r] = {"var": var, "cb": cb, "cells": cells, "btn": btn, "bg": bg, "btn_cell": btn_cell}
            update_row(r)
        mid.configure(text="游戏：%s（共 %d 个；点一行勾选 / 取消，点绿色「启动」直接开游戏）" % (
            v_folder.get() or "全部", len(rows)))

    def update_row(r):
        w = rows.get(r)
        if not w:
            return
        title, ver, status, color, texts, cost = row_values(r)
        on = r in checked
        bg = BG_SEL if on else w["bg"]
        w["var"].set(on)
        w["cb"].configure(bg=bg, activebackground=bg)
        w["btn_cell"].configure(bg=bg)
        vals = {"title": title, "ver": ver, "status": status, "texts": texts, "cost": cost, "path": r}
        for key, lab in w["cells"].items():
            lab.configure(text=vals[key], bg=bg, fg=COLOR.get(color, "#24292f") if key == "status" else "#24292f")
        st = statuses.get(r) or {}
        w["btn"].configure(state="disabled" if "error" in st else "normal")

    def set_all(on):
        vis = visible()
        if on:
            checked.update(vis)
        else:
            checked.difference_update(vis)
        for r in vis:
            update_row(r)

    # ---------------------------------------------------------- 文件夹 / 列表
    def refresh_folder_box():
        cb_folder.configure(values=cfg["scan_folders"])
        if v_folder.get() not in cfg["scan_folders"]:
            cur = cfg.get("current_folder")
            v_folder.set(cur if cur in cfg["scan_folders"] else (cfg["scan_folders"][0] if cfg["scan_folders"] else ""))

    def rescan(log_new=True):
        """扫当前选中的文件夹：新游戏加进来，硬盘上已经没有的拿掉"""
        folder = v_folder.get()
        seen = set(_key(g) for g in games)
        new = []
        if folder and os.path.isdir(folder):
            for g in core.find_games(folder):
                if _key(g) not in seen:
                    seen.add(_key(g))
                    new.append(g)
        gone = [g for g in games if _under(g, folder) and not os.path.isdir(g)]
        for g in gone:
            games.remove(g)
            checked.discard(g)
            statuses.pop(g, None)
        if new or gone:
            games.extend(new)
            core.save_games(games)
        redraw()
        if log_new:
            if new:
                append_log("扫出 %d 个新游戏（没勾）：%s" % (len(new), "、".join(os.path.basename(g) for g in new)))
            if gone:
                append_log("%d 个游戏在硬盘上找不到了，已从列表拿掉：%s" % (len(gone), "、".join(os.path.basename(g) for g in gone)))
        return new

    def switch_folder(d):
        v_folder.set(d)
        cfg["current_folder"] = d
        core.save_config(cfg)
        checked.clear()
        rescan(log_new=False)
        start_refresh([g for g in visible() if g not in statuses])

    cb_folder.bind("<<ComboboxSelected>>", lambda e: switch_folder(v_folder.get()))

    def add_folder():
        d = filedialog.askdirectory(title="选一个放游戏的文件夹（会记住，以后在这里切换、点「刷新」扫里面的游戏）")
        if not d:
            return
        d = os.path.normpath(d)
        if d not in cfg["scan_folders"]:
            cfg["scan_folders"].append(d)
            core.save_config(cfg)
        refresh_folder_box()
        switch_folder(d)
        if not visible():
            messagebox.showinfo("没找到", "这个文件夹里暂时没找到 Ren'Py 或 Unity 游戏。文件夹已经记住了，以后放了游戏点「刷新」就会扫出来。")

    def remove_folder():
        d = v_folder.get()
        if not d:
            return
        if not messagebox.askyesno("移除文件夹", "不再记住这个文件夹：\n%s\n\n（不会删任何文件，以后想用再「添加文件夹…」就行）" % d):
            return
        cfg["scan_folders"] = [x for x in cfg["scan_folders"] if x != d]
        core.save_config(cfg)
        v_folder.set("")
        refresh_folder_box()
        switch_folder(v_folder.get())

    def do_refresh():
        if worker.busy:
            return
        rescan()
        start_refresh()

    def remove_from_list():
        sel = selected()
        if not sel:
            messagebox.showinfo("提示", "先在列表里勾上要移除的游戏。")
            return
        if not messagebox.askyesno("从列表移除", "只是暂时从列表里拿掉，不会删游戏、也不会动已装的东西。\n点「刷新」会重新扫出来。\n\n移除勾选的 %d 个？" % len(sel)):
            return
        for g in sel:
            games.remove(g)
            checked.discard(g)
        core.save_games(games)
        redraw()

    def open_glossary():
        sel = [g for g in selected() if core.game_kind(g) == "renpy"]
        if not sel:
            messagebox.showinfo("名词表", "先勾上一个 Ren'Py 游戏。\n名词表是这个游戏的角色名统一译法，翻译时自动生成，可以自己改；改完以后新翻的句子会照着用。")
            return
        p = core.glossary_path(sel[0])
        if not os.path.exists(p):
            core.write_json(p, {}, indent=1)
        try:
            os.startfile(p)
        except Exception as e:
            append_log("打不开名词表：%s" % e)

    for text, cmd in (("添加文件夹…", add_folder), ("移除文件夹", remove_folder), ("刷新", do_refresh)):
        ttk.Button(tools, text=text, command=cmd).pack(side="left", padx=(0, 6))
    ttk.Separator(tools, orient="vertical").pack(side="left", fill="y", padx=8)
    for text, cmd in (("全选", lambda: set_all(True)), ("全不选", lambda: set_all(False)),
                      ("从列表移除", remove_from_list)):
        ttk.Button(tools, text=text, command=cmd).pack(side="left", padx=(0, 6))

    # ---------------------------------------------------------- 操作按钮
    bar = ttk.Frame(root, padding=(10, 4))
    bar.pack(fill="x")

    def selected():
        return [g for g in visible() if g in checked]

    def start_refresh(roots=None):
        if worker.busy:
            return
        roots = list(visible() if roots is None else roots)
        if not roots:
            return
        for r in roots:
            statuses.pop(r, None)
            update_row(r)
        set_busy(True)
        worker.start(worker.refresh, roots, collect_cfg())

    def run_translate(sel):
        c = collect_cfg()
        if not c["api_key"]:
            messagebox.showwarning("缺 key", "先在上面填你的 API key。")
            e_key.focus_set()
            return
        if not c["model"] or not c["base_url"]:
            messagebox.showwarning("缺设置", "接口地址和模型名都要填。")
            return
        lines, total_cost, has_unity = [], 0.0, False
        for g in sel:
            st = statuses.get(g) or {}
            name = st.get("title") or os.path.basename(g)
            if st.get("kind") == "unity":
                has_unity = True
                lines.append("· %s（Unity：装翻译插件，玩的时候按量花钱）" % name)
            else:
                total_cost += core.estimate_cost(c, st.get("chars_left") or 0) or 0
                lines.append("· %s" % name)
        msg = "要处理的游戏：\n%s\n\n模型：%s%s\n根据语境翻译：%s\n字体：%s\nRen'Py 游戏预计花费：约 ¥%.2f（按当前时段价格估的）" % (
            "\n".join(lines), c["model"], "（开着深度思考）" if c["thinking"] else "",
            "开" if c["context"] else "关", core.font_display(c), total_cost)
        if has_unity:
            msg += "\nUnity 游戏需要从 GitHub 下载插件（几百 KB 到 30 多 MB）。"
        if not messagebox.askyesno("确认", msg + "\n\n开始？"):
            return
        set_busy(True)
        worker.start(worker.translate, sel, c)

    def do_translate():
        sel = selected()
        if not sel:
            messagebox.showinfo("提示", "先勾上要翻的游戏（点一行就是勾选）。")
            return
        run_translate(sel)

    def do_restart():
        sel = [g for g in worker.last_roots if g in games]
        if not sel:
            messagebox.showinfo("重启任务", "还没有可以重启的任务。")
            return
        run_translate(sel)

    def do_pause():
        if core.PAUSE.is_set():
            core.PAUSE.clear()
            b_pause.configure(text="暂停")
            append_log("继续翻译。")
            if worker.current:
                live.pop(worker.current, None)
                update_row(worker.current)
        else:
            core.PAUSE.set()
            b_pause.configure(text="继续")
            n = core.inflight()
            append_log("已暂停：不再发新请求。%s点「继续」接着翻。" % ("已经发出去的 %d 批回来后会存好（进度可能再涨一点），" % n if n else ""))
            if worker.current:
                live[worker.current] = "已暂停"
                update_row(worker.current)

    def do_stop():
        worker.stop.set()
        core.PAUSE.clear()
        b_pause.configure(text="暂停")
        n = core.inflight()
        append_log("已停止。%s" % ("已经发出去的 %d 批不用等，回来后会自动存好。" % n if n else ""))

    def do_restore():
        sel = selected()
        if not sel:
            messagebox.showinfo("提示", "先勾上要还原的游戏。")
            return
        if not messagebox.askyesno("还原成原版", "会拿掉勾选游戏里这个工具装的中文补丁 / 翻译插件，游戏恢复原样。\n翻译记录会留在工具里，以后再装不用重新花钱。\n\n还原勾选的 %d 个？" % len(sel)):
            return
        set_busy(True)
        worker.start(worker.restore, sel, collect_cfg())

    def do_delete():
        if worker.busy:
            return
        if core.busy_texts():
            messagebox.showinfo("等一下再删", "停止前发出去的翻译请求还有 %d 句没回来存好（一般一两分钟内）。\n"
                                "现在删的话，它们回来会把刚删掉的译文又写回去。等日志安静下来再删。" % core.busy_texts())
            return
        if service.busy():
            messagebox.showinfo("等一下再删", "Unity 游戏正在请求翻译。先关掉游戏再删（游戏开着的话，插件还会把译文写回去）。")
            return
        service.save()  # 先把还没自动保存的 Unity 新译文写盘，下面按模型统计的条数才准
        win = tk.Toplevel(root)
        win.title("删除翻译记录")
        win.transient(root)
        win.grab_set()
        win.resizable(False, False)
        win.geometry("+%d+%d" % (root.winfo_rootx() + 240, root.winfo_rooty() + 200))
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

        counts_cache = {}

        def counts_for(g):
            if g not in counts_cache:
                try:
                    k = core.game_kind(g)
                    counts_cache[g] = core.model_counts(g) if k == "renpy" else (unity.model_counts(g) if k == "unity" else core.Counter())
                except Exception:
                    counts_cache[g] = core.Counter()
            return counts_cache[g]

        v_scope = tk.StringVar(value="checked" if checked else "all")
        v_m = tk.StringVar()
        ttk.Label(frm, text="删哪些游戏的：").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(frm, text="勾选的游戏（%d 个）" % len(checked), variable=v_scope, value="checked").grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(frm, text="当前文件夹里所有游戏", variable=v_scope, value="all").grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Label(frm, text="删哪个模型翻的：").grid(row=1, column=0, sticky="w", pady=(10, 0))
        cb_m = ttk.Combobox(frm, textvariable=v_m, state="readonly", width=40)
        cb_m.grid(row=1, column=1, columnspan=2, sticky="w", pady=(10, 0))
        preview = ttk.Label(frm, text="", justify="left", foreground="#333")
        preview.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        def scope_games():
            return selected() if v_scope.get() == "checked" else visible()

        def update_models(*_):
            tot = core.Counter()
            for g in scope_games():
                tot.update(counts_for(g))
            labels = ["%s（%d 条）" % (m, n) for m, n in tot.most_common()]
            labels.append("%s（%d 条）" % (ALL_MODELS, sum(tot.values())))
            cb_m.configure(values=labels)
            if v_m.get() not in labels:
                v_m.set(labels[0])
            update_preview()

        def chosen_model():
            lab = v_m.get()
            return lab[:lab.rfind("（")] if "（" in lab else lab

        def update_preview(*_):
            m = chosen_model()
            rows = []
            for g in scope_games():
                c = counts_for(g)
                n = sum(c.values()) if m == ALL_MODELS else c.get(m, 0)
                if n:
                    rows.append("  %s：%d 条" % ((statuses.get(g) or {}).get("title") or os.path.basename(g), n))
            preview.configure(text=("会删掉：\n" + "\n".join(rows)) if rows else "勾选范围里没有这个模型翻的译文。")

        v_scope.trace_add("write", update_models)
        cb_m.bind("<<ComboboxSelected>>", update_preview)

        def go():
            m = chosen_model()
            targets = [g for g in scope_games() if (sum(counts_for(g).values()) if m == ALL_MODELS else counts_for(g).get(m, 0))]
            n = sum((sum(counts_for(g).values()) if m == ALL_MODELS else counts_for(g).get(m, 0)) for g in targets)
            if not n:
                messagebox.showinfo("没有可删的", "没有符合条件的译文。", parent=win)
                return
            unity_note = ""
            if any(core.game_kind(g) == "unity" for g in targets):
                unity_note = "\n\nUnity 游戏要先关掉再删：游戏开着的话，插件会把内存里的译文又写回去。"
            if not messagebox.askyesno("确认删除", "确定删掉 %s 翻的 %d 条译文？\n涉及 %d 个游戏。\n\n删掉后这些句子在游戏里会变回原文，可以换个模型重新翻。删了就找不回来。%s" % (m, n, len(targets), unity_note), parent=win):
                return
            win.destroy()
            set_busy(True)
            worker.start(worker.delete, targets, m, collect_cfg(), service)

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="删除…", command=go).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left")
        update_models()

    def launch_game(g):
        collect_cfg()
        exe = core.game_exe(g) if os.path.isdir(g) else None
        if not exe or not os.path.isfile(exe):
            append_log("%s：找不到游戏的 exe" % os.path.basename(g))
            return
        st = statuses.get(g) or {}
        kind = core.game_kind(g)
        full = bool(cfg.get("game_fullscreen", True))
        args = []
        if kind == "unity":
            if not st.get("installed"):
                append_log("%s：还没装翻译插件，会以原版打开。要翻译的话先勾上它点「翻译勾选的游戏」。" % (st.get("title") or os.path.basename(g)))
            else:
                if not service_ok[0]:
                    append_log("注意：翻译服务没启动成功，Unity 游戏里的新文字不会翻。")
                service.set_game(st.get("title") or os.path.basename(g))
                unity.write_config(g, cfg)  # 字体、原文语言这些设置改过的话同步进去
            if full:
                args = ["-screen-fullscreen", "1"]  # Unity 自带的启动参数
        elif kind == "renpy":
            # Ren'Py 没有全屏启动参数：靠中文补丁在开窗口前把游戏的「全屏」设置打开
            try:
                if core.patch_installed(g):
                    core.update_patch_script(g)  # 旧补丁换成带全屏功能的新版（不动译文、不花钱）
                    core.set_fullscreen_flag(g, full)
                elif full:
                    append_log("%s 还没装中文补丁，工具管不了它全不全屏：游戏里按一下 F 或 Alt+回车切全屏，游戏自己会记住。"
                               % (st.get("title") or os.path.basename(g)))
            except Exception as e:
                append_log("设置全屏失败（%s），照常打开" % e)
        try:
            unity.launch(exe, g, args)
            append_log("已启动 %s%s" % (os.path.basename(exe), "（全屏）" if full and (args or core.patch_installed(g)) else ""))
        except Exception as e:
            append_log("启动失败：%s" % e)

    def open_tool_folder():
        try:
            os.startfile(core.TOOL_DIR)
        except Exception:
            pass

    b_go = ttk.Button(bar, text="翻译勾选的游戏", command=do_translate)
    b_pause = ttk.Button(bar, text="暂停", command=do_pause, state="disabled")
    b_stop = ttk.Button(bar, text="停止", command=do_stop, state="disabled")
    b_restart = ttk.Button(bar, text="重启任务", command=do_restart)
    b_delete = ttk.Button(bar, text="删除翻译记录…", command=do_delete)
    b_restore = ttk.Button(bar, text="还原勾选的游戏", command=do_restore)
    b_gloss = ttk.Button(bar, text="名词表", command=open_glossary)
    b_folder = ttk.Button(bar, text="打开工具文件夹（用量记录）", command=open_tool_folder)
    for b in (b_go, b_pause, b_stop, b_restart):
        b.pack(side="left", padx=(0, 6))
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
    for b in (b_delete, b_restore, b_gloss):
        b.pack(side="left", padx=(0, 6))
    b_folder.pack(side="right")

    # ---------------------------------------------------------- 进度和日志
    bottom = ttk.Frame(root, padding=(10, 4, 10, 10))
    bottom.pack(fill="both")
    line = ttk.Frame(bottom)
    line.pack(fill="x")
    lbl_prog = ttk.Label(line, text="")
    lbl_prog.pack(side="left")
    lbl_usage = ttk.Label(line, text="", foreground="#555")
    lbl_usage.pack(side="right")
    pbar = ttk.Progressbar(bottom, mode="determinate")
    pbar.pack(fill="x", pady=(2, 6))
    txt_log = ScrolledText(bottom, height=8, wrap="word", font=("Microsoft YaHei UI", 9))
    txt_log.pack(fill="both", expand=True)

    def append_log(s):
        txt_log.insert("end", s + "\n")
        txt_log.see("end")

    busy_widgets = (b_go, b_restart, b_delete, b_restore)

    def set_busy(b):
        for w in busy_widgets:
            w.configure(state="disabled" if b else "normal")
        b_stop.configure(state="normal" if b else "disabled")
        b_pause.configure(state="normal" if b else "disabled")
        if not b:
            b_pause.configure(text="暂停")

    def usage_text():
        parts = []
        if worker.usage.hit + worker.usage.miss + worker.usage.out:
            parts.append("Ren'Py：" + worker.usage.text())
        if service.usage.hit + service.usage.miss + service.usage.out:
            parts.append("Unity 边玩边翻：已翻 %d 句，%s" % (service.count, service.usage.text()))
        return "本次用量  " + ("；".join(parts) if parts else "还没花钱")

    def pump():
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    append_log(msg[1])
                elif kind == "status":
                    statuses[msg[1]] = msg[2]
                    live.pop(msg[1], None)
                    update_row(msg[1])
                elif kind == "live":
                    if msg[2] is None:
                        live.pop(msg[1], None)
                    else:
                        live[msg[1]] = msg[2]
                    update_row(msg[1])
                elif kind == "progress":
                    r, t, a, b = msg[1], msg[2], msg[3], msg[4]
                    pbar["maximum"] = max(1, b)
                    pbar["value"] = a
                    lbl_prog.configure(text="%s：%d / %d" % (t, a, b))
                    live[r] = ("已暂停 %d/%d" if core.PAUSE.is_set() else "翻译中 %d/%d") % (a, b)
                    update_row(r)
                elif kind == "error":
                    messagebox.showerror("出错", msg[1])
                elif kind == "idle":
                    set_busy(False)
                    live.clear()
                    for r in games:
                        update_row(r)
        except queue.Empty:
            pass
        lbl_usage.configure(text=usage_text())
        root.after(200, pump)

    def on_close():
        if worker.busy:
            if not messagebox.askyesno("还在翻译", "还在翻译，确定关掉吗？翻好的部分已经保存了。"):
                return
            worker.stop.set()
            core.PAUSE.clear()
        if service.count and service.busy() == 0:
            if not messagebox.askyesno("关掉工具", "关掉后，正在玩的 Unity 游戏里新出现的文字就不会翻了（翻过的照样是中文）。\n\n确定关掉？"):
                return
        n = core.inflight() + service.busy()
        if n:
            ans = messagebox.askyesnocancel(
                "还有请求在路上",
                "还有 %d 个已经发出去的翻译请求没回来（这些已经在计费）。\n\n"
                "「是」：等它们回来存好再关（最多等 2 分钟）\n「否」：现在就关，这几批的结果不要了\n「取消」：不关" % n)
            if ans is None:
                return
            if ans:
                for w in busy_widgets + (b_pause, b_stop):
                    w.configure(state="disabled")
                append_log("等已经发出去的请求回来……回来后自动关闭。")
                deadline = [600]  # 约 2 分钟（每 200ms 检查一次）

                def wait_then_close():
                    deadline[0] -= 1
                    # 不只等接口回复，还要等回来的结果写完盘
                    if (core.inflight() + service.busy() or core.busy_texts()) and deadline[0] > 0:
                        root.after(200, wait_then_close)
                        return
                    finish_close()

                root.after(200, wait_then_close)
                return
        finish_close()

    def finish_close():
        # 已经付过钱、但还在内存里没写进硬盘的译文：关之前再存一次，还存不进去就问你
        left = core.flush_unsaved() if core.unsaved_count() else 0
        u_ok = service.save()
        if left or not u_ok:
            parts = []
            if left:
                parts.append("Ren'Py %d 条" % left)
            if not u_ok:
                parts.append("Unity 约 %d 句" % service.unsaved())
            if not messagebox.askyesno(
                    "还有译文没存进硬盘",
                    "已经付过钱的译文还有 %s 存不进硬盘（硬盘满了，或者文件被别的程序占着）。\n"
                    "现在关掉，这些就没了，以后要重新花钱翻。\n\n"
                    "「否」：先不关，腾出空间 / 关掉占用的程序后再关（关的时候会自动再存一次）\n"
                    "「是」：不要了，直接关" % "、".join(parts), icon="warning", default="no"):
                set_busy(worker.busy)
                append_log("没关。腾出硬盘空间或关掉占用文件的程序后再关，关的时候会自动再存一次。")
                return
        worker.flush_late_usage()
        service.shutdown(wait_seconds=0)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # ---------------------------------------------------------- 启动
    on_provider()
    service_ok = [service.start()]
    if not service_ok[0]:
        append_log("Unity 翻译服务没启动成功（端口 %d 被占用，可能已经开着另一个工具窗口）。Ren'Py 不受影响。" % unity.PORT)
    refresh_folder_box()
    redraw()
    rescan()
    append_log("列表只显示上面「游戏文件夹」选中的那个文件夹（含子文件夹）里的游戏；可以记住多个文件夹，下拉切换。点「刷新」重新扫当前文件夹。勾哪个处理哪个，没勾的不会动。")
    if not fonts:
        append_log("没在系统里找到中文字体，点「选字体文件…」选一个。")
    for w in core.CONFIG_WARNINGS:
        append_log(w)
        messagebox.showwarning("设置文件坏了", w)
    start_refresh()
    root.after(200, pump)
    if not v_key.get():
        e_key.focus_set()
    root.mainloop()


_INSTANCE_MUTEX = None  # 整个进程期间都拿着，不要关


def already_running():
    """同一个工具文件夹只允许开一个窗口：两个窗口同时写翻译记录会互相覆盖、重复花钱"""
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return False
    try:
        import ctypes
        import hashlib
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        name = "Local\\CnGameTool_" + hashlib.sha256(
            os.path.normcase(core.TOOL_DIR).encode("utf-8")).hexdigest()[:32]
        _INSTANCE_MUTEX = k32.CreateMutexW(None, False, name)
        return bool(_INSTANCE_MUTEX) and ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False  # 检查不了就照常打开，不挡你


def main():
    if already_running():
        try:
            import tkinter as tk
            from tkinter import messagebox
            w = tk.Tk()
            w.withdraw()
            messagebox.showinfo("游戏汉化工具", "工具已经开着了（看看任务栏）。\n同时开两个会互相覆盖翻译记录，所以这个就不开了。")
            w.destroy()
        except Exception:
            pass
        return
    try:
        run_gui()
    except Exception:
        err = traceback.format_exc()
        with open(os.path.join(core.TOOL_DIR, "错误日志.txt"), "a", encoding="utf-8") as f:
            f.write(err + "\n")
        try:
            from tkinter import messagebox
            messagebox.showerror("出错了", err[-1500:])
        except Exception:
            print(err)


if __name__ == "__main__":
    main()
