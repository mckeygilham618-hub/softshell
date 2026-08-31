# -*- coding: utf-8 -*-
"""Softshell —— 本地桥接
把 Claude Code CLI 接到聊天软件风格网页界面。支持发图片、发表情包。
双击桌面的「Softshell」快捷方式启动；再次双击只会重新打开窗口，不会重复启动。
想彻底退出后台：任务管理器结束 pythonw.exe。

表情包：往 stickers 下的任意子文件夹丢图片就行，子文件夹名就是面板上的页签名。
        不用重启，聊天窗口每次点开表情面板都会重新扫一遍。
"""
import io
import json
import os
import re
import shutil
import subprocess
import threading
import webbrowser
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")


def find_claude():
    """找 claude CLI。先看 PATH，再看官方安装器的默认位置。"""
    hit = shutil.which("claude")
    if hit:
        return hit
    for p in (
        os.path.join(HOME, ".local", "bin", "claude.exe"),
        os.path.join(HOME, "AppData", "Local", "Programs", "claude", "claude.exe"),
        os.path.join(HOME, ".claude", "local", "claude.exe"),
    ):
        if os.path.isfile(p):
            return p
    return None


def find_browser():
    """找一个能开无地址栏应用窗口的浏览器：Edge 优先，其次 Chrome。
    都没有就返回 None，改用系统默认浏览器打开（会带地址栏，但能用）。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local"))
    cands = [
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(lad, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in cands:
        if os.path.isfile(p):
            return p
    for name in ("msedge", "chrome"):
        hit = shutil.which(name)
        if hit:
            return hit
    return None


CLAUDE = find_claude()
BROWSER = find_browser()
PORT = 8618
DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(DIR, "session.txt")      # 旧版单会话指针，仅迁移用
SESSIONS_FILE = os.path.join(DIR, "sessions.json")   # 会话列表：桥接独占写
LOOKS_DIR = os.path.join(DIR, "looks")               # 每会话外观：Claude 只写这里
GROUPS_DIR = os.path.join(DIR, "groups")             # 群聊转录：桥接独占写
HISTORY_DIR = os.path.join(DIR, "history")           # 单聊消息档案：桥接独占写
EXPORT_DIR = os.path.join(DIR, "exports")            # 导出的聊天记录(md)落这里
UPLOAD_DIR = os.path.join(DIR, "uploads")
STICKER_DIR = os.path.join(DIR, "stickers")
NO_WINDOW = subprocess.CREATE_NO_WINDOW

IMG_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

# 外观状态：当前头像、聊天背景用的是哪张图。Claude 改这个文件就等于换装。
# model / effort 也存在这里：用户在界面上切，下一条消息生效。
STATE_FILE = os.path.join(DIR, "state.json")
LOOK_KEYS = ("avatar_claude", "avatar_user", "background")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def valid_model(s):
    """别名(fable/opus/opusplan...)、完整型号名(claude-opus-4-6)，
    或带百万上下文后缀的写法(opus[1m])。挡住奇怪字符，
    防止 state.json 里的值变成别的命令行参数。"""
    return bool(isinstance(s, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9.\[\]-]{0,63}$", s))


def look_state_line(skey):
    """给 Claude 看的当前外观描述 + 外观文件写错时的纠错提示。"""
    p = look_path(skey)
    st, warns = normalize_look(_read_json(p) if p else {})

    def nm(key, default):
        sp = sticker_path(st.get(key) or "")
        if not sp:
            return default
        return "「" + os.path.splitext(os.path.basename(sp))[0] + "」"

    line = ("当前外观状态：你是%s头像，用户是%s头像，聊天背景是%s。" % (
        nm("avatar_claude", "默认白色方块"),
        nm("avatar_user", "默认绿色方块"),
        nm("background", "默认素色"),
    ))
    if warns:
        line += "⚠你写的外观文件有问题：" + "；".join(warns) + "。"
    return line


# 被测件（软壳里的Claude）提的折中方案：完整说明只注入一次；
# 外观状态变过的那一轮，追加一行约20个token的状态——它的"回读通道"。
# 绝大多数轮次一个字不加。
LAST_LOOK_SIG = {}


# 每段新对话的第一条消息带上这段。不带的话，新会话里的Claude不知道自己也能发表情包、换头像。
# 这里故意不列出文件夹里有哪些图：判断权交给模型，它用不上就不必花token去翻。
def intro_text(skey):
    return (
        "(这是「Softshell」窗口，按聊天软件的方式显示：支持粗体、代码块、表格和列表，"
        "但气泡里更适合短句分段的聊天式表达，长篇标题层级不好读。\n"
        + STICKER_DIR + " 里有表情包，如果你认为需要发表情包，"
        "可以去翻看那个文件夹，以写 [[表情:xxx]] 来输出表情包给用户，"
        "xxx 是图片的文件名（带不带后缀都认）。表情包请单独占一行发送："
        "那样它会像微信一样显示成独立的一条表情消息，不带文字气泡框；"
        "夹在句子中间写则嵌在文字里。直接打 Unicode emoji 也可以。"
        "如果文件夹里没有你想要的那个表情，你可以用文字写出自己的表情和动作，像RP那样；"
        "也可以直接跟用户说，能不能在表情包文件夹里给你加一个XX表情，"
        "或者请用户往里加头像图、聊天背景图。\n"
        "表情包文件夹里除了表情，可能还混着头像图和聊天背景图。" + look_state_line(skey) +
        "用户要求换头像或聊天背景时：List文件名找图就行，不必读图片内容；"
        "认不出用户说的是哪张，就直接问文件名。"
        "然后把「子文件夹/文件名.后缀」写进 " + (look_path(skey) or STATE_FILE) +
        " 的对应键：avatar_claude / avatar_user / background（JSON对象，没有该文件就新建）。"
        "写完不用做别的，你回复结束时窗口自动刷新；把键置空就恢复默认。)"
    )


# ── 正在运行的 claude 进程登记表：让前端能中断 ──
# key = 前端生成的请求id，value = {"proc": Popen, "stopped": bool}
ACTIVE = {}
ACTIVE_LOCK = threading.Lock()


def kill_tree(proc):
    """杀掉进程及其所有子进程。claude.exe 会派生子进程，
    只 terminate() 父进程会留下孤儿继续烧额度。"""
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            creationflags=NO_WINDOW,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        try:
            proc.terminate()
        except OSError:
            pass


# ── 会话管理：sessions.json 由桥接独占读写，Claude 不碰它 ──
SESS_LOCK = threading.Lock()


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load_sessions():
    d = _read_json(SESSIONS_FILE)
    if not isinstance(d.get("list"), list):
        d = {"active": "", "list": []}
    return d


def save_sessions(d):
    try:
        d["list"].sort(key=lambda s: 0 if s.get("pin") else 1)   # 置顶优先，稳定排序
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def get_session(d, key):
    for s in d["list"]:
        if s.get("key") == key:
            return s
    return None


def new_session_entry(name="新会话"):
    return {
        "key": "s%d" % int(time.time() * 1000),
        "name": name, "sid": "", "model": "", "effort": "",
        "last": "", "ts": int(time.time()),
    }


def look_path(key):
    """这个会话的外观文件。路径会写进 INTRO 告诉 Claude。"""
    if not re.match(r"^s\d{10,16}$", str(key)):
        return None
    return os.path.join(LOOKS_DIR, key + ".json")


def load_look_of(key):
    p = look_path(key)
    return normalize_look(_read_json(p))[0] if p else {}


def sync_legacy_state(key):
    """兼容：老会话里的Claude学的是往 state.json 写外观。
    发现旧地址有新投递，就搬进当前会话的外观文件并清空旧地址。"""
    st = _read_json(STATE_FILE)
    moved = {k: st[k] for k in LOOK_KEYS if st.get(k)}
    if not moved:
        return
    p = look_path(key)
    if not p:
        return
    cur = _read_json(p)
    cur.update(moved)
    try:
        os.makedirs(LOOKS_DIR, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        for k in LOOK_KEYS:
            st.pop(k, None)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def ensure_sessions():
    """启动迁移：把旧的单会话指针和全局外观变成第一个会话。"""
    with SESS_LOCK:
        if os.path.exists(SESSIONS_FILE):
            return
        d = {"active": "", "list": []}
        entry = new_session_entry("默认会话")
        try:
            s = open(SESSION_FILE, encoding="utf-8-sig").read()
            s = s.replace("\ufeff", "").strip()
            if re.match(r"^[0-9a-fA-F-]{8,}$", s):
                entry["sid"] = s
        except OSError:
            pass
        st = _read_json(STATE_FILE)
        look = {k: st[k] for k in LOOK_KEYS if st.get(k)}
        if look:
            p = look_path(entry["key"])
            try:
                os.makedirs(LOOKS_DIR, exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(look, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
        if valid_model(st.get("model") or ""):
            entry["model"] = st["model"]
        if (st.get("effort") or "") in EFFORTS:
            entry["effort"] = st["effort"]
        d["list"].append(entry)
        d["active"] = entry["key"]
        save_sessions(d)


# ── 消息档案：每个会话一份 jsonl，回放/查找/导出都吃这一份 ──
# 单聊存 history/<key>.jsonl；群聊直接复用 groups/<key>.jsonl（转录即档案）。
def history_path(key):
    if not re.match(r"^s\d{10,16}$", str(key)):
        return None
    return os.path.join(HISTORY_DIR, key + ".jsonl")


def append_jsonl(path, rec):
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_jsonl(path):
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def archive_of(sess):
    """这个会话的档案文件路径。"""
    if sess.get("type") == "group":
        return group_log_path(sess["key"])
    return history_path(sess["key"])


def export_md(sess):
    """把会话档案导成 md 文件，返回文件路径；失败返回 None。"""
    msgs = read_jsonl(archive_of(sess))
    name = re.sub(r'[\\/:*?"<>|]', "_", sess.get("name") or "会话").strip() or "会话"
    lines = ["# %s —— Softshell 聊天记录" % (sess.get("name") or "会话"), ""]
    if sess.get("type") == "group":
        lines.append("群成员：用户、" +
                     "、".join(m["name"] for m in sess.get("members", [])))
        lines.append("")
    lines.append("导出时间：" + time.strftime("%Y-%m-%d %H:%M"))
    lines += ["", "---", ""]
    if not msgs:
        lines += ["（这个会话还没有留档的消息。留档从档案功能上线那天开始。）", ""]
    for m in msgs:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"])) if m.get("ts") else ""
        lines.append("**%s**（%s）：" % (m.get("name", "?"), ts))
        lines.append("")
        if m.get("text"):
            lines += [m["text"], ""]
        for x in m.get("stk") or []:
            lines.append("（表情包：stickers/%s）" % x)
        for x in m.get("imgs") or []:
            lines.append("（图片：uploads/%s）" % x)
        if m.get("stk") or m.get("imgs"):
            lines.append("")
        lines += ["---", ""]
    try:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        out = os.path.join(EXPORT_DIR,
                           "%s-%s.md" % (name, time.strftime("%Y%m%d-%H%M%S")))
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return out
    except OSError:
        return None


# ── 群聊：转录存 groups/<key>.jsonl，成员靠"已读指针"拿增量 ──
# 群成员是单聊联系人的"分身"：借名字、头像、模型设置，
# 但群内是独立会话链——群里聊的不进单聊记忆，反之亦然。
def group_log_path(key):
    if not re.match(r"^s\d{10,16}$", str(key)):
        return None
    return os.path.join(GROUPS_DIR, key + ".jsonl")


def append_group_msg(key, who, name, text, imgs=None, stk=None):
    rec = {"who": who, "name": name, "text": text, "ts": int(time.time())}
    if imgs:
        rec["imgs"] = imgs
    if stk:
        rec["stk"] = stk
    append_jsonl(group_log_path(key), rec)


def read_group_msgs(key, start):
    """返回 (从第start条起的消息, 当前总条数)"""
    p = group_log_path(key)
    if not p or not os.path.exists(p):
        return [], 0
    try:
        with open(p, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
    except OSError:
        return [], 0
    msgs = []
    for l in lines[start:]:
        try:
            msgs.append(json.loads(l))
        except ValueError:
            pass
    return msgs, len(lines)


def transcript_text(msgs):
    out = []
    for m in msgs:
        line = "%s: %s" % (m.get("name", "?"), m.get("text", ""))
        if m.get("stk"):
            line += " [表情包:%s]" % ",".join(
                os.path.splitext(os.path.basename(x))[0] for x in m["stk"])
        if m.get("imgs"):
            line += " [附图%d张]" % len(m["imgs"])
        out.append(line)
    return "\n".join(out)


def group_intro(member_name, all_names):
    others = "、".join(n for n in all_names if n != member_name) or "（暂无）"
    return (
        "(这是「Softshell」群聊窗口。你是群成员「" + member_name + "」，"
        "群里还有：用户（人类，群主）、" + others + "（其他Claude实例，和你一样各自独立）。\n"
        "下面的群聊转录里，「用户」是人类的发言，其他名字是别的成员。"
        "请以「" + member_name + "」的身份直接发言，不要在开头带自己的名字，"
        "你说的话会转达给群里所有人。支持粗体、代码块、表格，但更适合聊天式短句。\n"
        + STICKER_DIR + " 里有表情包，需要时可写 [[表情:文件名]] 发送"
        "（单独占一行，会显示成独立的一条表情消息），直接打emoji也行。"
        "群里不改头像背景，换装请让用户去你的单聊窗口找你。)"
    )


def ensure_sticker_dir():
    """确保表情包文件夹存在。用户可能只下载了脚本、没克隆整个仓库，
    缺了这个文件夹，表情面板会是一片空白，连提示都看不到。"""
    d = os.path.join(STICKER_DIR, "共享")
    try:
        os.makedirs(d, exist_ok=True)
        tip = os.path.join(d, "把图片丢这里.txt")
        if not os.path.exists(tip):
            with io.open(tip, "w", encoding="utf-8") as f:
                f.write('把图片（png / jpg / gif / webp）丢进这个文件夹，\n关掉表情面板再点开就能看到了，不用重启。\n\n文件名就是给 AI 看的标签，起得越清楚它挑得越准，例如：\n    开心-举手欢呼.png\n    无奈-扶额.jpg\n\n嫌一张张改名麻烦？可以让 Claude 自己读图重命名。\n')
    except OSError:
        pass


def sticker_groups():
    """扫 stickers 下的每个子文件夹，返回 [{"name": 页签名, "items": [文件名...]}]"""
    groups = []
    try:
        subs = sorted(os.listdir(STICKER_DIR))
    except OSError:
        return groups
    for sub in subs:
        if sub.startswith("."):
            continue
        d = os.path.join(STICKER_DIR, sub)
        if not os.path.isdir(d):
            continue
        items = []
        try:
            for fn in sorted(os.listdir(d)):
                if os.path.splitext(fn)[1].lower() not in IMG_TYPES:
                    continue
                if os.path.isfile(os.path.join(d, fn)):
                    items.append(fn)
        except OSError:
            continue
        # 空文件夹也列出来。新用户点开面板得能看见「往这儿丢图」的提示。
        groups.append({"name": sub, "items": items})
    return groups


def sticker_path(rel):
    """把 共享/开心.png 这样的相对路径解成绝对路径。
    越界、非图片、不存在，一律返回None。"""
    if not isinstance(rel, str) or not rel:
        return None
    rel = rel.replace("\\", "/")
    if rel.startswith("/"):
        return None
    root = os.path.normpath(STICKER_DIR)
    p = os.path.normpath(os.path.join(root, rel))
    try:
        # 挡住 ../ 之类往外跑的路径。这东西要发布出去，这道校验是必须的。
        if os.path.commonpath([root, p]) != root:
            return None
    except ValueError:
        return None
    if os.path.splitext(p)[1].lower() not in IMG_TYPES:
        return None
    if not os.path.isfile(p):
        return None
    return p


# ── 外观文件容错：被测件反馈，键名手滑写错时用户只看到"没反应" ──
LOOK_ALIASES = {
    "avatar_claude": ("avatar_ai", "avatar_bot", "avatar_assistant", "claude_avatar", "avatar"),
    "avatar_user": ("avatar_me", "avatar_human", "user_avatar", "avatar_owner"),
    "background": ("bg", "wallpaper", "background_image", "chat_background", "背景"),
}
_LOOK_CANON = {}
for _k, _als in LOOK_ALIASES.items():
    _LOOK_CANON[_k] = _k
    for _a in _als:
        _LOOK_CANON[_a] = _k


def find_sticker_by_name(name):
    """裸文件名（没带子文件夹前缀）时，全库找同名图"""
    name = os.path.basename(str(name).replace("\\", "/"))
    bare = os.path.splitext(name)[0]
    for grp in sticker_groups():
        for fn in grp["items"]:
            if fn == name or os.path.splitext(fn)[0] == bare:
                return grp["name"] + "/" + fn
    return None


def normalize_look(raw):
    """键名别名归一 + 裸文件名解析。返回 (归一后的dict, 告警列表)。
    告警会顺着外观回读通道带给Claude，它下一轮自己就会改。"""
    out, warns = {}, []
    for k, v in (raw or {}).items():
        ck = _LOOK_CANON.get(str(k).strip().lower())
        if not ck:
            warns.append("认不出键名「%s」，有效键: avatar_claude / avatar_user / background" % k)
            continue
        if v:
            out[ck] = str(v).replace("\\", "/")
    for ck in list(out):
        if not sticker_path(out[ck]):
            hit = find_sticker_by_name(out[ck])
            if hit:
                out[ck] = hit
            else:
                warns.append("%s 指向的图找不到: %s" % (ck, out[ck]))
    return out, warns


# 工具调用的一行摘要：模型自己在这些字段里写了"这一步在干什么"，
# 桌面版显示的就是它。按这个顺序找到哪个用哪个。
TOOL_DETAIL_KEYS = ("description", "query", "pattern", "file_path", "url", "command", "prompt")


def tool_detail(block):
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return ""
    for k in TOOL_DETAIL_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")[:80]
    return ""


def translate(obj):
    """把CLI的stream-json事件翻译成前端认识的简化事件"""
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        sid = obj.get("session_id")
        yield {"kind": "session", "id": sid}
    elif t == "assistant":
        for block in obj.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text" and block.get("text"):
                yield {"kind": "text", "text": block["text"]}
            elif bt == "thinking" and block.get("thinking"):
                yield {"kind": "thinking", "text": block["thinking"]}
            elif bt == "tool_use":
                yield {"kind": "tool", "name": block.get("name", "?"),
                       "detail": tool_detail(block)}
    elif t == "result":
        sid = obj.get("session_id")
        if sid:
            yield {"kind": "session", "id": sid}
        if obj.get("is_error") and obj.get("result"):
            yield {"kind": "error", "text": str(obj.get("result"))[:800]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_bytes(self, data, ctype, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _origin_ok(self):
        """跨站防护：浏览器跨域POST必带别站Origin；
        自家页面是同源Origin，本地脚本无Origin。Host校验挡DNS重绑定。"""
        ok_hosts = ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)
        origin = self.headers.get("Origin", "")
        if origin and origin not in ("http://" + h for h in ok_hosts):
            return False
        host = (self.headers.get("Host") or "").strip()
        if host and host not in ok_hosts:
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(DIR, "index.html"), "rb") as f:
                    self._send_bytes(f.read(), "text/html; charset=utf-8")
            except OSError:
                self.send_error(404)
            return
        if path == "/sessions":
            with SESS_LOCK:
                d = load_sessions()
            out = {"active": d.get("active", ""), "list": []}
            for s in d["list"]:
                lk = load_look_of(s["key"])
                rel = lk.get("avatar_claude") or ""
                item = dict(s)
                item["avatar"] = rel if sticker_path(rel) else ""
                out["list"].append(item)
            self._send_bytes(
                json.dumps(out, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return
        if path == "/look":
            q = parse_qs(urlparse(self.path).query)
            qkey = (q.get("key") or [""])[0]
            with SESS_LOCK:
                d = load_sessions()
                e = get_session(d, qkey) or get_session(d, d.get("active"))
            st = load_look_of(e["key"]) if e else {}
            out = {"key": e["key"] if e else ""}
            for k in LOOK_KEYS:
                rel = st.get(k) or ""
                out[k] = rel if sticker_path(rel) else ""
            mdl = (e.get("model") if e else "") or ""
            out["model"] = mdl if valid_model(mdl) else ""
            eff = (e.get("effort") if e else "") or ""
            out["effort"] = eff if eff in EFFORTS else ""
            self._send_bytes(
                json.dumps(out, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return
        if path == "/stickers":
            self._send_bytes(
                json.dumps(sticker_groups(), ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return
        if path == "/sticker":
            q = parse_qs(urlparse(self.path).query)
            p = sticker_path((q.get("f") or [""])[0])
            if not p:
                self.send_error(404)
                return
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(404)
                return
            # no-cache：同名文件被换掉以后，面板要能立刻显示新的那张
            self._send_bytes(data, IMG_TYPES[os.path.splitext(p)[1].lower()],
                             {"Cache-Control": "no-cache"})
            return
        if path == "/history":
            q = parse_qs(urlparse(self.path).query)
            qkey = (q.get("key") or [""])[0]
            with SESS_LOCK:
                d = load_sessions()
                e = get_session(d, qkey)
            if not e:
                self.send_error(404)
                return
            out = {"list": read_jsonl(archive_of(e)),
                   "type": e.get("type", "chat")}
            if e.get("type") == "group":
                av = {}
                for m in e.get("members", []):
                    rel = load_look_of(m.get("srckey") or "").get("avatar_claude") or ""
                    av[m["name"]] = rel if sticker_path(rel) else ""
                out["avatars"] = av
            self._send_bytes(
                json.dumps(out, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return
        if path == "/uploadfile":
            q = parse_qs(urlparse(self.path).query)
            fn = (q.get("f") or [""])[0]
            # 只认桥接自己起的名，挡住任意文件读取
            if not re.match(r"^img-\d{10,16}\.(png|jpg|jpeg|gif|webp)$", fn):
                self.send_error(404)
                return
            fp = os.path.join(UPLOAD_DIR, fn)
            try:
                with open(fp, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(404)
                return
            self._send_bytes(data, IMG_TYPES[os.path.splitext(fn)[1].lower()],
                             {"Cache-Control": "no-cache"})
            return
        self.send_error(404)

    def do_POST(self):
        if not self._origin_ok():
            # 恶意网页可能借本地端口驱动免许可的Claude，跨站请求一律拒
            self.send_error(403)
            return
        if self.path == "/stop":
            n = int(self.headers.get("Content-Length", 0))
            rid = ""
            try:
                rid = str(json.loads(self.rfile.read(n).decode("utf-8")).get("rid", ""))
            except (ValueError, UnicodeDecodeError):
                pass
            killed = 0
            with ACTIVE_LOCK:
                if rid:
                    # rid没命中说明那轮已经结束了：什么都不杀，
                    # 绝不能回退成"杀全部"——别的窗口可能正在生成
                    targets = [rid] if rid in ACTIVE else []
                else:
                    targets = list(ACTIVE.keys())
                for k in targets:
                    slot = ACTIVE.get(k)
                    if slot:
                        slot["stopped"] = True
                        kill_tree(slot["proc"])
                        killed += 1
            self._send_bytes(
                json.dumps({"killed": killed}).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        if self.path == "/group/new":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            gname = str(p.get("name", "")).strip()[:50] or "群聊"
            srckeys = [str(k) for k in (p.get("srckeys") or [])]
            with SESS_LOCK:
                d = load_sessions()
                members = []
                for sk in srckeys:
                    e = get_session(d, sk)
                    if not e or e.get("type") == "group":
                        continue
                    if any(m.get("srckey") == sk for m in members):
                        continue   # 同一联系人勾两次只算一个，防分身串号
                    nm = (e.get("name") or "成员").strip()
                    i = 2   # 重名成员会让@路由分不清人，加编号区分
                    while nm in [m["name"] for m in members]:
                        nm = (e.get("name") or "成员") + str(i)
                        i += 1
                    members.append({
                        "name": nm, "srckey": sk, "sid": "",
                        "model": e.get("model", ""), "effort": e.get("effort", ""),
                        "read": 0,
                    })
                if len(members) < 2:
                    self.send_error(400)
                    return
                g = new_session_entry(gname)
                g["type"] = "group"
                g["members"] = members
                d["list"].insert(0, g)
                d["active"] = g["key"]
                save_sessions(d)
            self._send_bytes(json.dumps(g, ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8")
            return
        if self.path == "/session/new":
            with SESS_LOCK:
                d = load_sessions()
                e = new_session_entry()
                d["list"].insert(0, e)
                d["active"] = e["key"]
                save_sessions(d)
            self._send_bytes(json.dumps(e, ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8")
            return
        if self.path in ("/session/switch", "/session/rename",
                         "/session/delete", "/session/pin"):
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            key = str(p.get("key", ""))
            with SESS_LOCK:
                d = load_sessions()
                e = get_session(d, key)
                if not e:
                    self.send_error(404)
                    return
                if self.path == "/session/switch":
                    d["active"] = key
                elif self.path == "/session/rename":
                    name = str(p.get("name", "")).strip()[:50]
                    if name:
                        e["name"] = name
                elif self.path == "/session/pin":
                    e["pin"] = bool(p.get("pin"))
                else:  # delete
                    d["list"] = [s for s in d["list"] if s["key"] != key]
                    for pth in (look_path(key), group_log_path(key), history_path(key)):
                        if pth and os.path.exists(pth):
                            try:
                                os.remove(pth)
                            except OSError:
                                pass
                    if d["active"] == key:
                        if not d["list"]:
                            d["list"].append(new_session_entry())
                        d["active"] = d["list"][0]["key"]
                save_sessions(d)
            self._send_bytes(b"ok", "text/plain")
            return
        if self.path == "/export":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            with SESS_LOCK:
                d = load_sessions()
                e = get_session(d, str(p.get("key", "")))
            if not e:
                self.send_error(404)
                return
            out = export_md(e)
            if not out:
                self.send_error(500)
                return
            self._send_bytes(
                json.dumps({"path": out}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8")
            return
        if self.path == "/prefs":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            with SESS_LOCK:
                d = load_sessions()
                e = get_session(d, str(p.get("key", ""))) or get_session(d, d.get("active"))
                if not e:
                    self.send_error(404)
                    return
                if "model" in p:
                    m = str(p.get("model") or "")
                    if m and not valid_model(m):
                        self.send_error(400)
                        return
                    e["model"] = m
                if "effort" in p:
                    ef = str(p.get("effort") or "")
                    if ef and ef not in EFFORTS:
                        self.send_error(400)
                        return
                    e["effort"] = ef
                save_sessions(d)
            self._send_bytes(b"ok", "text/plain")
            return
        if self.path.startswith("/upload"):
            self._handle_upload()
            return
        if self.path != "/send":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_error(400)
            return
        rid = str(payload.get("rid", "")) or ("r%d" % int(time.time() * 1000))
        text = str(payload.get("text", "")).strip()
        images = []
        for p in (payload.get("images") or []):
            # 只认 uploads 里桥接自己存的文件；外部塞进来的任意路径不理，
            # 防止跨站请求诱导Claude去读盘上别的东西
            if not isinstance(p, str):
                continue
            ap = os.path.join(UPLOAD_DIR, os.path.basename(p))
            if os.path.isfile(ap):
                images.append(ap)
        up_names = [os.path.basename(p) for p in images]   # 档案里只记文件名，走/uploadfile取
        stk_rels = []
        for rel in (payload.get("stickers") or []):
            p = sticker_path(rel)
            if p:
                images.append(p)
                stk_rels.append(str(rel).replace("\\", "/"))
        n_stickers = len(stk_rels)
        if not text and not images:
            self.send_error(400)
            return

        skey = str(payload.get("skey", ""))
        with SESS_LOCK:
            d = load_sessions()
            sess = get_session(d, skey) or get_session(d, d.get("active"))
            if not sess:
                sess = new_session_entry()
                d["list"].insert(0, sess)
                d["active"] = sess["key"]
                save_sessions(d)
            skey = sess["key"]
        if sess.get("type") == "group":
            self._handle_group(sess, text, images, up_names, stk_rels, rid)
            return
        raw_text = text
        sync_legacy_state(skey)   # 老会话的Claude还在往旧地址(state.json)投外观，搬过来
        urec = {"who": "user", "name": "用户", "text": raw_text, "ts": int(time.time())}
        if up_names:
            urec["imgs"] = up_names
        if stk_rels:
            urec["stk"] = stk_rels
        append_jsonl(history_path(skey), urec)
        sid = sess.get("sid") or None
        if images:
            what = "表情包" if n_stickers == len(images) else "图片"
            text = (text or ("我发了一个" + what + "。")) + \
                "\n\n(我发了" + what + "，请先用Read工具查看这些文件再回答: " + " ; ".join(images) + ")"
        cur_sig = look_state_line(skey)
        if not sid:
            text += "\n\n" + intro_text(skey)
        elif cur_sig != LAST_LOOK_SIG.get(skey):
            # 外观变了（多半是Claude自己上一轮改的）：给它一行回读确认。
            # 桥接刚重启时也会注入一次，保证停机期间的变化不被错过。
            text += "\n\n(" + cur_sig + ")"
        LAST_LOOK_SIG[skey] = cur_sig

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        if not CLAUDE:
            self._emit({"kind": "error", "text":
                        "没找到 claude 命令。请先安装 Claude Code CLI："
                        "在 PowerShell 里运行  irm https://claude.ai/install.ps1 | iex"})
            self._emit({"kind": "done"})
            return

        base = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions"]
        mdl = sess.get("model") or ""
        if valid_model(mdl):
            base += ["--model", mdl]
        eff = sess.get("effort") or ""
        if eff in EFFORTS:
            base += ["--effort", eff]
        attempt = 0
        while True:
            attempt += 1
            cmd = list(base)
            if sid:
                cmd += ["--resume", sid]
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=HOME,
                    encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                )
            except OSError as ex:
                self._emit({"kind": "error", "text": "启动claude失败：" + str(ex)[:300]})
                self._emit({"kind": "done"})
                return

            # prompt 从 stdin 喂：不占命令行长度，超长消息也不崩。
            # 用单独线程写，避免子进程还没开始读时把管道写满卡死
            def _feed(p=proc, s=text):
                try:
                    p.stdin.write(s)
                    p.stdin.close()
                except OSError:
                    pass
            threading.Thread(target=_feed, daemon=True).start()
            with ACTIVE_LOCK:
                ACTIVE[rid] = {"proc": proc, "stopped": False}
            err_buf = []
            self.emit_lock = threading.Lock()
            drainer = threading.Thread(
                target=self._drain_stderr, args=(proc, err_buf), daemon=True
            )
            drainer.start()
            emitted = False
            new_sid = None
            last_text = ""
            err_ev = None
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    for ev in translate(obj):
                        if ev.get("kind") == "session" and ev.get("id"):
                            new_sid = ev["id"]
                        elif ev.get("kind") == "text":
                            last_text = ev["text"]
                            append_jsonl(history_path(skey),
                                         {"who": "claude", "name": "Claude",
                                          "text": ev["text"], "ts": int(time.time())})
                        if ev.get("kind") == "error":
                            err_ev = ev   # 攒着：可能要自动重开重试，别过早吓用户
                            continue
                        self._emit(ev)
                        emitted = True
                proc.wait()
                drainer.join(timeout=2)
            except (ConnectionError, OSError):
                # 用户关了窗口：停掉底层进程树，别浪费额度
                kill_tree(proc)
                with ACTIVE_LOCK:
                    ACTIVE.pop(rid, None)
                return
            with ACTIVE_LOCK:
                was_stopped = ACTIVE.get(rid, {}).get("stopped", False)
                ACTIVE.pop(rid, None)
            if was_stopped:
                # 用户按了停止：不要当成会话失效去重试，否则中断白按
                self._finish_session(skey, new_sid, raw_text, last_text)
                self._emit({"kind": "note", "text": "已停止"})
                self._emit({"kind": "done"})
                return
            if proc.returncode != 0 and not last_text:
                if sid and attempt == 1:
                    # 旧会话接不上（被清理/ID损坏）：自动开新对话重试一次。
                    # 新链的第一条消息要补开场白，不然它不认识表情包和换装协议
                    sid = None
                    text += "\n\n" + intro_text(skey)
                    self._emit({"kind": "note", "text": "旧会话接不上了，已自动开新对话"})
                    continue
                err = ("".join(err_buf)).strip()[-800:]
                self._emit(err_ev or {"kind": "error",
                                      "text": err or ("claude退出码 %s" % proc.returncode)})
            elif err_ev:
                self._emit(err_ev)
            elif proc.returncode != 0:
                # 有正文但进程非正常退出（比如被外部杀掉）：提示别装没事
                self._emit({"kind": "note", "text": "（进程中途退出，回复可能没写完）"})
            self._finish_session(skey, new_sid, raw_text, last_text)
            self._emit({"kind": "done"})
            return

    def _run_member(self, member, prompt, rid):
        """跑一个群成员的一轮发言。返回 {text, sid, stopped, rc, err}"""
        cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
               "--dangerously-skip-permissions"]
        if valid_model(member.get("model") or ""):
            cmd += ["--model", member["model"]]
        if (member.get("effort") or "") in EFFORTS:
            cmd += ["--effort", member["effort"]]
        if member.get("sid"):
            cmd += ["--resume", member["sid"]]
        rel = load_look_of(member.get("srckey") or "").get("avatar_claude") or ""
        minfo = {"name": member["name"], "avatar": rel if sticker_path(rel) else ""}
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=HOME,
                encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
            )
        except OSError as ex:
            return {"text": "", "sid": None, "stopped": False, "rc": -1,
                    "err": "启动claude失败：" + str(ex)[:300]}

        def _feed():
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except OSError:
                pass
        threading.Thread(target=_feed, daemon=True).start()
        with ACTIVE_LOCK:
            ACTIVE[rid] = {"proc": proc, "stopped": False}
        err_buf = []
        drainer = threading.Thread(target=self._drain_stderr, args=(proc, err_buf), daemon=True)
        drainer.start()
        new_sid, texts = None, []
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                for ev in translate(obj):
                    if ev.get("kind") == "session" and ev.get("id"):
                        new_sid = ev["id"]
                        continue
                    ev["member"] = minfo
                    if ev.get("kind") == "text":
                        texts.append(ev["text"])
                    self._emit(ev)
            proc.wait()
            drainer.join(timeout=2)
        except (ConnectionError, OSError):
            # 用户关了窗口：停掉底层进程树，别浪费额度
            kill_tree(proc)
            with ACTIVE_LOCK:
                ACTIVE.pop(rid, None)
            raise
        with ACTIVE_LOCK:
            stopped = ACTIVE.get(rid, {}).get("stopped", False)
            ACTIVE.pop(rid, None)
        return {"text": "\n".join(texts).strip(), "sid": new_sid,
                "stopped": stopped, "rc": proc.returncode,
                "err": ("".join(err_buf)).strip()[-800:]}

    def _member_prompt(self, gkey, member, all_names, images, fresh):
        """拼一个成员这轮看到的内容：新成员给开场白，老成员给转录增量。
        fresh=True 表示会话链刚重开，把全量转录补给它。"""
        start = 0 if fresh else member.get("read", 0)
        msgs, _ = read_group_msgs(gkey, start)
        parts = []
        if fresh:
            parts.append(group_intro(member["name"], all_names))
            if msgs:
                parts.append("[群聊转录]\n" + transcript_text(msgs))
        elif msgs:
            parts.append("[群聊转录·你上次发言之后的新消息]\n" + transcript_text(msgs))
        if images:
            parts.append("(用户发的图片，请先用Read工具查看: " + " ; ".join(images) + ")")
        parts.append("请以「" + member["name"] + "」的身份就上面的对话发言。")
        return "\n\n".join(parts)

    def _handle_group(self, sess, text, images, up_names, stk_rels, rid):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.emit_lock = threading.Lock()
        if not CLAUDE:
            self._emit({"kind": "error", "text":
                        "没找到 claude 命令。请先安装 Claude Code CLI："
                        "在 PowerShell 里运行  irm https://claude.ai/install.ps1 | iex"})
            self._emit({"kind": "done"})
            return
        gkey = sess["key"]
        members = sess.get("members") or []
        all_names = [m["name"] for m in members]
        append_group_msg(gkey, "user", "用户", text, imgs=up_names, stk=stk_rels)
        # @路由：长名字先匹配防前缀重叠；按@出现的位置排队——@谁在前谁先说
        hits = []
        tmp = text
        for m in sorted(members, key=lambda x: -len(x["name"])):
            tag = "@" + m["name"]
            i = tmp.find(tag)
            if i >= 0:
                hits.append((i, m))
                tmp = tmp.replace(tag, "\x00" * len(tag))   # 占位不移位，位置才数得准
        targets = [m for _, m in sorted(hits, key=lambda x: x[0])]
        if not targets:
            self._emit({"kind": "note",
                        "text": "没有@任何成员：话已记进群，他们下次发言时会看到"})
            self._touch_group(gkey, text)
            self._emit({"kind": "done"})
            return
        last_reply = ""
        try:
            for m in targets:
                self._emit({"kind": "status", "text": "「" + m["name"] + "」正在回复"})
                fresh = not m.get("sid")
                r = self._run_member(
                    m, self._member_prompt(gkey, m, all_names, images, fresh), rid)
                if r["rc"] != 0 and not r["text"] and not r["stopped"] and not fresh:
                    # 旧会话链接不上：重开一条，把全量转录补给它，重试一次
                    m["sid"] = ""
                    self._emit({"kind": "note",
                                "text": "「" + m["name"] + "」的会话接不上了，已重开并补发群记录"})
                    r = self._run_member(
                        m, self._member_prompt(gkey, m, all_names, images, True), rid)
                if r["rc"] != 0 and not r["text"] and not r["stopped"]:
                    self._emit({"kind": "error", "text":
                                "「" + m["name"] + "」响应失败：" +
                                (r["err"] or ("claude退出码 %s" % r["rc"]))})
                # 落账：按名字匹配（群内唯一）。这轮失败就不推进已读指针，
                # 没送达的消息下一轮还能补给他，群内认知不错位
                ok = bool(r["text"]) or (r["rc"] == 0 and not r["stopped"])
                with SESS_LOCK:
                    d = load_sessions()
                    g = get_session(d, gkey)
                    if g:
                        for mm in g.get("members", []):
                            if mm.get("name") == m["name"]:
                                if r["sid"]:
                                    mm["sid"] = r["sid"]
                                if r["text"]:
                                    append_group_msg(gkey, "member", m["name"], r["text"])
                                if ok:
                                    _, tot = read_group_msgs(gkey, 0)
                                    mm["read"] = tot
                                break
                        save_sessions(d)
                if r["text"]:
                    last_reply = r["text"]
                if r["stopped"]:
                    if len(targets) > 1:
                        self._emit({"kind": "note", "text": "已停止，后面的成员不再发言"})
                    else:
                        self._emit({"kind": "note", "text": "已停止"})
                    break
        except (ConnectionError, OSError):
            return
        self._touch_group(gkey, last_reply or text)
        self._emit({"kind": "done"})

    def _touch_group(self, gkey, summary):
        with SESS_LOCK:
            d = load_sessions()
            g = get_session(d, gkey)
            if not g:
                return
            g["ts"] = int(time.time())
            summ = (summary or "").strip().replace("\n", " ")
            if summ:
                g["last"] = summ[:24]
            save_sessions(d)

    def _finish_session(self, skey, new_sid, user_text, bot_text):
        """一轮结束：存下会话ID、最后一条摘要，新会话用首条消息起名。"""
        with SESS_LOCK:
            d = load_sessions()
            e = get_session(d, skey)
            if not e:
                return
            if new_sid:
                e["sid"] = new_sid
            summ = (bot_text or user_text or "").strip().replace("\n", " ")
            if summ:
                e["last"] = summ[:24]
            e["ts"] = int(time.time())
            if e.get("name") == "新会话" and user_text.strip():
                e["name"] = user_text.strip().replace("\n", " ")[:12]
            save_sessions(d)

    def _handle_upload(self):
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0 or n > 30 * 1024 * 1024:
            self.send_error(400)
            return
        data = self.rfile.read(n)
        q = parse_qs(urlparse(self.path).query)
        ext = (q.get("ext", ["png"])[0]).lower()
        if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
            ext = "png"
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            name = "img-%d.%s" % (int(time.time() * 1000), ext)
            path = os.path.join(UPLOAD_DIR, name)
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            self.send_error(500)
            return
        self._send_bytes(
            json.dumps({"path": path}, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    # stderr 里出现这些字样，多半是网络在抖、CLI在自动重试
    RETRY_PAT = re.compile(r"retry|attempt \d|overloaded|rate.?limit|econn|timed?.?out", re.I)

    def _drain_stderr(self, proc, err_buf):
        """逐行读stderr：既攒着当错误报告，也顺手把重试迹象报给前端。"""
        try:
            for line in proc.stderr:
                err_buf.append(line)
                if self.RETRY_PAT.search(line):
                    try:
                        self._emit({"kind": "status", "text": "网络波动，自动重试中"})
                    except (OSError, ConnectionError):
                        return
        except (OSError, ValueError):
            pass

    def _emit(self, obj):
        # 主循环和stderr线程都会写响应流，得排队
        lock = getattr(self, "emit_lock", None)
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        if lock:
            with lock:
                self.wfile.write(data)
                self.wfile.flush()
        else:
            self.wfile.write(data)
            self.wfile.flush()


class SingleInstanceServer(ThreadingHTTPServer):
    # Windows 默认允许多进程绑同一端口，会导致每次双击都起一个新服务。
    # 关掉端口复用，第二次绑定才会失败，防重复启动的逻辑才生效。
    allow_reuse_address = False


def open_window():
    url = "http://127.0.0.1:%d/" % PORT
    if BROWSER:
        subprocess.Popen(
            [BROWSER, "--app=" + url, "--window-size=900,860"],
            creationflags=NO_WINDOW,
        )
    else:
        # 没找到 Edge/Chrome：用系统默认浏览器，会带地址栏但功能一样
        webbrowser.open(url)


def main():
    ensure_sticker_dir()
    ensure_sessions()
    try:
        srv = SingleInstanceServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # 端口被占 = 桥接已在跑，直接开窗口
        open_window()
        return
    threading.Timer(0.6, open_window).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
