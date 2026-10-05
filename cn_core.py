# -*- coding: utf-8 -*-
"""
游戏汉化工具 - 核心部分
读取 Ren'Py 游戏的脚本（.rpyc / .rpa），用你自己的 API 翻译，再装一个中文补丁进游戏。
补丁不改动游戏原文件：只新增 game/zz_cn_patch.rpy 和 game/zz_cn/ 文件夹，删掉就恢复原样。
"""
import builtins
import codecs
import collections
import copyreg
import datetime
import hashlib
import io
import json
import os
import pickle
import re
import shutil
import ssl
import struct
import threading
import time
import tokenize
import urllib.error
import urllib.request
import zlib
import ast as pyast
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(TOOL_DIR, "config.json")
GAMES_PATH = os.path.join(TOOL_DIR, "games.json")
DATA_DIR = os.path.join(TOOL_DIR, "data")
USAGE_LOG = os.path.join(TOOL_DIR, "用量记录.csv")

PATCH_RPY = "zz_cn_patch.rpy"
PATCH_DIR = "zz_cn"
MAP_NAME = "zh_map.json"

# 旧版脚本存过 key 的位置，第一次运行时自动搬过来
OLD_CONFIGS = []

# ============================================================== 配置

DEFAULT_PRICES = {
    # 元 / 百万 tokens，[闲时, 高峰]。高峰：北京时间工作日 9-12 点、14-18 点
    "deepseek-flash": {"hit": [0.02, 0.04], "miss": [1.0, 2.0], "out": [4.0, 8.0]},
    "deepseek-v4-pro": {"hit": [0.15, 0.30], "miss": [4.5, 9.0], "out": [13.5, 27.0]},
}

DEFAULT_CONFIG = {
    "provider": "DeepSeek",
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "threads": 4,
    "batch_size": 20,
    "batch_chars": 1500,
    "temperature": 0.7,
    "json_mode": True,
    "timeout": 180,
    "font": "",
    "glossary": {},
    "extra_prompt": "",
    "renpy_from_lang": "auto",
    "prices": DEFAULT_PRICES,
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    src = CONFIG_PATH if os.path.exists(CONFIG_PATH) else None
    migrated = False
    if not src:
        for p in OLD_CONFIGS:
            if os.path.exists(p):
                src, migrated = p, True
                break
    if src:
        try:
            with open(src, "r", encoding="utf-8-sig") as f:
                old = json.load(f)
            for k, v in old.items():
                if not k.startswith("_") and (k in cfg or not migrated):
                    cfg[k] = v  # 文件夹、当前文件夹、各种勾选项都要读回来
        except Exception as e:
            if src == CONFIG_PATH:
                # 设置文件坏了：先留一份备份再用默认设置，不然一启动就被覆盖，key 和文件夹全没了
                bak = CONFIG_PATH + ".坏了_%s.bak" % datetime.datetime.now().strftime("%m%d_%H%M%S")
                try:
                    shutil.copyfile(CONFIG_PATH, bak)
                    CONFIG_WARNINGS.append("设置文件 config.json 读不了（%s），已经备份成 %s，这次先用默认设置。"
                                           "key 要重新填；备份用记事本打开能找回原来的 key。" % (e, os.path.basename(bak)))
                except OSError:
                    CONFIG_WARNINGS.append("设置文件 config.json 读不了（%s），这次先用默认设置。" % e)
    if migrated:
        # 旧脚本默认填的 deepseek-chat 现在已经不在 DeepSeek 的模型列表里了
        if cfg.get("model") == "deepseek-chat":
            cfg["model"] = "deepseek-flash"
        save_config(cfg)
    for name, p in DEFAULT_PRICES.items():
        cfg.setdefault("prices", {}).setdefault(name, p)
    _EXTRA_HOLIDAYS.update(str(d) for d in (cfg.get("holidays") or []))
    if cfg.get("batch_size") == 30 and cfg.get("batch_chars") == 2500:
        cfg["batch_size"], cfg["batch_chars"] = 20, 1500  # 旧默认值，换成小批次
    return cfg


CONFIG_WARNINGS = []  # 启动时要在日志里告诉你的事


def save_config(cfg):
    # 先写临时文件再换上：写到一半断电 / 硬盘满，原来的设置（含 key）还在
    write_json(CONFIG_PATH, {k: v for k, v in cfg.items() if not str(k).startswith("_")}, indent=2)


def load_games():
    if os.path.exists(GAMES_PATH):
        try:
            with open(GAMES_PATH, "r", encoding="utf-8") as f:
                return [p for p in json.load(f) if isinstance(p, str)]
        except Exception:
            pass
    return []


def save_games(paths):
    with open(GAMES_PATH, "w", encoding="utf-8") as f:
        json.dump(paths, f, ensure_ascii=False, indent=2)


# ============================================================== 找游戏

def is_renpy_root(path):
    return os.path.isdir(os.path.join(path, "game")) and os.path.isdir(os.path.join(path, "renpy"))


def find_renpy_games(folder, max_depth=4):
    found = []
    folder = os.path.abspath(folder)
    skip = {"game", "renpy", "lib", "images", "audio", "gui", "cache", "saves", "__pycache__", PATCH_DIR}
    for dirpath, dirnames, filenames in os.walk(folder):
        rel = os.path.relpath(dirpath, folder)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if is_renpy_root(dirpath):
            found.append(dirpath)
            dirnames[:] = []
            continue
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d.lower() not in skip]
    return sorted(found)


def game_kind(root):
    if is_renpy_root(root):
        return "renpy"
    import cn_unity
    if cn_unity.is_unity_root(root):
        return "unity"
    return None


def find_games(folder, max_depth=4):
    """找 Ren'Py 和 Unity 游戏"""
    found = []
    folder = os.path.abspath(folder)
    skip = {"game", "renpy", "lib", "images", "audio", "gui", "cache", "saves", "__pycache__", PATCH_DIR,
            "bepinex", "mono", "monobleedingedge", "dotnet"}
    for dirpath, dirnames, filenames in os.walk(folder):
        rel = os.path.relpath(dirpath, folder)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if game_kind(dirpath):
            found.append(dirpath)
            dirnames[:] = []
            continue
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.endswith("_Data")]
    return sorted(found)


def game_exe(root):
    kind = game_kind(root)
    if kind == "unity":
        import cn_unity
        info = cn_unity.unity_info(root)
        return info["exe"] if info else None
    exes = [f for f in sorted(os.listdir(root)) if f.lower().endswith(".exe")]
    good = [f for f in exes if not f.lower().endswith("-32.exe") and "crash" not in f.lower()]
    pick = (good or exes or [None])[0]
    return os.path.join(root, pick) if pick else None


def game_title(root):
    name = os.path.basename(root.rstrip("\\/"))
    for exe in sorted(os.listdir(root)):
        if exe.lower().endswith(".exe") and not exe.lower().endswith("-32.exe"):
            name = exe[:-4]
            break
    return name


def renpy_version(root):
    p = os.path.join(root, "renpy", "vc_version.py")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            m = re.search(r"version\s*=\s*u?['\"]([\d.]+)", f.read())
        if m:
            return ".".join(m.group(1).split(".")[:3])
    except Exception:
        pass
    cands = [os.path.join(root, "renpy", n) for n in ("vc_version.pyo", "vc_version.pyc")]
    pyc = os.path.join(root, "renpy", "__pycache__")
    if os.path.isdir(pyc):
        cands += [os.path.join(pyc, n) for n in os.listdir(pyc) if n.startswith("vc_version")]
    for c in cands:
        try:
            with open(c, "rb") as f:
                m = re.search(rb"(\d+\.\d+\.\d+)\.\d{6,}", f.read())
            if m:
                return m.group(1).decode("ascii")
        except Exception:
            pass
    return "?"


# ============================================================== 读 rpyc（不执行游戏代码，只把结构读出来）

def _apply_state(obj, state):
    d = getattr(obj, "__dict__", None)
    if d is None:
        return
    if isinstance(state, tuple) and len(state) == 2 and all(p is None or isinstance(p, dict) for p in state):
        parts = state
    elif isinstance(state, dict):
        parts = (state,)
    else:
        d["_state"] = state
        return
    for part in parts:
        if part:
            for k, v in part.items():
                d[k if isinstance(k, str) else str(k)] = v


class _FakeObj(object):
    def __new__(cls, *a, **k):
        return object.__new__(cls)

    def __init__(self, *a, **k):
        pass

    def __setstate__(self, state):
        _apply_state(self, state)

    def append(self, x):
        self.__dict__.setdefault("_items", []).append(x)

    def extend(self, xs):
        self.__dict__.setdefault("_items", []).extend(xs)

    def add(self, x):
        self.__dict__.setdefault("_items", []).append(x)

    def __setitem__(self, k, v):
        self.__dict__.setdefault("_map", {})[k] = v


class _FakeStr(str):
    """renpy.ast.PyExpr：一段 Python 代码文本。"""

    def __new__(cls, s="", *a, **k):
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        if not isinstance(s, str):
            s = ""
        return str.__new__(cls, s)

    def __init__(self, *a, **k):
        pass

    def __setstate__(self, state):
        _apply_state(self, state)


class _FakeList(list):
    def __setstate__(self, state):
        _apply_state(self, state)


class _FakeDict(dict):
    def __setstate__(self, state):
        _apply_state(self, state)


class _FakeSet(set):
    def __setstate__(self, state):
        _apply_state(self, state)


_FAKE_CACHE = {}
_SAFE_BUILTINS = {"set", "frozenset", "list", "dict", "tuple", "object", "str", "bytes",
                  "bytearray", "int", "float", "complex", "bool", "slice", "range"}


def _fake_class(module, name):
    key = (module, name)
    cls = _FAKE_CACHE.get(key)
    if cls is None:
        if name == "PyExpr":
            base = _FakeStr
        elif name.endswith("List"):
            base = _FakeList
        elif name.endswith("Dict"):
            base = _FakeDict
        elif name.endswith("Set"):
            base = _FakeSet
        else:
            base = _FakeObj
        cls = type(str(name), (base,), {"_module": module})
        _FAKE_CACHE[key] = cls
    return cls


class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module in ("__builtin__", "builtins"):
            if name == "unicode":
                return str
            if name == "long":
                return int
            if name in _SAFE_BUILTINS:
                return getattr(builtins, name)
        if module in ("copy_reg", "copyreg") and name in ("_reconstructor", "__newobj__", "__newobj_ex__"):
            return getattr(copyreg, name)
        if module == "collections" and name in ("OrderedDict", "defaultdict", "deque"):
            return getattr(collections, name)
        if module == "_codecs" and name == "encode":
            return codecs.encode
        return _fake_class(module, name)


def _unpickle(raw):
    return _Unpickler(io.BytesIO(raw), encoding="utf-8", errors="replace").load()


def rpyc_statements(raw):
    """rpyc 文件内容 -> 顶层语句列表"""
    if raw[:10] == b"RENPY RPC2":
        pos, slots = 10, {}
        while pos + 12 <= len(raw):
            slot, start, length = struct.unpack("<III", raw[pos:pos + 12])
            pos += 12
            if slot == 0:
                break
            slots[slot] = (start, length)
        slot = 2 if 2 in slots else 1
        start, length = slots[slot]
        data = zlib.decompress(raw[start:start + length])
    else:
        data = zlib.decompress(raw)
    obj = _unpickle(data)
    if isinstance(obj, tuple) and len(obj) == 2 and isinstance(obj[1], list):
        return obj[1]
    if isinstance(obj, list):
        return obj
    return [obj]


class _IndexUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError("unexpected class in rpa index")


def rpa_entries(path):
    """读 .rpa 的目录，返回 {文件名: (offset, length, prefix)}；读不了返回 None"""
    with open(path, "rb") as f:
        header = f.readline(200)
        if not header.startswith(b"RPA-"):
            return None
        parts = header.split()
        if len(parts) < 2:
            return None
        try:
            offset = int(parts[1], 16)
            key = 0
            for p in parts[2:]:
                if re.fullmatch(rb"[0-9a-fA-F]{8}", p):
                    key ^= int(p, 16)
        except ValueError:
            return None
        f.seek(offset)
        index = _IndexUnpickler(io.BytesIO(zlib.decompress(f.read())), encoding="latin1").load()
    out = {}
    for name, items in index.items():
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if not items:
            continue
        it = items[0]
        off, length = it[0] ^ key, it[1] ^ key
        prefix = it[2] if len(it) > 2 else b""
        if isinstance(prefix, str):
            prefix = prefix.encode("latin1")
        out[name.replace("\\", "/")] = (off, length, prefix)
    return out


def rpa_read(path, entry):
    off, length, prefix = entry
    with open(path, "rb") as f:
        f.seek(off)
        return prefix + f.read(length - len(prefix))


def script_sources(root):
    """列出游戏的所有编译脚本：[(显示名, 读取函数)]，按文件名排序"""
    game = os.path.join(root, "game")
    loose = {}
    for dirpath, dirnames, filenames in os.walk(game):
        dirnames[:] = [d for d in dirnames if d.lower() not in ("cache", "saves", PATCH_DIR)]
        for fn in filenames:
            low = fn.lower()
            if (low.endswith(".rpyc") or low.endswith(".rpymc")) and not low.startswith("zz_cn"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, game).replace("\\", "/")
                loose[rel] = full
    out = {}
    for rel, full in loose.items():
        out[rel] = (lambda p=full: open(p, "rb").read())
    for fn in sorted(os.listdir(game)):
        if fn.lower().endswith(".rpa"):
            ap = os.path.join(game, fn)
            try:
                ents = rpa_entries(ap)
            except Exception:
                ents = None
            if not ents:
                continue
            for name, ent in ents.items():
                low = name.lower()
                if (low.endswith(".rpyc") or low.endswith(".rpymc")) and name not in out:
                    out[name] = (lambda p=ap, e=ent: rpa_read(p, e))
    return sorted(out.items())


def sources_signature(root):
    """脚本文件有没有变化的指纹（用来判断要不要重新读）"""
    h = hashlib.md5()
    game = os.path.join(root, "game")
    for dirpath, dirnames, filenames in os.walk(game):
        dirnames[:] = [d for d in dirnames if d.lower() not in ("cache", "saves", PATCH_DIR)]
        for fn in sorted(filenames):
            low = fn.lower()
            if low.endswith((".rpyc", ".rpymc", ".rpa")) and not low.startswith("zz_cn"):
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                h.update(("%s|%d|%d\n" % (os.path.relpath(p, game), st.st_size, int(st.st_mtime))).encode("utf-8"))
    return h.hexdigest()


# ============================================================== 判断哪些文本要翻

TOKEN_RE = re.compile(r"\[\[|\{\{|\[[^\[\]\n]*\]|\{[^{}\n]*\}")
LETTER_RE = re.compile(r"[A-Za-z\u00C0-\u024F\u0370-\u03FF\u0400-\u04FF\u3040-\u30FF\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF]")
FOREIGN_RE = re.compile(r"[A-Za-z\u00C0-\u024F\u0400-\u04FF\u3040-\u30FF\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
RES_RE = re.compile(
    r"\.(png|jpe?g|webp|gif|bmp|avif|svg|ogg|mp3|wav|opus|flac|ogv|webm|mp4|avi|mkv|"
    r"ttf|otf|ttc|woff2?|rpyc?|rpym|rpa|json|txt|py|moc3|model3|exp3|motion3|cdi3|cur|ico)$",
    re.I,
)
IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-/\\:]+$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def tokens(s):
    return Counter(TOKEN_RE.findall(s))


_CONV_FLAGS = frozenset("rstiqulcf!")


def _interp_spans(s):
    """照抄 Ren'Py 自己的变量解析（renpy/substitutions.py 的 parse，状态机逐行对应，连它的小怪癖也照搬），
    只是不取值，而是记下每个 [变量] 和 [[ 转义在原文里的起止位置 [(起, 止)]。Ren'Py 会报错的写法返回 None"""
    LITERAL, EXPRESSION, CONVERSION, FORMAT = 0, 1, 2, 3
    pos = -1
    size = len(s) - 1
    cut = mark = 0
    brackets = parens = 0
    conv = fmt = None
    state = LITERAL
    start = 0
    spans = []
    while pos < size:
        pos += 1
        c = s[pos]
        if state == LITERAL:
            if c == "[":
                cut = pos + 1
                if c == s[pos + 1:pos + 2]:
                    spans.append((pos, pos + 1))
                    pos += 1
                else:
                    state = EXPRESSION
                    start = pos
        elif state == EXPRESSION:
            if c == "(":
                parens += 1
            elif c == ")":
                if not parens:
                    return None
                parens -= 1
            elif c == '"' or c == "'":
                chars = 1
                found = 0
                if c * 2 == s[pos + 1:pos + 3]:
                    chars += 2
                    pos += 2
                while pos < size:
                    pos += 1
                    n = s[pos]
                    if n == c:
                        found += 1
                        if found == chars:
                            break
                    else:
                        if n == "\\":
                            pos += 1
                        found = 0
            elif parens:
                pass
            elif c == "[":
                brackets += 1
            elif c == "]":
                if brackets:
                    brackets -= 1
                else:
                    spans.append((start, pos))
                    cut = pos + 1
                    state = LITERAL
            elif brackets:
                pass
            elif c == "!":
                if s[pos + 1:pos + 2] == "=":
                    pos += 1
                else:
                    state = CONVERSION
                    cut = pos + 1
            elif c == ":":
                state = FORMAT
                cut = pos + 1
        elif state == CONVERSION:
            if c == "]":
                spans.append((start, pos))
                cut = pos + 1
                state = LITERAL
                fmt = None
            elif c == ":":
                state = FORMAT
                conv = s[cut:pos]
                cut = pos + 1
            elif c not in _CONV_FLAGS:
                if fmt is None:
                    return None
                state = FORMAT
                pos = cut
                cut = mark
        elif state == FORMAT:
            if c == "]":
                spans.append((start, pos))
                cut = pos + 1
                state = LITERAL
                conv = None
            elif conv is None and c == "!":
                state = CONVERSION
                fmt = s[cut:pos]
                mark = cut
                cut = pos + 1
    if state != LITERAL:
        return None
    return spans


def text_tokens(s):
    """拆出变量 [..] 和标签 {..}；变量的边界和 Ren'Py 自己算的一模一样
    （[player[0]]、["]" + player] 这种都算一整个）。[[ 和 {{ 是转义，也算一个记号。
    先拆变量、再在剩下的文字里找标签（Ren'Py 也是先替换变量、再解析标签）。
    Ren'Py 会直接报错的写法（括号没闭合之类）返回 None"""
    spans = _interp_spans(s)
    if spans is None:
        return None
    out, parts, last = [], [], 0
    for a, b in spans:
        out.append(s[a:b + 1])
        parts.append(s[last:a])
        parts.append("\0")  # 变量占一个位置，标签里套变量（{a=[url]}）也能认出来
        last = b + 1
    parts.append(s[last:])
    s = "".join(parts)
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "{":
            if s.startswith("{{", i):
                out.append("{{")
                i += 2
                continue
            j = s.find("}", i)
            if j < 0:
                return None
            out.append(s[i:j + 1])
            i = j + 1
            continue
        i += 1
    return out


def _tag_name(tag):
    body = tag[1:-1].strip()
    if body.startswith("/"):
        return "/", body[1:].strip()
    return "", re.split(r"[=\s]", body, 1)[0]


def _tags_nest_ok(tags, paired):
    """{b}..{/b} 这类成对标签要先开后关、层层对应；{w}、{p} 这种单个的不管"""
    stack = []
    for t in tags:
        kind, name = _tag_name(t)
        if kind == "/":
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        elif name in paired:
            stack.append(name)
    return not stack


def _bracket_shape(s):
    return "".join(ch for ch in s if ch in "[]{}")


def valid_translation(src, dst):
    """译文能不能安全放进游戏：变量一个不差、标签成对且顺序合法、没有多出半个括号。
    变量可以换位置（中文语序不同很正常），但内容必须一模一样，改了变量名游戏会报错"""
    if not isinstance(dst, str) or not dst.strip():
        return False
    if "[" not in src and "{" not in src and "[" not in dst and "{" not in dst:
        return True  # 绝大多数句子没有变量和标签，不用逐字解析（大游戏几万句，刷新状态快很多）
    ts = text_tokens(src)
    if ts is None:
        # 原文自己括号就不完整（多半不是显示给玩家的文字）：译文的括号得和原文一模一样
        return _bracket_shape(src) == _bracket_shape(dst) and tokens(src) == tokens(dst)
    td = text_tokens(dst)
    if td is None or Counter(ts) != Counter(td):
        return False
    tags_s = [t for t in ts if t.startswith("{") and t != "{{"]
    tags_d = [t for t in td if t.startswith("{") and t != "{{"]
    if tags_s == tags_d:
        return True
    paired = set(name for kind, name in map(_tag_name, tags_s) if kind == "/")
    if _tags_nest_ok(tags_s, paired):
        return _tags_nest_ok(tags_d, paired)
    return False  # 原文标签本来就不规整，只接受顺序完全一样的


def need_translate(s, src_lang=None):
    if not isinstance(s, str):
        return False
    t = s.strip()
    if not t or len(t) > 5000:
        return False
    bare = TOKEN_RE.sub("", t)
    han_source = src_lang in ("ja", "zh-TW")
    if not LETTER_RE.search(bare) and not (han_source and CJK_RE.search(bare)):
        return False
    if not han_source and CJK_RE.search(bare) and not FOREIGN_RE.search(bare):
        return False
    if RES_RE.search(t) or COLOR_RE.match(t):
        return False
    # 变量名、路径、网址（my_var、store.var、images/bg、http://x）不翻；
    # 但「Okay.」「Hmm...」「Note:」这种结尾带标点的短台词要翻（以前被当成变量名漏掉了）
    if IDENT_RE.match(t) and re.search(r"[_/\\]|\w[.:]\w", t):
        return False
    # 通配符（*.png、**/*.rpy）不翻；「*sigh*」「*sighs* Fine.」这种动作描写要翻（以前也漏了）
    if "*" in t and " " not in t and re.search(r"[./\\]\*|\*[./\\]|\*\*", t):
        return False
    return True


def looks_like_ui_text(s, src_lang=None):
    """代码里的普通字符串（没包在 _() 里）要更严格一点才算界面文字"""
    t = s.strip()
    if not need_translate(t, src_lang):
        return False
    if " " not in t and not t[:1].isupper() and not re.search(r"[^\x00-\x7f]", t):
        return False  # 小写单词多半是变量名、样式名
    if re.search(r"[=<>]{1}|\(\)|\bdef\b|\breturn\b", t) and " " in t and not re.search(r"[.!?…]$", t):
        if t.count("(") or t.count("="):
            return False
    return True


_WRAP_NAMES = {"_", "__", "_p", "renpy.notify", "Notify"}


def code_literals(src):
    """从一段 Python 代码里找出字符串常量 -> [(文本, 是否包在 _() 里)]"""
    out = []
    if not src or ("'" not in src and '"' not in src):
        return out
    try:
        prev = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING:
                s = tok.string
                prefix = re.match(r"^[A-Za-z]*", s).group(0).lower()
                if "f" in prefix or "b" in prefix:
                    prev = []
                    continue
                try:
                    v = pyast.literal_eval(s)
                except Exception:
                    v = None
                if isinstance(v, str):
                    wrapped = len(prev) >= 2 and prev[-1] == "(" and prev[-2] in ("_", "__", "_p")
                    out.append((v, wrapped))
            if tok.type in (tokenize.NAME, tokenize.OP):
                prev = (prev + [tok.string])[-3:]
            elif tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
                prev = []
    except Exception:
        pass
    return out


def _walk(roots):
    stack = list(reversed(roots))
    seen = set()
    while stack:
        o = stack.pop()
        if o is None or isinstance(o, (bool, int, float, bytes)):
            continue
        if isinstance(o, str) and not isinstance(o, _FakeStr):
            continue
        oid = id(o)
        if oid in seen:
            continue
        seen.add(oid)
        yield o
        children = []
        if isinstance(o, dict):
            children.extend(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            children.extend(o)
        d = getattr(o, "__dict__", None)
        if d:
            for k, v in d.items():
                if k != "next":
                    children.append(v)
        stack.extend(reversed(children))


def pycode_source(o):
    """PyCode 的源码：新版本存在 source 属性，旧/新版本也可能存在 __setstate__ 的元组里"""
    if o is None:
        return None
    src = getattr(o, "source", None)
    if isinstance(src, str):
        return src
    st = getattr(o, "_state", None)
    if isinstance(st, tuple) and len(st) >= 2 and isinstance(st[1], str):
        return st[1]
    return None


def extract_texts(root, log=None, src_lang="auto"):
    """读出一个游戏里需要翻的全部文本（按出现顺序去重）"""
    all_objs = []
    errors = 0
    for name, reader in script_sources(root):
        try:
            stmts = rpyc_statements(reader())
        except Exception as e:
            errors += 1
            if log:
                log("  读不了 %s（%s），跳过" % (name, type(e).__name__))
            continue
        all_objs.extend(_walk(stmts))

    # 找游戏默认语言（例如 last_missing 原文是俄语、默认显示英语）
    default_lang = None
    for o in all_objs:
        cn = type(o).__name__
        if cn == "Define" and getattr(o, "varname", None) == "default_language" and "config" in str(getattr(o, "store", "")):
            src = pycode_source(getattr(o, "code", None))
            try:
                v = pyast.literal_eval(str(src))
                if isinstance(v, str):
                    default_lang = v
            except Exception:
                pass
        elif isinstance(o, _FakeStr) or cn == "PyCode":
            src = o if isinstance(o, _FakeStr) else (pycode_source(o) or "")
            m = re.search(r"config\.default_language\s*=\s*u?(['\"])(\w+)\1", str(src))
            if m:
                default_lang = m.group(2)

    def lang(o):
        d = getattr(o, "__dict__", None) or {}
        return d.get("language")

    # 游戏实际显示的语言：默认语言有翻译的就用翻译，没有的用原文；其他语言的翻译不管
    covered_ids, covered_old = set(), set()
    if default_lang:
        for o in all_objs:
            cn = type(o).__name__
            if cn in ("Translate", "TranslateSay") and lang(o) == default_lang:
                covered_ids.add(getattr(o, "identifier", None))
            elif cn == "TranslateString" and lang(o) == default_lang:
                covered_old.add(getattr(o, "old", None))

    skip_ids = set()
    for o in all_objs:
        cn = type(o).__name__
        if cn not in ("Translate", "TranslateSay", "TranslateBlock", "TranslateEarlyBlock", "TranslatePython"):
            continue
        lg = lang(o)
        other_lang = lg is not None and lg != default_lang
        replaced = lg is None and cn in ("Translate", "TranslateSay") and getattr(o, "identifier", None) in covered_ids
        if other_lang or replaced:
            skip_ids.add(id(o))
            for s in _walk(list(getattr(o, "block", None) or [])):
                skip_ids.add(id(s))

    # 有假名的日文游戏也要提取纯汉字台词；全是汉字时可以在界面明确选择日语。
    if src_lang == "auto":
        visible_text = []
        for o in all_objs:
            if id(o) in skip_ids:
                continue
            if type(o).__name__ in ("Say", "TranslateSay"):
                visible_text.append(getattr(o, "what", ""))
            elif type(o).__name__ == "Menu":
                visible_text.extend(it[0] for it in (getattr(o, "items", None) or [])
                                    if isinstance(it, (tuple, list)) and it)
        src_lang = "ja" if any(isinstance(t, str) and re.search(r"[\u3040-\u30ff]", t)
                               for t in visible_text) else None

    # 角色定义：define r = Character("Rhys") 或 init python 里 r = Character(...)
    who_map = {}
    for o in all_objs:
        cn = type(o).__name__
        if cn in ("Define", "Default"):
            nm = _char_name_from_expr(pycode_source(getattr(o, "code", None)))
            vn = getattr(o, "varname", None)
            if nm is not None and isinstance(vn, str):
                who_map[vn] = nm
        elif cn == "PyCode":
            src = pycode_source(o)
            if src and "Character" in src:
                who_map.update(_char_names_from_module(src))

    def speaker_of(o):
        who = getattr(o, "who", None)
        if not who:
            return "旁白"
        who = str(who).strip()
        if who in who_map:
            return who_map[who] or "旁白"
        if who[:1] in "'\"":
            try:
                v = pyast.literal_eval(who)
                if isinstance(v, str):
                    return v
            except Exception:
                pass
        return who

    speakers, say_speaker, tl_pairs = {}, {}, []
    texts, seen = [], set()

    def add(s, strict=False):
        if not isinstance(s, str):
            return
        s = str(s)
        if s in seen or s in covered_old:
            return
        if (looks_like_ui_text(s, src_lang) if strict else need_translate(s, src_lang)):
            seen.add(s)
            texts.append(s)

    for o in all_objs:
        if id(o) in skip_ids:
            continue
        cn = type(o).__name__
        if isinstance(o, _FakeStr):
            for v, wrapped in code_literals(str(o)):
                add(v, strict=not wrapped)
            continue
        if cn in ("Say", "TranslateSay"):
            w = getattr(o, "what", None)
            add(w)
            if isinstance(w, str):
                say_speaker.setdefault(str(w), speaker_of(o))
                if w in seen and w not in speakers:
                    speakers[str(w)] = say_speaker[str(w)]
        elif cn == "Menu":
            for it in getattr(o, "items", None) or []:
                if isinstance(it, (tuple, list)) and it and isinstance(it[0], str):
                    add(it[0])
        elif cn == "TranslateString":
            lg = lang(o)
            if lg is None:
                add(getattr(o, "old", None))
            elif lg == default_lang:
                add(getattr(o, "new", None))
                tl_pairs.append((getattr(o, "old", None), getattr(o, "new", None)))
        elif cn == "PyCode":
            src = pycode_source(o)
            if isinstance(src, str) and not isinstance(src, _FakeStr):
                for v, wrapped in code_literals(src):
                    add(v, strict=not wrapped)
    # 对话是用「翻译成默认语言」的方式显示的（比如原文俄语、显示英语），说话人跟着原句走
    for old, new in tl_pairs:
        if isinstance(new, str) and new in seen and new not in speakers and old in say_speaker:
            speakers[new] = say_speaker[old]

    names = []
    for nm in who_map.values():
        if nm and nm not in names and not TOKEN_RE.search(nm) and need_translate(nm, src_lang):
            names.append(nm)
    return {"texts": texts, "default_language": default_lang, "source_language": src_lang, "errors": errors,
            "speakers": speakers, "names": names}


_CHAR_FUNCS = ("Character", "DynamicCharacter", "ADVCharacter", "NVLCharacter", "SpeechBubbleCharacter")


def _char_name_from_node(node):
    if not isinstance(node, pyast.Call):
        return None
    fn = node.func
    fname = fn.attr if isinstance(fn, pyast.Attribute) else getattr(fn, "id", "")
    if not (fname in _CHAR_FUNCS or str(fname).endswith("Character")):
        return None
    arg = node.args[0] if node.args else None
    if arg is None:
        for kw in node.keywords:
            if kw.arg == "name":
                arg = kw.value
    if isinstance(arg, pyast.Call) and getattr(arg.func, "id", "") in ("_", "__") and arg.args:
        arg = arg.args[0]
    if isinstance(arg, pyast.Constant) and isinstance(arg.value, str):
        return arg.value
    return ""  # Character(None) 之类：旁白


def _char_name_from_expr(src):
    if not src or "Character" not in src:
        return None
    try:
        return _char_name_from_node(pyast.parse(src.strip(), mode="eval").body)
    except Exception:
        return None


def _char_names_from_module(src):
    out = {}
    try:
        tree = pyast.parse(src)
    except Exception:
        return out
    for node in pyast.walk(tree):
        if isinstance(node, pyast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], pyast.Name):
            nm = _char_name_from_node(node.value)
            if nm is not None:
                out[node.targets[0].id] = nm
    return out


# ============================================================== 每个游戏的数据（缓存在工具的 data 文件夹里）

def game_id(root):
    base = re.sub(r"[^\w\-.]+", "_", os.path.basename(root.rstrip("\\/")))[:60]
    return "%s_%s" % (base, hashlib.md5(os.path.abspath(root).lower().encode("utf-8")).hexdigest()[:8])


def _data_path(root, kind):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "%s.%s.json" % (game_id(root), kind))


class DataFileError(Exception):
    pass


def read_json(path, default=None):
    """文件不存在才返回 default。文件坏了 / 读不了就报错停下——
    要是当成空的，翻译记录会被覆盖、还会把翻过的全部重新花钱翻一遍"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeDecodeError) as e:
        raise DataFileError("%s 内容坏了（%s）。为了不覆盖它、不重新花钱，先停下了。"
                            "用记事本打开修好，或者确认不要了再删掉它。文件在：%s"
                            % (os.path.basename(path), str(e)[:80], path))
    except OSError as e:
        raise DataFileError("读不了 %s（%s），可能被别的程序占用了。先停下了，免得覆盖它。"
                            % (os.path.basename(path), e))


def read_json_soft(path, default=None):
    """只用于坏了也无所谓、可以重新生成的缓存（文本列表、估价统计）"""
    try:
        return read_json(path, default)
    except DataFileError:
        return default


def write_json(path, data, indent=None):
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


TEXTS_VERSION = 3  # 改了「哪些文本要翻」的规则就加 1，旧的文本缓存会自动重新读一遍


def get_text_info(root, log=None, force=False, src_lang="auto"):
    """{"texts": [...], "speakers": {原文: 说话人}, "names": [角色名]}"""
    sig = sources_signature(root)
    p = _data_path(root, "texts")
    cached = read_json_soft(p)
    if (not force and isinstance(cached, dict) and cached.get("sig") == sig and "speakers" in cached
            and cached.get("v") == TEXTS_VERSION and cached.get("from_lang") == src_lang):
        return cached
    res = extract_texts(root, log=log, src_lang=src_lang)
    info = {"sig": sig, "v": TEXTS_VERSION, "texts": res["texts"], "default_language": res["default_language"],
            "speakers": res["speakers"], "names": res["names"], "from_lang": src_lang,
            "source_language": res["source_language"]}
    write_json(p, info)
    return info


def get_texts(root, log=None, force=False, src_lang="auto"):
    return get_text_info(root, log=log, force=force, src_lang=src_lang)["texts"]


def load_translations(root):
    return read_json(_data_path(root, "zh"), {}) or {}


def save_translations(root, done):
    write_json(_data_path(root, "zh"), done, indent=1)


UNKNOWN_MODEL = "不知道是哪个模型（旧记录）"


def models_in_usage_log(title):
    out = []
    try:
        with open(USAGE_LOG, "r", encoding="utf-8-sig") as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split(",")
                if len(parts) >= 3 and parts[1] == title and parts[2] not in out:
                    out.append(parts[2])
    except OSError:
        pass
    return out


def load_meta(root, done=None):
    p = _data_path(root, "zh_meta")
    meta = read_json(p, None)
    if meta is None:
        done = load_translations(root) if done is None else done
        meta = {}
        if done:
            models = models_in_usage_log(game_title(root))
            tag = models[0] if len(models) == 1 else UNKNOWN_MODEL
            meta = {t: tag for t in done}
        write_json(p, meta, indent=1)
    return meta


def save_meta(root, meta):
    write_json(_data_path(root, "zh_meta"), meta, indent=1)


def model_counts(root):
    done = load_translations(root)
    meta = load_meta(root, done)
    return Counter(meta.get(t, UNKNOWN_MODEL) for t in done)


def delete_translations(root, model):
    """删掉某个模型翻的译文（model=None 表示全部删）。返回删了几条。
    整个读-删-写都在 SAVE_LOCK 里，停止后才回来的请求不会和它同时写"""
    with SAVE_LOCK:
        done = load_translations(root)
        meta = load_meta(root, done)
        gone = [t for t in done if model is None or meta.get(t, UNKNOWN_MODEL) == model]
        for t in gone:
            done.pop(t, None)
            meta.pop(t, None)
        save_translations(root, done)
        save_meta(root, meta)
    with _UNSAVED_LOCK:  # 内存里没存进去的同一模型结果也一起丢掉，免得下次补存又冒出来
        pend = UNSAVED.get(game_id(root))
        if pend:
            for t in [t for t, m in pend["meta"].items() if model is None or m == model]:
                pend["zh"].pop(t, None)
                pend["meta"].pop(t, None)
    return len(gone)


def glossary_path(root):
    return _data_path(root, "glossary")


def load_glossary(root):
    return read_json(glossary_path(root), {}) or {}


def patch_installed(root):
    return os.path.exists(os.path.join(root, "game", PATCH_RPY))


def is_done(t, done):
    """翻过、而且译文能安全放进游戏的才算翻完（旧版本存下的标签坏掉的译文会重翻）"""
    return t in done and valid_translation(t, done[t])


def game_status(root, log=None, src_lang="auto"):
    """返回 dict：title, version, total, chars_left, left, installed"""
    texts = get_texts(root, log=log, src_lang=src_lang)
    done = load_translations(root)
    left = [t for t in texts if not is_done(t, done)]
    return {
        "title": game_title(root),
        "version": renpy_version(root),
        "total": len(texts),
        "left": len(left),
        "chars_left": sum(len(t) for t in left),
        "installed": patch_installed(root),
    }


# ============================================================== 花费

# 法定节假日（DeepSeek 高峰价不含这些日子）。来源：国务院办公厅关于 2026 年部分节假日安排的通知。
# 2027 年的安排出来后，在这里或 config.json 的 "holidays" 里补上就行。
HOLIDAYS = set(
    ["2026-01-%02d" % d for d in (1, 2, 3)]
    + ["2026-02-%02d" % d for d in range(15, 24)]
    + ["2026-04-%02d" % d for d in (4, 5, 6)]
    + ["2026-05-%02d" % d for d in (1, 2, 3, 4, 5)]
    + ["2026-06-%02d" % d for d in (19, 20, 21)]
    + ["2026-09-%02d" % d for d in (25, 26, 27)]
    + ["2026-10-%02d" % d for d in range(1, 8)]
)
_EXTRA_HOLIDAYS = set()


def is_peak(now=None):
    """DeepSeek 高峰：北京时间周一到周五（不含法定节假日）9-12 点、14-18 点"""
    now = now or datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if now.weekday() >= 5:
        return False
    day = now.strftime("%Y-%m-%d")
    if day in HOLIDAYS or day in _EXTRA_HOLIDAYS:
        return False
    h = now.hour
    return 9 <= h < 12 or 14 <= h < 18


def price_of(cfg):
    return (cfg.get("prices") or {}).get(cfg.get("model"))


def cost_of(cfg, hit, miss, out, peak=None):
    p = price_of(cfg)
    if not p:
        return None
    i = 1 if (is_peak() if peak is None else peak) else 0
    return (hit * p["hit"][i] + miss * p["miss"][i] + out * p["out"][i]) / 1e6


STATS_PATH = os.path.join(DATA_DIR, "model_stats.json")


def stats_key(cfg):
    return "%s|%s" % (cfg.get("model"), "think" if cfg.get("thinking") else "nothink")


def record_stats(cfg, chars, hit, miss, out):
    """记下这个模型每翻 1 个字实际用了多少 tokens，下次估价更准"""
    if chars <= 0:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    st = read_json_soft(STATS_PATH, {}) or {}
    k = stats_key(cfg)
    cur = st.get(k) or {"chars": 0, "hit": 0, "miss": 0, "out": 0}
    for name, v in (("chars", chars), ("hit", hit), ("miss", miss), ("out", out)):
        cur[name] = cur.get(name, 0) + v
    st[k] = cur
    write_json(STATS_PATH, st, indent=1)


def estimate_cost(cfg, chars, peak=None):
    """有实测数据就按实测比例估；没有就按经验值（开了深度思考的话输出会多好几倍）"""
    if chars <= 0:
        return 0.0
    st = (read_json_soft(STATS_PATH, {}) or {}).get(stats_key(cfg))
    if st and st.get("chars", 0) >= 3000:
        r = float(chars) / st["chars"]
        return cost_of(cfg, st["hit"] * r, st["miss"] * r, st["out"] * r, peak)
    batches = max(1, chars // max(500, int(cfg.get("batch_chars", 2500)) // 2))
    miss = chars / 3.5 + 600
    hit = batches * 550
    out = chars * (1.7 if cfg.get("thinking") else 0.35) + batches * 40
    return cost_of(cfg, hit, miss, out, peak)


# ============================================================== 翻译

SYSTEM_PROMPT = """你是资深游戏本地化译者，负责把视觉小说/游戏里的文本翻译成简体中文。
用户会发来一个 JSON 对象，键是编号，值是原文。请返回结构完全相同的 JSON 对象：键不变，值换成简体中文译文。
规则：
1. 每个键都要返回，不要增删键，不要把几条合并成一条，也不要拆开。
2. 方括号 [ ] 和花括号 { } 里的内容是程序标签或变量（例如 [player_name]、[mc]、{i}、{/i}、{b}、{w}、{w=0.5}、{color=#ffffff}、{size=+10}、{#context}），必须一模一样地保留，数量相同，不要翻译里面的内容，不要改成中文括号【】。原文里的 [[ 和 {{ 也要原样保留。
3. 原文里的换行照样保留。
4. 译文要像中文游戏里角色自然说出来的话：口语化，保留原文的语气、情绪和粗口程度，不要书面腔，不要加任何解释、注释或括号说明。
5. 人名、地名用常见音译并前后保持一致；界面按钮类短词（如 Start、Load、Yes、Preferences）用中文游戏里的常见说法。
6. 原文已经是中文、或者只是符号/代码/文件名的，原样返回。
7. 用户可能附带「前文」和「说话人」：只用来理解语境（谁在说、对谁说、男女、关系、语气），让译文和前文连贯、人称和称呼对得上。前文不用翻译，也不要出现在输出里。
8. 有「名词表」的话，表里的译名必须照用，整部游戏保持一致。
只输出 JSON，不要输出任何其他内容。"""


PAUSE = threading.Event()  # 设上 = 暂停：不再发新请求
_INFLIGHT = [0]
_INFLIGHT_LOCK = threading.Lock()
SAVE_LOCK = threading.Lock()  # 存翻译记录时用：先读盘再合并，晚回来的请求也不会覆盖掉新结果


INFLIGHT_TEXTS = set()  # (游戏 id, 原文)：交出去翻译、结果还没存好的句子——从发请求一直算到写盘完成

UNSAVED = {}  # 游戏 id -> {"root": 游戏目录, "zh": {原文: 译文}, "meta": {原文: 模型}}：钱花了、但写盘失败的结果
_UNSAVED_LOCK = threading.Lock()


def inflight():
    """正在等接口回复的请求批数"""
    return _INFLIGHT[0]


def busy_texts(root=None):
    """还没存好的句子数（root=None 表示所有游戏）"""
    with _INFLIGHT_LOCK:
        if root is None:
            return len(INFLIGHT_TEXTS)
        gid = game_id(root)
        return sum(1 for g, t in INFLIGHT_TEXTS if g == gid)


def _write_records(root, zh, models):
    """把译文和对应模型合并进硬盘上的记录（先读盘再合并，不会盖掉别处刚存的）"""
    with SAVE_LOCK:
        disk = load_translations(root)
        disk.update(zh)
        save_translations(root, disk)
        dmeta = read_json(_data_path(root, "zh_meta"), {}) or {}
        dmeta.update(models)
        save_meta(root, dmeta)


def _stash_unsaved(root, zh, models):
    with _UNSAVED_LOCK:
        cur = UNSAVED.setdefault(game_id(root), {"root": root, "zh": {}, "meta": {}})
        for k, new in (("zh", zh), ("meta", models)):
            merged = dict(new)
            merged.update(cur[k])  # 已经在里面的是更新的结果
            cur[k] = merged


def unsaved_count():
    with _UNSAVED_LOCK:
        return sum(len(b["zh"]) for b in UNSAVED.values())


def flush_unsaved():
    """把内存里还没存进硬盘的译文再写一次。返回还剩多少条没存进去"""
    with _UNSAVED_LOCK:
        gids = list(UNSAVED)
    for gid in gids:
        with _UNSAVED_LOCK:
            b = UNSAVED.pop(gid, None)
        if not b:
            continue
        try:
            _write_records(b["root"], b["zh"], b["meta"])
        except Exception:
            _stash_unsaved(b["root"], b["zh"], b["meta"])
    return unsaved_count()


def wait_if_paused(stop):
    while PAUSE.is_set() and not stop.is_set():
        time.sleep(0.2)
    if stop.is_set():
        raise StopRequested()


class StopRequested(Exception):
    pass


class AuthError(Exception):
    pass


class SaveError(Exception):
    """译文写不进硬盘：任务停下，不再发请求"""
    pass


class ApiResponseError(RuntimeError):
    """接口返回了 200 但内容不对（缺 choices 之类），原样重发多半还是这样"""
    pass


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


class Usage(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.hit = self.miss = self.out = 0
        self.cost = 0.0
        self.cost_known = True

    def add(self, cfg, usage):
        if not usage:
            return
        pt = int(usage.get("prompt_tokens") or 0)
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or max(0, pt - hit))
        out = int(usage.get("completion_tokens") or 0)
        c = cost_of(cfg, hit, miss, out)
        with self.lock:
            self.hit += hit
            self.miss += miss
            self.out += out
            if c is None:
                self.cost_known = False
            else:
                self.cost += c

    def text(self):
        s = "输入 %d tokens（缓存命中 %d），输出 %d tokens" % (self.hit + self.miss, self.hit, self.out)
        if self.cost_known:
            s += "，约 ¥%.4f" % self.cost
        else:
            s += "（这个模型没有价格表，算不了钱）"
        return s


def call_api(cfg, messages, usage):
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg.get("temperature", 0.7),
        "stream": False,
    }
    if cfg.get("json_mode"):
        body["response_format"] = {"type": "json_object"}
    if "deepseek" in cfg.get("base_url", "").lower() and not cfg.get("_no_thinking_param"):
        # DeepSeek 默认开着「深度思考」，翻译用不着，关掉能省很多输出 tokens
        body["thinking"] = {"type": "enabled" if cfg.get("thinking") else "disabled"}
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]},
    )
    kw = {"timeout": cfg.get("timeout", 180)}
    if url.lower().startswith("https"):
        kw["context"] = SSL_CTX
    with urllib.request.urlopen(req, **kw) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if not isinstance(resp, dict):
        raise ApiResponseError("接口返回的不是 JSON 对象：%s" % str(resp)[:200])
    usage.add(cfg, resp.get("usage"))  # 到这一步已经计费了，先记上
    try:
        msg = resp["choices"][0]["message"]
        content = msg.get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ApiResponseError("接口返回的内容缺东西（没有 choices/message）：%s" % str(resp)[:200])
    if content is not None and not isinstance(content, str):
        raise ApiResponseError("接口返回的 message.content 不是文字：%s" % str(content)[:200])
    return content or ""


def call_with_retry(cfg, messages, usage, stop, tries=4):
    """只对网络问题、限流（429）、服务器错误（5xx）原样重发。
    请求本身有问题（普通 400 等）、返回内容格式不对，原样重发也是白花钱，直接报给上层（上层会拆小批次再试）"""
    delay, last = 3, ""
    for attempt in range(tries):
        wait_if_paused(stop)  # 暂停期间连重试也不发；停止了就直接退出
        wait_s = None
        try:
            return call_api(cfg, messages, usage)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            low = detail.lower()
            if e.code in (401, 403):
                raise AuthError("key 不对或没有权限（HTTP %d）%s" % (e.code, detail))
            if e.code == 402:
                raise AuthError("账户余额不足（HTTP 402）")
            if e.code == 404:
                raise AuthError("接口地址或模型名不对（HTTP 404）%s" % detail)
            if e.code == 400 and cfg.get("json_mode") and "response_format" in low:
                cfg["json_mode"] = False
                continue
            if e.code == 400 and "thinking" in low and not cfg.get("_no_thinking_param"):
                if not cfg.get("thinking"):
                    # 不能偷偷去掉参数接着发：服务端默认可能是开着思考的，会贵好几倍
                    raise AuthError("这个接口不接受「关闭深度思考」的参数，没法确认思考是关着的（开着贵好几倍），"
                                    "先停下了。换成官方 DeepSeek 地址，或者确实想开思考就勾上「深度思考」。%s" % detail)
                cfg["_no_thinking_param"] = True  # 本来就要开思考：去掉参数用服务端默认，不会多花冤枉钱
                continue
            if e.code == 400 and "model" in low and not re.search(r"context|length|token", low):
                raise AuthError("模型名不对（HTTP 400）%s" % detail)
            if e.code in (400, 413, 422):
                raise RuntimeError("HTTP %d %s" % (e.code, detail))
            last = "HTTP %d %s" % (e.code, detail)
            if e.code == 429:
                try:
                    wait_s = min(60, max(1, int(float(e.headers.get("Retry-After")))))
                except Exception:
                    wait_s = None
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = repr(e)
        if attempt < tries - 1:
            for _ in range(int((wait_s or delay) * 10)):
                if stop.is_set():
                    raise StopRequested()
                time.sleep(0.1)
            delay = min(delay * 2, 60)
    raise RuntimeError(last)


def parse_reply(text):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def system_prompt(cfg, game_glossary=None):
    p = SYSTEM_PROMPT
    p += source_language_rule(cfg)
    gl = dict(game_glossary or {})
    gl.update({k: v for k, v in (cfg.get("glossary") or {}).items() if k and v})
    gl = {k: v for k, v in gl.items() if k and v}
    if gl:
        p += "\n\n名词表（必须遵守）：\n" + "\n".join("- %s → %s" % (k, v) for k, v in gl.items())
    if cfg.get("extra_prompt"):
        p += "\n\n额外要求：" + cfg["extra_prompt"]
    return p


def source_language_rule(cfg):
    return {
        "ja": "\n原文是日语：纯汉字的日文也必须翻成简体中文，不要当作已经是中文（如「開始」→「开始」）。",
        "zh-TW": "\n原文是繁体中文：必须转成简体中文，不能因为原文是中文就原样返回。",
    }.get(cfg.get("renpy_from_lang"), "")


GLOSSARY_PROMPT = """你是资深游戏本地化译者。用户会发来一个游戏里的角色名/专有名词列表（JSON 数组）。
请给每个名字定一个简体中文译名，返回 JSON 对象 {原名: 译名}。
人名用常见、简短自然的音译；有含义的称号或绰号按意思译；已经是中文的原样返回；看不出是什么的代号、缩写保留原文。只输出 JSON。"""


def ensure_glossary(cfg, root, names, usage, stop, log):
    """翻译前先把角色名定下来，整部游戏统一用；结果存在工具的 data 文件夹，可以手动改"""
    gl = load_glossary(root)
    missing = [n for n in names if n not in gl][:200]
    if not missing:
        return gl
    wait_if_paused(stop)
    c = dict(cfg)
    c["json_mode"] = True
    msgs = [{"role": "system", "content": GLOSSARY_PROMPT + source_language_rule(cfg)},
            {"role": "user", "content": json.dumps(missing, ensure_ascii=False)}]
    try:
        obj = parse_reply(call_with_retry(c, msgs, usage, stop))
    except (AuthError, StopRequested, ApiResponseError):
        raise
    except Exception as e:
        log("  定角色译名失败（%s），这次先不用名词表" % str(e)[:80])
        return gl
    added = 0
    if isinstance(obj, dict):
        for n in missing:
            v = obj.get(n)
            if isinstance(v, str) and v.strip() and not TOKEN_RE.search(v):
                gl[n] = v.strip()
                added += 1
    write_json(glossary_path(root), gl, indent=1)
    if added:
        log("  定好了 %d 个角色名的译名（名词表），整部游戏统一用" % added)
    return gl


def _restore_spaces(orig, trans):
    lead = orig[: len(orig) - len(orig.lstrip())]
    trail = orig[len(orig.rstrip()):]
    return lead + trans.strip() + trail


def translate_batch(cfg, sp, texts, usage, stop, ctx=None, speakers=None):
    wait_if_paused(stop)
    payload = {str(i + 1): t for i, t in enumerate(texts)}
    parts = []
    if ctx:
        parts.append("前文（只作参考，不用翻译，也不要输出）：\n" + "\n".join(ctx))
    spk = {str(i + 1): speakers[t] for i, t in enumerate(texts) if speakers and speakers.get(t)}
    if spk:
        parts.append("说话人（编号: 角色）：" + json.dumps(spk, ensure_ascii=False))
    parts.append(("请翻译下面的 JSON：\n" if parts else "") + json.dumps(payload, ensure_ascii=False))
    msgs = [{"role": "system", "content": sp},
            {"role": "user", "content": "\n\n".join(parts)}]
    with _INFLIGHT_LOCK:
        _INFLIGHT[0] += 1
    try:
        reply = call_with_retry(cfg, msgs, usage, stop)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT[0] -= 1
    try:
        obj = parse_reply(reply)
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for i, t in enumerate(texts):
        v = obj.get(str(i + 1))
        if isinstance(v, str) and valid_translation(t, v):
            out[t] = _restore_spaces(t, v)
    return out


def _batches(items, size, max_chars):
    b, n = [], 0
    for t in items:
        if b and (len(b) >= size or n + len(t) > max_chars):
            yield b
            b, n = [], 0
        b.append(t)
        n += len(t)
    if b:
        yield b


def translate_game(cfg, root, usage, stop, log, progress):
    """翻译一个游戏还没翻的文本，边翻边存。返回 (已翻, 总数, 失败数, 这次翻了多少字)"""
    info = get_text_info(root, log=log, src_lang=cfg.get("renpy_from_lang", "auto"))
    cfg = dict(cfg)
    cfg["renpy_from_lang"] = info.get("source_language") or cfg.get("renpy_from_lang", "auto")
    texts = info["texts"]
    speakers = info.get("speakers") or {}
    index = {t: i for i, t in enumerate(texts)}
    done = load_translations(root)
    meta = load_meta(root, done)
    model = cfg.get("model") or "?"
    use_ctx = cfg.get("context", True)
    total = len(texts)
    lock = threading.Lock()
    stats = {"chars": 0}
    gid = game_id(root)
    send_ok = threading.Event()  # 清掉 = 写盘出问题了，排队的批次先别发（重试写盘期间也不花钱）
    send_ok.set()

    def store(res):
        """先写盘，写成功了才算数。写不进去（硬盘满、被占用）：结果留在内存里，抛 SaveError 让整个任务停下"""
        with _UNSAVED_LOCK:
            old = UNSAVED.pop(gid, None)
        if not res and not old:
            return
        zh = dict(old["zh"]) if old else {}
        models = dict(old["meta"]) if old else {}
        zh.update(res or {})
        models.update({t: model for t in (res or {})})
        err = None
        for attempt in range(3):
            try:
                _write_records(root, zh, models)
                err = None
                break
            except Exception as e:  # 杀毒软件临时占用之类的，等一下再试
                err = e
                send_ok.clear()
                if attempt < 2:
                    time.sleep(1)
        if err is None:
            send_ok.set()
        else:
            _stash_unsaved(root, zh, models)
            raise SaveError("译文写不进硬盘（%s）" % err)
        with lock:
            done.update(zh)
            meta.update(models)
            stats["chars"] += sum(len(t) for t in (res or {}))

    # 上次写盘失败、还留在内存里的结果：先补存，存不进去就别再发请求花钱
    with _UNSAVED_LOCK:
        n_pend = len((UNSAVED.get(gid) or {}).get("zh") or {})
    if n_pend:
        store({})
        log("  上次没存进硬盘的 %d 条已经补存好了（没重新花钱）" % n_pend)

    gl = {}
    left_all = [t for t in texts if not is_done(t, done)]
    if use_ctx and left_all:
        gl = ensure_glossary(cfg, root, info.get("names") or [], usage, stop, log)
        # 角色名本身（比如名字框里显示的）直接用名词表，不用再翻
        pre = {t: gl[t] for t in left_all if t in gl and valid_translation(t, gl[t])}
        if pre:
            store(pre)
            left_all = [t for t in left_all if t not in pre]
    bad_old = sum(1 for t in left_all if t in done)
    if bad_old:
        log("  有 %d 条旧译文的变量/标签对不上（放进游戏可能报错），这次重翻" % bad_old)
    with _INFLIGHT_LOCK:
        busy = set(t for g, t in INFLIGHT_TEXTS if g == gid)  # 只看同一个游戏的
    todo = [t for t in left_all if t not in busy]
    if len(todo) < len(left_all):
        log("  有 %d 条上一轮已经发出去了，回来会自动存，这次先不重复翻（不重复花钱）"
            % (len(left_all) - len(todo)))
    state = {"n": total - len(left_all)}
    progress(state["n"], total)
    if not todo:
        return state["n"], total, 0, stats["chars"]
    sp = system_prompt(cfg, gl)

    def context_for(batch):
        if not use_ctx:
            return None
        i0 = index.get(batch[0], 0)
        lines = []
        for t in texts[max(0, i0 - 6):i0]:
            if len(t) > 300:
                continue
            who = speakers.get(t)
            line = ("%s：" % who if who else "") + t
            tr = done.get(t)
            if tr:
                line += "  →  " + tr
            lines.append(line)
        return lines

    def run_round(items, size, max_chars):
        failed = []
        if not items:
            return failed
        ex = ThreadPoolExecutor(max_workers=max(1, int(cfg.get("threads", 4))))

        def job(b, ctx):
            while not send_ok.wait(0.2):
                if stop.is_set():
                    raise StopRequested()
            if stop.is_set():
                raise StopRequested()
            with _INFLIGHT_LOCK:  # 从这里开始算「在路上」，一直到结果存好（见 release）
                INFLIGHT_TEXTS.update((gid, t) for t in b)
            return translate_batch(cfg, sp, b, usage, stop, ctx, speakers if use_ctx else None)

        def release(b):
            with _INFLIGHT_LOCK:
                INFLIGHT_TEXTS.difference_update((gid, t) for t in b)

        futs = {}
        for b in _batches(items, size, max_chars):
            futs[ex.submit(job, b, context_for(b))] = b
        pending = set(futs)
        processed = set()

        def late(fut):
            # 停止后才回来的请求：结果照样存下来，不浪费（存不进去的留在内存里，下次开始前补存）
            try:
                if not fut.cancelled() and fut.exception() is None:
                    store(fut.result())
            except Exception:
                pass
            finally:
                release(futs[fut])

        try:
            while pending:
                if stop.is_set():
                    raise StopRequested()
                finished, pending = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
                fatal = None
                for fut in finished:
                    processed.add(fut)
                    b = futs[fut]
                    try:
                        try:
                            res = fut.result()
                        except (AuthError, StopRequested, ApiResponseError) as e:
                            # 返回格式不对：拆小批次、逐句再试也只会一样，多试就是多花钱，直接停
                            fatal = fatal or e
                            continue
                        except Exception as e:
                            log("  有一批请求失败（%s），稍后重试" % str(e)[:120])
                            res = {}
                        store(res)
                    finally:
                        release(b)  # 存好（或者确定存不进去、已留在内存）之后才算这批结束
                    with lock:
                        state["n"] += len(res)
                        failed.extend(t for t in b if t not in res)
                    progress(state["n"], total)
                if fatal is not None:
                    raise fatal
        except BaseException:
            # 不管是停止、key 不对，还是写盘失败：排队的请求全部取消，不再花钱；
            # 已经发出去的回来后照样存（同一轮里已回来、还没处理的也一样）
            stop.set()
            for f in futs:
                if f in processed:
                    continue
                if not f.cancel():
                    f.add_done_callback(late)
            ex.shutdown(wait=False)
            raise
        ex.shutdown(wait=True)
        return failed

    failed = run_round(todo, int(cfg.get("batch_size", 20)), int(cfg.get("batch_chars", 1500)))
    if failed:
        log("  %d 条没翻好，拆小了重试" % len(failed))
        failed = run_round(failed, 5, 800)
    if failed:
        log("  还剩 %d 条，一条一条再试" % len(failed))
        failed = run_round(failed, 1, 10 ** 6)
    return state["n"], total, len(failed), stats["chars"]


def log_usage(game, cfg, usage_before, usage_after):
    hit = usage_after[0] - usage_before[0]
    miss = usage_after[1] - usage_before[1]
    out = usage_after[2] - usage_before[2]
    if hit + miss + out == 0:
        return
    c = cost_of(cfg, hit, miss, out)
    new = not os.path.exists(USAGE_LOG)
    with open(USAGE_LOG, "a", encoding="utf-8-sig") as f:
        if new:
            f.write("时间,游戏,模型,输入tokens(缓存命中),输入tokens(未命中),输出tokens,花费(元)\n")
        f.write("%s,%s,%s,%d,%d,%d,%s\n" % (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), game.replace(",", " "), cfg.get("model"),
            hit, miss, out, "" if c is None else "%.4f" % c))


# ============================================================== 装补丁 / 还原

# (显示名, 文件名, 系统里的字体名——Unity 用这个名字)
FONT_CANDIDATES = [
    ("微软雅黑", "msyh.ttc", "Microsoft YaHei"),
    ("微软雅黑", "msyh.ttf", "Microsoft YaHei"),
    ("黑体", "simhei.ttf", "SimHei"),
    ("等线", "Deng.ttf", "DengXian"),
    ("宋体", "simsun.ttc", "SimSun"),
    ("楷体", "simkai.ttf", "KaiTi"),
    ("仿宋", "simfang.ttf", "FangSong"),
    ("微软正黑", "msjh.ttc", "Microsoft JhengHei"),
    ("幼圆", "SIMYOU.TTF", "YouYuan"),
    ("隶书", "SIMLI.TTF", "LiSu"),
    ("华文细黑", "STXIHEI.TTF", "STXihei"),
    ("华文楷体", "STKAITI.TTF", "STKaiti"),
    ("华文宋体", "STSONG.TTF", "STSong"),
    ("华文仿宋", "STFANGSO.TTF", "STFangsong"),
    ("华文中宋", "STZHONGS.TTF", "STZhongsong"),
    ("华文行楷", "STXINGKA.TTF", "STXingkai"),
    ("华文新魏", "STXINWEI.TTF", "STXinwei"),
    ("华文琥珀", "STHUPO.TTF", "STHupo"),
    ("华文彩云", "STCAIYUN.TTF", "STCaiyun"),
    ("方正舒体", "FZSTK.TTF", "FZShuTi"),
    ("方正姚体", "FZYTK.TTF", "FZYaoti"),
]


def _font_dirs():
    windir = os.environ.get("WINDIR", r"C:\Windows")
    return [os.path.join(windir, "Fonts"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")]


def list_cn_fonts():
    """电脑上装着的中文字体：[(显示名, 路径, 字体名)]"""
    out, seen = [], set()
    for disp, fn, fam in FONT_CANDIDATES:
        if disp in seen:
            continue
        for d in _font_dirs():
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                out.append((disp, p, fam))
                seen.add(disp)
                break
    return out


def find_system_font(cfg):
    if cfg.get("font") and os.path.isfile(cfg["font"]):
        return cfg["font"]
    fonts = list_cn_fonts()
    return fonts[0][1] if fonts else None


def font_family(cfg):
    """Unity 插件要的是系统字体名；自己选的字体文件 Unity 用不了，就用微软雅黑"""
    p = cfg.get("font") or ""
    base = os.path.basename(p).lower()
    for disp, fn, fam in FONT_CANDIDATES:
        if fn.lower() == base:
            return fam
    return "Microsoft YaHei"


def font_display(cfg):
    p = cfg.get("font") or ""
    if not p:
        fonts = list_cn_fonts()
        return fonts[0][0] if fonts else "（没找到中文字体）"
    base = os.path.basename(p).lower()
    for disp, fn, fam in FONT_CANDIDATES:
        if fn.lower() == base:
            return disp
    return os.path.basename(p)


PATCH_TEMPLATE = r'''# 中文补丁（由「游戏汉化工具」生成）
# 不改动游戏原文件。想还原：在工具里点「还原」，或者删掉本文件、同名 .rpyc 和 zz_cn 文件夹。

init 999 python:
    def _zzcn_setup():
        import json
        import renpy.translation as _tr

        try:
            _open = getattr(renpy, "open_file", None) or renpy.file
            _f = _open("zz_cn/zh_map.json")
            _raw = _f.read()
            _f.close()
            if isinstance(_raw, bytes):
                _raw = _raw.decode("utf-8")
            _tr._zzcn_map = json.loads(_raw)
        except Exception:
            return

        # 对话和选项
        _prev = config.say_menu_text_filter
        if not getattr(_prev, "_zzcn", False):
            def _filter(s):
                try:
                    _t = _tr._zzcn_map.get(s)
                    if _t is not None:
                        s = _t
                except Exception:
                    pass
                if _prev is not None:
                    s = _prev(s)
                return s
            _filter._zzcn = True
            config.say_menu_text_filter = _filter

        # 界面文字、人名等
        if getattr(_tr, "_zzcn_orig", None) is None:
            _tr._zzcn_orig = _tr.translate_string
            def _ts(s, *args, **kwargs):
                r = _tr._zzcn_orig(s, *args, **kwargs)
                try:
                    _m = _tr._zzcn_map
                    _t = _m.get(r)
                    if _t is None:
                        _t = _m.get(s)
                    if _t is not None:
                        return _t
                except Exception:
                    pass
                return r
            _tr.translate_string = _ts

        # 中文字体
        _font = "%(font)s"
        if _font and renpy.loadable(_font):
            class _FontMap(dict):
                def get(self, key, default=None):
                    try:
                        return (_font, key[1], key[2])
                    except Exception:
                        return default
            config.font_replacement_map = _FontMap()

        # 中文按字换行
        try:
            style.default.language = "unicode"
        except Exception:
            pass

    _zzcn_setup()

    # 全屏打开：工具里勾了「游戏全屏打开」，zz_cn 文件夹里就有 fullscreen.txt。
    # 这段在游戏开窗口之前运行，窗口一出来就是全屏；游戏里按 F 还能切回窗口，下次打开又是全屏。
    try:
        if renpy.loadable("zz_cn/fullscreen.txt"):
            _preferences.fullscreen = True
    except Exception:
        pass
'''


def install_patch(root, cfg, log):
    texts = get_texts(root, log=log, src_lang=cfg.get("renpy_from_lang", "auto"))
    done = load_translations(root)
    m = {t: done[t] for t in texts if t in done and done[t] != t and valid_translation(t, done[t])}
    bad = sum(1 for t in texts if t in done and not valid_translation(t, done[t]))
    if bad:
        log("  %d 条旧译文的变量/标签对不上，补丁里先用原文（免得游戏报错），下次翻译会重翻它们" % bad)
    game = os.path.join(root, "game")
    pdir = os.path.join(game, PATCH_DIR)
    os.makedirs(pdir, exist_ok=True)
    write_json(os.path.join(pdir, MAP_NAME), m)

    font_rel = ""
    src = find_system_font(cfg)
    if src:
        ext = os.path.splitext(src)[1].lower()
        dst = os.path.join(pdir, "cn_font" + ext)
        marker = os.path.join(pdir, "font_source.txt")
        old_src = ""
        if os.path.exists(marker):
            with open(marker, "r", encoding="utf-8") as f:
                old_src = f.read().strip()
        if old_src != src or not os.path.exists(dst):
            # 先复制到临时文件、完整了再换上：硬盘满了也不会留下半个字体文件让游戏打不开
            tmp = dst + ".tmp"
            try:
                shutil.copyfile(src, tmp)
                for fn in os.listdir(pdir):
                    if fn.startswith("cn_font.") and not fn.endswith(".tmp"):
                        os.remove(os.path.join(pdir, fn))
                os.replace(tmp, dst)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            _write_text_atomic(marker, src)
        font_rel = PATCH_DIR + "/cn_font" + ext
    else:
        log("  没找到中文字体，游戏里中文可能显示成方块。在工具上面的「中文字体」里选一个字体文件。")

    # 补丁脚本同样先写临时文件再换上：写到一半失败的话，游戏里还是旧的完整补丁，不会启动报错
    _write_text_atomic(os.path.join(game, PATCH_RPY), PATCH_TEMPLATE % {"font": font_rel})
    set_fullscreen_flag(root, cfg.get("game_fullscreen", True))
    return len(m)


FULLSCREEN_FLAG = "fullscreen.txt"


def set_fullscreen_flag(root, on):
    """补丁文件夹里放 / 删一个标记文件，补丁看到它就让游戏全屏打开"""
    pdir = os.path.join(root, "game", PATCH_DIR)
    p = os.path.join(pdir, FULLSCREEN_FLAG)
    if on:
        if os.path.isdir(pdir) and not os.path.exists(p):
            _write_text_atomic(p, "游戏汉化工具：有这个文件，游戏就全屏打开。不想全屏的话，在工具里取消勾选「游戏全屏打开」。\n")
    elif os.path.exists(p):
        os.remove(p)


def update_patch_script(root):
    """已经装好的补丁脚本换成最新版本（只换脚本，不动译文和字体，不花钱）"""
    game = os.path.join(root, "game")
    p = os.path.join(game, PATCH_RPY)
    pdir = os.path.join(game, PATCH_DIR)
    if not os.path.isfile(p):
        return False
    fonts = sorted(fn for fn in os.listdir(pdir) if fn.startswith("cn_font.") and not fn.endswith(".tmp")) \
        if os.path.isdir(pdir) else []
    new = PATCH_TEMPLATE % {"font": PATCH_DIR + "/" + fonts[0] if fonts else ""}
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        old = f.read()
    if old == new:
        return False
    _write_text_atomic(p, new)
    return True


def _write_text_atomic(path, text):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def remove_patch(root):
    game = os.path.join(root, "game")
    removed = False
    for fn in (PATCH_RPY, PATCH_RPY + "c"):
        p = os.path.join(game, fn)
        if os.path.exists(p):
            os.remove(p)
            removed = True
    pdir = os.path.join(game, PATCH_DIR)
    if os.path.isdir(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
        removed = True
    return removed
