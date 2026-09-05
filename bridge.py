# -*- coding: utf-8 -*-
"""Softshell —— 本地桥接
把 Claude Code CLI 接到聊天软件风格网页界面。支持发图片、发表情包。
Windows：双击 bridge.py（或桌面快捷方式）启动；Mac：双击 Softshell.command 或终端 python3 bridge.py。
再次启动只会重新打开窗口，不会重复起服务。
想彻底退出后台：Windows 在任务管理器结束 pythonw.exe；Mac 在活动监视器结束 Python，或终端 pkill -f bridge.py。

表情包：往 stickers 下的任意子文件夹丢图片就行，子文件夹名就是面板上的页签名。
        不用重启，聊天窗口每次点开表情面板都会重新扫一遍。
"""
import glob
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import webbrowser
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
OS_LABEL = "Windows" if IS_WIN else ("macOS" if IS_MAC else "Linux")
KEY_MOD = "⌘" if IS_MAC else "Ctrl"          # 提示词里说快捷键用
INSTALL_HINT = ("在 PowerShell 里运行  irm https://claude.ai/install.ps1 | iex" if IS_WIN
                else "在终端里运行  curl -fsSL https://claude.ai/install.sh | bash")


def find_claude():
    """找 claude CLI。先看 PATH，再看官方安装器 / npm / Homebrew 的默认位置。
    Mac 上双击启动时 PATH 往往只有系统目录，所以后面这些兜底很重要。"""
    hit = shutil.which("claude")
    if hit:
        return hit
    if IS_WIN:
        cands = [
            os.path.join(HOME, ".local", "bin", "claude.exe"),
            os.path.join(HOME, "AppData", "Local", "Programs", "claude", "claude.exe"),
            os.path.join(HOME, ".claude", "local", "claude.exe"),
        ]
    else:
        cands = [
            os.path.join(HOME, ".local", "bin", "claude"),
            os.path.join(HOME, ".claude", "local", "claude"),
            os.path.join(HOME, ".claude", "local", "bin", "claude"),
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
            os.path.join(HOME, ".npm-global", "bin", "claude"),
            os.path.join(HOME, ".volta", "bin", "claude"),
            os.path.join(HOME, ".bun", "bin", "claude"),
        ]
        cands += sorted(glob.glob(os.path.join(HOME, ".nvm", "versions", "node", "*", "bin", "claude")),
                        reverse=True)
        cands += sorted(glob.glob(os.path.join(HOME, ".fnm", "node-versions", "*", "installation", "bin", "claude")),
                        reverse=True)
    for p in cands:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def find_browser():
    """找一个能开无地址栏应用窗口的浏览器：Edge 优先，其次 Chrome。
    都没有就返回 None，改用系统默认浏览器打开（会带地址栏，但能用）。"""
    if not IS_WIN:
        if IS_MAC:
            apps = [
                ("Microsoft Edge.app", "Microsoft Edge"),
                ("Google Chrome.app", "Google Chrome"),
                ("Chromium.app", "Chromium"),
                ("Brave Browser.app", "Brave Browser"),
            ]
            for base in ("/Applications", os.path.join(HOME, "Applications")):
                for app, exe in apps:
                    p = os.path.join(base, app, "Contents", "MacOS", exe)
                    if os.path.isfile(p):
                        return p
        for name in ("microsoft-edge", "google-chrome", "google-chrome-stable",
                     "chromium", "chromium-browser", "brave-browser"):
            hit = shutil.which(name)
            if hit:
                return hit
        return None
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


def claude_bin():
    """启动时没找到 claude 的话，每次用到时再找一遍：用户常常先开软壳后装 CLI。"""
    global CLAUDE
    if not CLAUDE:
        CLAUDE = find_claude()
    return CLAUDE
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
# 子进程启动参数：Windows 不弹黑窗；POSIX 起独立进程组，中断时能整树杀掉
SPAWN = ({"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN
         else {"start_new_session": True})

IMG_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

# 外观状态：当前头像、聊天背景用的是哪张图。Claude 改这个文件就等于换装。
# model / effort 也存在这里：用户在界面上切，下一条消息生效。
STATE_FILE = os.path.join(DIR, "state.json")
LOOK_KEYS = ("avatar_claude", "avatar_user", "background")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# 型号 → 界面显示名（与 index.html 的 MODEL_NAMES 同一份内容；建群时成员初始昵称用它）
MODEL_LABELS = {
    "fable": "Fable 5.1", "opus": "Opus 5", "sonnet": "Sonnet 5", "haiku": "Haiku 4.5",
    "claude-fable-5": "Fable 5", "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7", "claude-opus-4-6": "Opus 4.6",
    "claude-sonnet-4-6": "Sonnet 4.6", "opusplan": "OpusPlan",
}
# 群里不许当昵称的词：@路由的保留字、系统条署名
RESERVED_NAMES = ("用户", "所有人", "全体成员", "全员", "系统")


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

# 朗读嗓音：只存在浏览器里，前端每条消息随身带 voice 字段；
# 和外观一样，换过的那一轮追加一行回读，其余轮次不加。
_MAC_ZH = None   # [(完整嗓音名, 语种, 中文名)]，扫一次


def _mac_scan():
    """扫 `say -v ?` 里的中文嗓音。中文名从示例句「你好！我叫瀚。」里取。"""
    global _MAC_ZH
    if _MAC_ZH is not None:
        return _MAC_ZH
    _MAC_ZH = []
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return _MAC_ZH
    for ln in out.splitlines():
        m = re.match(r"^(.*?)\s{2,}((?:zh|yue)_[A-Z]{2})\s+#\s*(.*)$", ln)
        if not m:
            continue
        name, lang, sample = m.group(1).strip(), m.group(2), m.group(3)
        cn = re.search(r"我叫\s*(.+?)\s*[。.!！]", sample)
        head = name.split("(")[0].strip()
        label = cn.group(1) if cn else head
        if label.lower() == head.lower():      # 没有中文名的（Eddy/Flo…）就用英文
            label = head
        _MAC_ZH.append((name, lang, label))
    return _MAC_ZH


def _mac_voices():
    """挑默认的女声/男声：优先系统设置里下载的高级/增强嗓音（黎潋、瀚），
    其次婷婷/Eddy。返回 {"female": 完整嗓音名, "male": 完整嗓音名}。"""
    zh = _mac_scan()

    def pick(wants):
        for w in wants:
            for name, _lang, _cn in zh:
                if name.startswith(w):
                    return name
        for name, lang, _cn in zh:
            if lang == "zh_CN":
                return name
        return zh[0][0] if zh else ""

    return {"female": pick(["Lilian", "Tingting (Premium)", "Tingting (Enhanced)",
                            "Lili", "Tingting", "Meijia", "Sinji"]),
            "male": pick(["Han", "Binbin", "Eddy", "Reed", "Rocko", "Grandpa"])}


def _voice_label(full):
    """嗓音展示名：'Lilian (Premium)'→黎潋，'Eddy (中文（中国大陆）)'→Eddy。"""
    if not full:
        return "无"
    for name, _lang, cn in _mac_scan():
        if name == full:
            return cn
    return full.split("(")[0].strip()


MAC_VOICES = _mac_voices() if IS_MAC else {"female": "", "male": ""}
LOCAL_TTS_LABEL = OS_LABEL + " 本机"
if IS_MAC:
    VOICE_NAMES = {"local-male": "男声「" + _voice_label(MAC_VOICES["male"]) + "」",
                   "local-female": "女声「" + _voice_label(MAC_VOICES["female"]) + "」"}
else:
    VOICE_NAMES = {"local-male": "男声「康康」", "local-female": "女声「慧慧」"}
LAST_VOICE = {}


def voice_id(v):
    return "local-male" if v == "local-male" else "local-female"


def voice_name(vid):
    return VOICE_NAMES[voice_id(vid)]


def voice_state_line(vid):
    return "当前朗读嗓音：" + LOCAL_TTS_LABEL + voice_name(vid) + "。"


LAST_EFFORT = {}


def effort_change_text(skey, eff):
    """effort档位和上次告诉过它的不同 → 一行回读；首次只记录不提示。"""
    cur = eff if eff in EFFORTS else "default"
    prev = LAST_EFFORT.get(skey)
    LAST_EFFORT[skey] = cur
    if prev is None or prev == cur:
        return ""
    label = "默认档" if cur == "default" else cur + " 档"
    return ("(用户把你的思考力度 effort 调成了" + label +
            "。这只是资源档位：低档少想快答，高档多想深答，不影响你的性格和记忆。)")


def voice_change_text(skey, vid):
    """嗓音和上次告诉过它的不同 → 一行回读；相同或首次（开场白已带）→ 空串。"""
    vid = voice_id(vid)
    prev = LAST_VOICE.get(skey)
    LAST_VOICE[skey] = vid
    if prev is None or prev == vid:
        return ""
    return "(用户把你的朗读嗓音换成了 " + LOCAL_TTS_LABEL + voice_name(vid) + "。)"


# 每段新对话的第一条消息带上这段。不带的话，新会话里的Claude不知道自己能发表情包、
# 换头像，也答不上用户关于软壳界面的提问。语气平实：介绍功能，不诱导动作。
def intro_text(skey, vid="local-female"):
    LAST_VOICE[skey] = voice_id(vid)
    return (
        "(这是「Softshell 软壳」聊天窗口，你的输出按聊天软件方式显示：支持粗体、"
        "代码块、表格和列表，但更适合短句分段的聊天式表达。\n"
        "【界面功能】用户问起时你要答得上来：左侧栏可开多个会话、可拉群聊"
        "（选型号和数量建群，群里@成员名字点名发言）；右键会话名可以改名、置顶、导出聊天记录"
        "（md文件，存到软壳目录的exports文件夹）、删除单个会话，以及一键清空所有会话"
        "（连聊天档案一起删，只留一个新空会话）；标题栏🔍搜索本会话"
        "聊天记录（全文/图片与表情包/链接/文件路径四类）；运行中按Esc或■随时打断；"
        "发图用📎按钮或" + KEY_MOD + "+V粘贴；状态栏右侧的模型/effort按钮可切换（下一条消息生效），"
        "状态栏左侧显示的是每轮token消耗（只是数字，不可点）；"
        "聊天记录自动留档在本地，重开窗口自动回放。\n"
        "【语音对讲】标题栏📞是对讲机模式（可选件，装好识别模型才能用）：用户点一下开麦"
        "直接说话，说完停顿自动发送并闭麦；你的回复会被逐句念出来，念完自动再开麦；"
        "你念的时候用户一开口你就会被打断；再点📞关掉。对讲时没有单独页面，"
        "双方的话都以普通气泡出现在聊天窗里。语音识别（SenseVoice）和朗读都在用户本机"
        "离线完成。朗读嗓音只有两个 " + LOCAL_TTS_LABEL + "嗓音：" + VOICE_NAMES["local-female"] +
        "和" + VOICE_NAMES["local-male"] + "，"
        "用户在状态栏「嗓音」里选（点击即试听），全局生效；"
        "嗓音换了会告诉你。" + voice_state_line(vid) + "\n"
        "【表情包】" + STICKER_DIR + " 文件夹里放着表情包，用到时再翻看即可。"
        "发送方式：写 [[表情:文件名]]（带不带后缀都认），单独占一行会显示成"
        "微信式的独立表情消息，夹在句中则嵌在文字里；只写你翻看后确认存在的文件名，"
        "猜错会穿帮成文字；直接打emoji也行，或像RP那样用文字写表情动作；"
        "缺什么表情可以请用户往文件夹里加图。\n"
        "【换装】表情包文件夹里可能混着头像图和聊天背景图。" + look_state_line(skey) +
        "用户要求换头像或聊天背景时：List文件名找图（不必读图片内容），"
        "把「子文件夹/文件名.后缀」写进 " + (look_path(skey) or STATE_FILE) +
        " 的对应键：avatar_claude / avatar_user / background（JSON对象，"
        "没有该文件就新建）；写完不用做别的，你回复结束时窗口自动刷新，键置空恢复默认。"
        "用户想用自己的新图片换装时，请用户把图存到桌面并告诉你文件名，"
        "你自己把它复制进表情包文件夹的子文件夹（如 共享），再照上面的办法写外观文件。)"
    )


# ── 正在运行的 claude 进程登记表：让前端能中断 ──
# key = 前端生成的请求id，value = {"proc": Popen, "stopped": bool}
def decode_body(raw):
    """请求体解码：UTF-8优先；Windows上有些客户端（命令行内联中文等）
    实际送来的是GBK字节，退一步试GBK，别把人家400回去。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk")


ACTIVE = {}
ACTIVE_LOCK = threading.Lock()

# 正在发送中的会话：同一会话并发发送会让CLI的会话链分叉，
# 后完成的一轮覆盖档案，先前那轮从模型记忆里静默蒸发——必须一轮一轮来
SENDING = set()
SENDING_LOCK = threading.Lock()


def kill_tree(proc):
    """杀掉进程及其所有子进程。claude 会派生子进程，
    只 terminate() 父进程会留下孤儿继续烧额度。
    Windows 用 taskkill /T；POSIX 上子进程都起在独立进程组里（SPAWN），整组 SIGKILL。"""
    if IS_WIN:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                **SPAWN,
                capture_output=True,
                timeout=10,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
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
        d["list"].sort(key=lambda s: (0 if s.get("pin") else 1, -(s.get("ts") or 0)))
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


def _export_safe(text):
    """消息正文里手打的分隔线/伪发言人抬头会与导出结构混淆，转义掉"""
    out = []
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if (s in ("---", "***", "___") or re.match(r"^={3,}$", s) or
                re.match(r"^\*\*.+\*\*（.+）：$", s)):
            ln = "\\" + ln
        out.append(ln)
    return "\n".join(out)


def export_md(sess):
    """把会话档案导成 md 文件，返回文件路径；失败返回 None。"""
    msgs = read_jsonl(archive_of(sess))
    name = re.sub(r'[\\/:*?"<>|]', "_", sess.get("name") or "会话").strip() or "会话"
    lines = ["# %s —— Softshell 聊天记录" % (sess.get("name") or "会话"), ""]
    if sess.get("type") == "group":
        lines.append("群成员：" + group_user_name(sess) + "、" +
                     "、".join(m["name"] for m in sess.get("members", [])))
        lines.append("群主：" + group_owner_name(sess))
        if (sess.get("notice") or "").strip():
            lines.append("群公告：" + sess["notice"].strip())
        lines.append("")
    lines.append("导出时间：" + time.strftime("%Y-%m-%d %H:%M"))
    lines += ["", "---", ""]
    if not msgs:
        lines += ["（这个会话还没有留档的消息。留档从档案功能上线那天开始。）", ""]
    # 改过昵称的成员：正文按 mid 映射成现名，和表头、网页回放一致；没 mid 的老记录照旧
    cur_names = {mm.get("mid"): mm["name"] for mm in sess.get("members", []) if mm.get("mid")}
    for m in msgs:
        if m.get("think"):
            continue   # md导出保持纯对话；思考在网页版/PDF里可见
        if m.get("who") == "system":
            lines += ["（系统：%s）" % m.get("text", ""), "", "---", ""]
            continue
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"])) if m.get("ts") else ""
        nm = m.get("name", "?")
        if sess.get("type") == "group":
            if m.get("who") == "user":
                nm = group_user_name(sess)
            elif m.get("mid") in cur_names:
                nm = cur_names[m["mid"]]
        lines.append("**%s**（%s）：" % (nm, ts))
        lines.append("")
        if m.get("text"):
            lines += [_export_safe(m["text"]), ""]
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


def append_group_msg(key, who, name, text, imgs=None, stk=None, think=False,
                     mid=None, tell=False):
    rec = {"who": who, "name": name, "text": text, "ts": int(time.time())}
    if mid:
        rec["mid"] = mid       # 成员的稳定身份：改昵称后旧消息也能认出是谁
    if tell:
        rec["tell"] = True     # 系统条里成员也该知道的事（改名/换群主/改公告/改群名）
    if imgs:
        rec["imgs"] = imgs
    if stk:
        rec["stk"] = stk
    if think:
        rec["think"] = True
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
        if m.get("think"):
            continue   # 思考只给人看，不进成员的耳朵
        if m.get("who") == "system":
            if m.get("tell"):
                out.append("（系统通知：%s）" % m.get("text", ""))
            continue   # 其余系统灰条（中断/报错）只给人看
        line = "%s: %s" % (m.get("name", "?"), m.get("text", ""))
        if m.get("stk"):
            line += " [表情包:%s]" % ",".join(
                os.path.splitext(os.path.basename(x))[0] for x in m["stk"])
        if m.get("imgs"):
            line += " [附图%d张]" % len(m["imgs"])
        out.append(line)
    return "\n".join(out)


def group_user_name(g):
    """用户在这个群里的昵称（默认「用户」）。"""
    return (g.get("user_name") or "用户").strip() or "用户"


def group_owner_name(g):
    """群主的名字：owner 是 "user" 或某成员的 mid；找不到就当用户是群主。"""
    ow = g.get("owner") or "user"
    if ow != "user":
        for m in g.get("members", []):
            if m.get("mid") == ow:
                return m["name"]
    return group_user_name(g)


def member_label(m):
    return MODEL_LABELS.get(m.get("model") or "", m.get("model") or "默认型号")


def group_state_text(g):
    """群的当前状态一段话：群名/群主/成员/公告。补课时用。"""
    names = "、".join(m["name"] for m in g.get("members", [])) or "（暂无）"
    notice = (g.get("notice") or "").strip() or "（暂无）"
    return ("群名：「%s」；群主：%s；成员：%s（人类）、%s。\n【群公告】%s" % (
        g.get("name") or "群聊", group_owner_name(g), group_user_name(g), names, notice))


def check_nickname(g, name, self_mid=None):
    """昵称合法性。self_mid 是改名者自己（"user" 或成员 mid），重名检查时跳过自己。
    返回错误文案；合法返回空串。"""
    name = (name or "").strip()
    if not name:
        return "昵称不能为空"
    if len(name) > 20:
        return "昵称不能超过20个字"
    if "@" in name or "\n" in name or "[" in name or "]" in name:
        return "昵称不能含 @、[ ]、换行"
    if name == "用户" and self_mid == "user":
        pass   # 用户改回默认名可以
    elif name in RESERVED_NAMES:
        return "「%s」是保留词，不能当昵称" % name
    elif name.startswith(("所有人", "全体成员", "全员")):
        return "昵称不能以「所有人/全员」开头，@点名会被当成全员发言"
    if self_mid != "user" and name == group_user_name(g):
        return "「%s」已被用户占用" % name
    for m in g.get("members", []):
        if (self_mid is None or m.get("mid") != self_mid) and m.get("name") == name:
            return "「%s」已有人在用" % name
    return ""


# 成员回复里的群操作标签：[[改名:新昵称]]（谁都能改自己的）、[[公告:内容]]（只有群主生效）
GTAG_RE = re.compile(r"^[ \t]*\[\[(改名|昵称|rename|公告|notice)[:：][ \t]*(.*?)[ \t]*\]\][ \t]*$")


def extract_group_tags(text):
    """摘出单独成行的群操作标签（代码围栏里的不算）。
    返回 (去掉标签后的文本, 新昵称或None, 新公告或None)。"""
    out, name, notice = [], None, None
    fence = False
    for ln in text.split("\n"):
        if ln.strip().startswith("```"):
            fence = not fence
            out.append(ln)
            continue
        mt = None if fence else GTAG_RE.match(ln)
        if not mt:
            out.append(ln)
            continue
        kind, val = mt.group(1), mt.group(2).strip()
        if kind in ("改名", "昵称", "rename"):
            name = val
        else:
            notice = val
    return "\n".join(out).strip(), name, notice


RELAY_RULE = (
    "【接茬】想让某位成员接着回应，在回复的最后单独一行写 @名字（那一行只写这一个@）。"
    "正文里顺嘴提到的@不算点名。每条回复只有一个接茬名额，只点你认为最该接话的那一位；"
    "@用户、@所有人不会触发接茬。一轮里接茬最多接力3次，同一成员不会被重复叫起。"
)
INTRO_V = 3   # 群规则版本：老成员低于此版本时补一次课（2→3 加了接茬与分组讨论）


def room_intro(parent, room, member):
    """成员第一次在分组讨论室里发言时的说明：身在何处、谁在场、保密。"""
    names = "、".join(m["name"] for m in room.get("members", [])
                      if m.get("mid") != member.get("mid")) or "（只有你）"
    return ("(你现在在群「" + (parent.get("name") or "群聊") + "」的分组讨论室「" +
            (room.get("room") or {}).get("topic", "") + "」里。室内只有：" +
            group_user_name(parent) + "（人类）、你、" + names +
            "。这里的话大厅里的其他成员看不到，是私下讨论；你带着大厅里的全部记忆进来，"
            "什么时候结束由用户决定，结束后你会带着这里的记忆回到大厅。"
            "室内同样可以用最后一行 @名字 点人接茬。)")


def back_note(parent, topic):
    return ("(分组讨论室「" + topic + "」已结束，你已回到群「" + (parent.get("name") or "群聊") +
            "」大厅。大厅里没进室的成员不知道室内讨论的内容，那是室内的秘密，别主动泄露；"
            "下面是你离开期间大厅的新消息。)")


def relay_target(text, members, speaker, user_name):
    """成员回复的最后一行若是 @某成员，返回那个成员；@自己/@用户/@所有人/非成员 → None。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1].replace("＠", "@")
    if not last.startswith("@"):
        return None
    if re.match(r"^@(所有人|全体成员|全员)", last):
        return None
    if last.startswith("@" + user_name):
        return None
    for m in sorted(members, key=lambda x: -len(x["name"])):
        if last.startswith("@" + m["name"]):
            if m.get("mid") and m.get("mid") == speaker.get("mid"):
                return None
            if not m.get("mid") and m["name"] == speaker["name"]:
                return None
            return m
    return None


def active_room_of(d, gkey, mid):
    """成员当前在哪个进行中的讨论室（没有则 None）。"""
    for e in d.get("list", []):
        r = e.get("room") or {}
        if r.get("parent") == gkey and not r.get("ended") and mid in (r.get("mids") or []):
            return e
    return None


def group_intro(g, member):
    """新成员（或会话链重开的成员）的开场白：群名、群主、公告、成员、能力说明。"""
    me = member["name"]
    others = "、".join(m["name"] for m in g.get("members", [])
                      if m.get("mid") != member.get("mid") and m["name"] != me) or "（暂无）"
    uname = group_user_name(g)
    is_owner = (g.get("owner") or "user") == member.get("mid")
    notice = (g.get("notice") or "").strip() or "（暂无）"
    return (
        "(这是「Softshell」群聊窗口，本群群名：「" + (g.get("name") or "群聊") + "」。"
        "你是群成员「" + me + "」（型号 " + member_label(member) + "），"
        "群里还有：" + uname + "（人类）、" + others + "（其他Claude实例，和你一样各自独立，"
        "同型号的成员也互不相通）。群主：" + group_owner_name(g) + "。\n"
        "【群公告】" + notice + "\n"
        "下面的群聊转录里，「" + uname + "」是人类的发言，其他名字是别的成员；"
        "「系统通知」是群里的变动（改名、换群主、改公告、改群名）。"
        "请以「" + me + "」的身份直接发言，不要在开头带自己的名字，"
        "你说的话会转达给群里所有人。支持粗体、代码块、表格，但更适合聊天式短句。\n"
        "【改昵称】你可以改自己在本群的昵称：回复里单独一行写 [[改名:新昵称]]"
        "（20字以内，不能和别人重名），改完全群会收到通知，之后@新昵称才叫得到你。"
        "用户给你设定身份、角色时，可以顺手把昵称改成合身份的名字。\n"
        "【群公告】只有群主能改。" + (
            "你是群主，可以在回复里单独一行写 [[公告:内容]] 更新公告（整条公告写在这一行里）。" if is_owner
            else "你不是群主，写 [[公告:…]] 不会生效。") + "\n"
        + RELAY_RULE + "\n"
        "【界面】标题栏右上角的⋯菜单里有：查找聊天记录、群公告、群成员名单"
        "（用户可在里面改成员昵称、指定群主）、用户改自己的群昵称、分组讨论"
        "（用户可以把部分成员拉进一个分组讨论室私下讨论，大厅里的其他成员看不到室内的话）；"
        "状态栏的 effort 按钮对本群所有成员统一生效。\n"
        + STICKER_DIR + " 里有表情包，需要时可写 [[表情:文件名]] 发送"
        "（单独占一行，会显示成独立的一条表情消息；只写翻看后确认存在的文件名，"
        "猜错会穿帮成文字），直接打emoji也行。"
        "群成员的头像固定，群里不换装。)"
    )


# ── 语音通话v2（可选件）：流式管道 ──
# 采音在浏览器完成（Chromium自带回声消除），PCM喂给 /voice/feed；
# 桥接跑 VAD+SenseVoice；通话用常驻CLI进程流式生成，逐句切给前端念。
VOICE_DIR = os.path.join(DIR, "voice")
VOICE = {"rec": None, "vad": None, "queue": [], "err": "",
         "plock": threading.Lock(), "lock": threading.Lock(),
         "segs": [], "seg_cv": threading.Condition(), "worker": None,
         "ring": None, "ring_off": 0}
PREROLL = 4000   # VAD 触发前的 0.25 秒也一起送去识别：silero 反应慢半拍，不补会掐掉首音节


def _asr_worker():
    """识别线程：VAD 切出来的整句在这里解码。收音频的请求只做断句、立刻返回，
    否则识别慢一点（尤其全精度模型）浏览器那头就会丢掉正在说的下一句开头。"""
    while True:
        with VOICE["seg_cv"]:
            while not VOICE["segs"]:
                VOICE["seg_cv"].wait()
            seg = VOICE["segs"].pop(0)
        rec = VOICE["rec"]
        if rec is None:
            continue
        try:
            st = rec.create_stream()
            st.accept_waveform(16000, seg)
            rec.decode_stream(st)
            tx = hotword_fix(st.result.text.strip())
            if tx:
                with VOICE["lock"]:
                    VOICE["queue"].append(tx)
        except Exception as ex:              # noqa: BLE001
            VOICE["err"] = "识别异常：" + str(ex)[:200]


def _resample_to_16k(samples, sr):
    """浏览器的 AudioContext 不一定给 16k（Safari 会无视 sampleRate 选项），线性插值到 16k。"""
    import numpy as np
    if sr == 16000 or sr <= 0 or len(samples) == 0:
        return samples
    n = int(round(len(samples) * 16000.0 / sr))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)
CALLP = {"proc": None, "skey": "", "reader": None, "events": [],
         "turn": 0, "sid": None, "lock": threading.Lock()}
SENT_SPLIT_RE = re.compile(r"[^。！？!?\n；;]*[。！？!?\n；;]+")
CALL_HINT = (
    "(语音对讲模式：用户开了📞对讲机，正对着麦克风跟你说话，"
    "你的回复会被本机嗓音逐句念出来。"
    "①开头先用一两个字的语气词接茬（嗯、好、哈、行这类），再说正文——"
    "第一句越短，用户越早听到你的声音；"
    "②全程口语短句，绝对不要换行、不要分段、不要markdown排版和列表，"
    "像打电话一样一句接一句说；回复整体要短，两三句说完最好；"
    "③用户的话是语音转文字来的，会出现吃字、错别字、同音字、重复字——"
    "先按上下文猜最合理的意思，拿不准就直接跟用户确认，不要瞎脑补；"
    "④听不懂就问。)"
)
CALL_ENDED = set()   # 挂断过电话的会话：下一条文字消息告知它已回到打字聊天


def asr_model_path():
    """识别模型：voice/ 里有全精度 model.onnx 就优先用它，否则用 int8 量化版。"""
    for fn in ("model.onnx", "model.int8.onnx"):
        p = os.path.join(VOICE_DIR, fn)
        if os.path.isfile(p) and os.path.getsize(p) > 1000000:
            return p
    return ""


def voice_missing():
    miss = []
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        miss.append("未安装识别引擎：pip install sherpa-onnx")
    if not asr_model_path():
        miss.append("缺模型文件 voice" + os.sep + "model.int8.onnx（下载地址见README语音章节）")
    for fn in ("tokens.txt", "silero_vad.onnx"):
        if not os.path.isfile(os.path.join(VOICE_DIR, fn)):
            miss.append("缺模型文件 voice" + os.sep + fn + "（下载地址见README语音章节）")
    return miss


HOTWORDS_FILE = os.path.join(VOICE_DIR, "hotwords.txt")
_HOTWORDS = {"mtime": 0.0, "rules": []}


def load_hotwords():
    """热词纠正表：ASR按词频抢答（Claude听成cloud），转写后按表映射回来。
    文件用户可改，存盘即生效；不存在时播种默认表。"""
    try:
        mt = os.path.getmtime(HOTWORDS_FILE)
    except OSError:
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            with io.open(HOTWORDS_FILE, "w", encoding="utf-8") as f:
                f.write("# 语音热词纠正表：每行「听错的词=>该是的词」，#开头是注释\n"
                        "# 英文按整词匹配（cloudy不受影响），中文按原样替换\n"
                        "cloud=>Claude\n"
                        "克劳德=>Claude\n")
            mt = os.path.getmtime(HOTWORDS_FILE)
        except OSError:
            return _HOTWORDS["rules"]
    if mt != _HOTWORDS["mtime"]:
        rules = []
        try:
            for ln in io.open(HOTWORDS_FILE, encoding="utf-8-sig"):
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=>" not in ln:
                    continue
                bad, good = ln.split("=>", 1)
                bad, good = bad.strip(), good.strip()
                if not bad or not good:
                    continue
                if re.fullmatch(r"[A-Za-z' ]+", bad):
                    # \b在中文旁失灵（CJK也算词字符），只用英文字母做边界
                    rules.append((re.compile(
                        r"(?i)(?<![A-Za-z])" + re.escape(bad) + r"(?![A-Za-z])"), good))
                else:
                    rules.append((re.compile(re.escape(bad)), good))
        except OSError:
            pass
        _HOTWORDS["rules"] = rules
        _HOTWORDS["mtime"] = mt
    return _HOTWORDS["rules"]


def hotword_fix(text):
    for pat, good in load_hotwords():
        text = pat.sub(good, text)
    return text


def voice_engine_up():
    """加载识别引擎（只加载一次，通话间复用）。返回错误串，空串=成功。"""
    if VOICE["rec"] is not None:
        return ""
    try:
        import sherpa_onnx
        VOICE["rec"] = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=asr_model_path(),
            tokens=os.path.join(VOICE_DIR, "tokens.txt"),
            use_itn=True, language="auto", num_threads=2)
        vcfg = sherpa_onnx.VadModelConfig()
        vcfg.silero_vad.model = os.path.join(VOICE_DIR, "silero_vad.onnx")
        vcfg.silero_vad.threshold = 0.35          # 灵敏些，别掐掉首音节
        vcfg.silero_vad.min_silence_duration = 0.7
        vcfg.silero_vad.min_speech_duration = 0.2
        vcfg.silero_vad.max_speech_duration = 30
        vcfg.sample_rate = 16000
        VOICE["vad"] = sherpa_onnx.VoiceActivityDetector(
            vcfg, buffer_size_in_seconds=60)
        if VOICE["worker"] is None:
            VOICE["worker"] = threading.Thread(target=_asr_worker, daemon=True)
            VOICE["worker"].start()
        return ""
    except Exception as ex:                        # noqa: BLE001 可选件要能报错
        VOICE["rec"] = None
        VOICE["vad"] = None
        return str(ex)[:300]


# ── 本机朗读合成：Windows 常驻 PowerShell 工作进程（voice_tts.ps1，OneCore 嗓音）；
#    macOS 直接调系统 `say` 命令（婷婷等自带中文嗓音），一句一进程，不用常驻 ──
# 为什么不让浏览器自己念（speechSynthesis）：那条路的声音是系统在浏览器外面放的，
# 浏览器的回声消除拿不到参照，通话时麦克风会把它念的话录回去当成用户说的。
# 改成桥接合成、浏览器播放，声音就是浏览器放的，回声消除才有参照。
TTS_SCRIPT = os.path.join(DIR, "voice_tts.ps1")
TTSW = {"proc": None, "lock": threading.Lock(), "voices": ""}


def _tts_start():
    if not os.path.isfile(TTS_SCRIPT) or not IS_WIN:
        return None
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", TTS_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", **SPAWN)
        line = proc.stdout.readline().strip()
        if not line.startswith("ready"):
            kill_tree(proc)
            return None
        TTSW["voices"] = line[6:]
        return proc
    except OSError:
        return None


def _tts_mac(text, voice, rate):
    """macOS：say -v 嗓音 -r 语速 -o x.wav，16k 单声道。rate 是倍速，换算成每分钟词数。"""
    name = MAC_VOICES.get(voice) or MAC_VOICES.get("female") or ""
    if not name:
        return None
    out = os.path.join(tempfile.gettempdir(),
                       "softshell_tts_%d.wav" % threading.get_ident())
    try:
        subprocess.run(
            ["say", "-v", name, "-r", str(int(round(180 * rate))),
             "-o", out, "--data-format=LEI16@16000", "-f", "-"],
            input=text, text=True, capture_output=True, timeout=60, **SPAWN)
        with open(out, "rb") as f:
            data = f.read()
        os.remove(out)
        return data or None
    except (OSError, subprocess.SubprocessError):
        return None


def tts_synth(text, voice, rate):
    """合成一句 → wav 字节；失败返回 None（前端自动退回浏览器嗓音）。"""
    if IS_MAC:
        return _tts_mac(text, voice, rate)
    if not IS_WIN:
        return None
    with TTSW["lock"]:
        proc = TTSW["proc"]
        if proc is None or proc.poll() is not None:
            proc = TTSW["proc"] = _tts_start()
            if proc is None:
                return None
        out = os.path.join(tempfile.gettempdir(),
                           "softshell_tts_%d.wav" % threading.get_ident())
        try:
            proc.stdin.write(json.dumps({"text": text, "voice": voice, "rate": rate,
                                         "out": out}) + "\n")
            proc.stdin.flush()
            resp = proc.stdout.readline().strip()
        except OSError:
            kill_tree(proc)
            TTSW["proc"] = None
            return None
        if not resp.startswith("ok"):
            return None
        try:
            with open(out, "rb") as f:
                data = f.read()
            os.remove(out)
            return data
        except OSError:
            return None


def _call_emit(ev):
    with CALLP["lock"]:
        CALLP["events"].append(ev)
        if len(CALLP["events"]) > 500:
            CALLP["events"] = CALLP["events"][-300:]


def _archive_call_rec(skey, text, think=False):
    rec = {"who": "claude", "name": "Claude",
           "text": canonicalize_stickers(text) if not think else text,
           "ts": int(time.time())}
    if think:
        rec["think"] = True
    append_jsonl(history_path(skey), rec)


def call_reader():
    """常驻进程的读线程：partial流→逐句切给前端；完整消息落档；result收账。"""
    proc = CALLP["proc"]
    pending = ""
    turn_said = False

    def flush_pending(force=False):
        nonlocal pending, turn_said
        while True:
            m = SENT_SPLIT_RE.match(pending)
            if not m:
                break
            sent = m.group(0).strip()
            pending = pending[m.end():]
            if sent:
                turn_said = True
                _call_emit({"kind": "say", "text": sent, "turn": CALLP["turn"]})
        if force and pending.strip():
            turn_said = True
            _call_emit({"kind": "say", "text": pending.strip(),
                        "turn": CALLP["turn"]})
            pending = ""

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            t = obj.get("type")
            if t == "stream_event":
                ev = obj.get("event") or {}
                if ev.get("type") == "content_block_start":
                    cb = ev.get("content_block") or {}
                    bt = cb.get("type")
                    if bt == "thinking":
                        _call_emit({"kind": "status", "text": "思考中",
                                    "turn": CALLP["turn"]})
                    elif bt == "tool_use":
                        _call_emit({"kind": "status",
                                    "text": "正在用 " + (cb.get("name") or "工具"),
                                    "turn": CALLP["turn"]})
                    elif bt == "text":
                        _call_emit({"kind": "status", "text": "组织语言中",
                                    "turn": CALLP["turn"]})
                    if bt == "text" and pending.strip():
                        # 上一个文字块（工具调用前的那半句）先念掉，别和下一块拼成一句
                        flush_pending(force=True)
                if ev.get("type") == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta":
                        pending += d.get("text") or ""
                        flush_pending()
            elif t == "system" and obj.get("subtype") == "init":
                if obj.get("session_id"):
                    CALLP["sid"] = obj["session_id"]
                _call_emit({"kind": "status", "text": "对方已接听",
                            "turn": CALLP["turn"]})
            elif t == "assistant":
                for block in (obj.get("message") or {}).get("content", []):
                    bt = block.get("type")
                    if bt == "text" and block.get("text"):
                        _archive_call_rec(CALLP["skey"], block["text"])
                        if not turn_said:
                            # 没吃到partial流（旗标不支持等情况）：整段现切现念
                            pending += block["text"]
                        flush_pending(force=True)
                        _call_emit({"kind": "text", "text": block["text"],
                                    "turn": CALLP["turn"]})
                    elif bt == "thinking" and block.get("thinking"):
                        # 和文字聊天一样：思考链落档、发给前端显示
                        _archive_call_rec(CALLP["skey"], block["thinking"], think=True)
                        _call_emit({"kind": "thinking", "text": block["thinking"],
                                    "turn": CALLP["turn"]})
                    elif bt == "tool_use":
                        name = block.get("name") or "?"
                        detail = tool_detail(block)
                        append_jsonl(history_path(CALLP["skey"]),
                                     {"who": "system", "name": "系统",
                                      "text": "⚙ " + name + ((" · " + detail) if detail else ""),
                                      "ts": int(time.time())})
                        _call_emit({"kind": "tool", "name": name, "detail": detail,
                                    "turn": CALLP["turn"]})
            elif t == "result":
                if obj.get("session_id"):
                    CALLP["sid"] = obj["session_id"]
                flush_pending(force=True)
                u = obj.get("usage") or {}
                _call_emit({"kind": "turn_end", "turn": CALLP["turn"],
                            "tin": ((u.get("input_tokens") or 0) +
                                    (u.get("cache_read_input_tokens") or 0) +
                                    (u.get("cache_creation_input_tokens") or 0)),
                            "cr": u.get("cache_read_input_tokens") or 0,
                            "tout": u.get("output_tokens") or 0,
                            "ms": obj.get("duration_ms") or 0,
                            "err": (str(obj.get("result"))[:800]
                                    if obj.get("is_error") and obj.get("result") else "")})
                turn_said = False
    except (OSError, ValueError):
        pass
    _call_emit({"kind": "call_dead"})


def call_stop_proc():
    """挂断：杀进程、把会话链指针交还给文字聊天。"""
    proc = CALLP["proc"]
    if proc:
        kill_tree(proc)
    CALLP["proc"] = None
    if CALLP.get("sid") and CALLP.get("skey"):
        with SESS_LOCK:
            d = load_sessions()
            e = get_session(d, CALLP["skey"])
            if e:
                e["sid"] = CALLP["sid"]
                e["ts"] = int(time.time())
                save_sessions(d)
    if CALLP.get("skey"):
        CALL_ENDED.add(CALLP["skey"])
    CALLP["skey"] = ""



def migrate_member_avatars():
    """老群的成员头像原本实时引用源会话，改成独立快照。
    源会话已删的成员保持无头像——不擅自还原用户已经放弃的脸。"""
    with SESS_LOCK:
        d = load_sessions()
        changed = False
        for s in d["list"]:
            if s.get("type") != "group":
                continue
            for mm in s.get("members", []):
                if "avatar" not in mm:
                    rel = load_look_of(mm.get("srckey") or "").get("avatar_claude") or ""
                    mm["avatar"] = rel if sticker_path(rel) else ""
                    changed = True
        if changed:
            save_sessions(d)


def migrate_group_fields():
    """老群补齐新字段：成员 mid、群主、公告、用户昵称。老群照常能用。"""
    with SESS_LOCK:
        d = load_sessions()
        changed = False
        for s in d["list"]:
            if s.get("type") != "group":
                continue
            for i, mm in enumerate(s.get("members", [])):
                if not mm.get("mid"):
                    mm["mid"] = "m%s%02d" % (s["key"][1:], i)
                    changed = True
            for k, v in (("owner", "user"), ("notice", ""), ("user_name", "用户")):
                if k not in s:
                    s[k] = v
                    changed = True
            backfill_log_mids(s)
        if changed:
            save_sessions(d)


def backfill_log_mids(g):
    """老群日志里的成员记录没有 mid：按当时的名字对上现在的成员补上，
    改过昵称后旧消息才能跟着显示新名。幂等，没得补就不动文件。"""
    p = group_log_path(g["key"])
    if not p or not os.path.exists(p):
        return
    by_name = {m["name"]: m["mid"] for m in g.get("members", []) if m.get("mid")}
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return
    out, changed = [], False
    for ln in lines:
        if ln.strip():
            try:
                rec = json.loads(ln)
            except ValueError:
                out.append(ln)
                continue
            if (rec.get("who") == "member" and not rec.get("mid")
                    and rec.get("name") in by_name):
                rec["mid"] = by_name[rec["name"]]
                ln = json.dumps(rec, ensure_ascii=False)
                changed = True
        out.append(ln)
    if not changed:
        return
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        os.replace(tmp, p)
    except OSError:
        pass


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


def _adopt_root_images():
    """换装工作流偶尔把图搬到stickers根目录——面板只显示子文件夹，收编进「共享」"""
    try:
        for fn in os.listdir(STICKER_DIR):
            src = os.path.join(STICKER_DIR, fn)
            if os.path.isfile(src) and os.path.splitext(fn)[1].lower() in IMG_TYPES:
                dst_dir = os.path.join(STICKER_DIR, "共享")
                os.makedirs(dst_dir, exist_ok=True)
                dst = os.path.join(dst_dir, fn)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
    except OSError:
        pass


def sticker_groups():
    """扫 stickers 下的每个子文件夹，返回 [{"name": 页签名, "items": [文件名...]}]"""
    _adopt_root_images()
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


def fuzzy_sticker(name):
    """模糊救援：模型爱写短情绪词（"无奈"→"无奈-扶额.jpg"）。
    前缀优先，其次互相包含，取第一个命中。"""
    name = str(name).strip()
    if not name:
        return None
    pre, sub = [], []
    for grp in sticker_groups():
        for fn in grp["items"]:
            bare = os.path.splitext(fn)[0]
            rel = grp["name"] + "/" + fn
            if bare.startswith(name):
                pre.append(rel)
            elif name in bare or bare in name:
                sub.append(rel)
    return (pre or sub or [None])[0]


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
            hit = find_sticker_by_name(out[ck]) or fuzzy_sticker(out[ck])
            if hit:
                out[ck] = hit
            else:
                warns.append("%s 指向的图找不到: %s" % (ck, out[ck]))
    return out, warns


# ── 表情包穿帮自纠：模型编了不存在的文件名，下一轮悄悄告诉它 ──
STK_TAG_RE = re.compile(r"\[\[(?:表情|sticker)[:：]\s*([^\]]+?)\s*\]\]")
STK_WARN = {}   # skey 或 "gkey/成员名" → 上一轮没兑现的表情名列表


def unresolved_stickers(text):
    """找出指向不存在的图的表情标签。代码围栏里的是教学内容，不算数。"""
    bad = []
    parts = (text or "").split("```")
    for i in range(0, len(parts), 2):
        for name in STK_TAG_RE.findall(parts[i]):
            rel = name.replace("\\", "/")
            if sticker_path(rel) or find_sticker_by_name(rel) or fuzzy_sticker(rel):
                continue
            if name not in bad:
                bad.append(name)
    return bad


def canonicalize_stickers(text):
    """落档前把能解析的表情标签改写成「子文件夹/文件名.后缀」完整形，
    回放和搜索不再依赖模糊匹配；代码块围栏里的内容原样保留。"""
    def _rep(m):
        rel = m.group(1).replace("\\", "/")
        hit = ((rel if sticker_path(rel) else None) or
               find_sticker_by_name(rel) or fuzzy_sticker(rel))
        return ("[[表情:%s]]" % hit) if hit else m.group(0)
    parts = (text or "").split("```")
    for i in range(0, len(parts), 2):
        parts[i] = STK_TAG_RE.sub(_rep, parts[i])
    return "```".join(parts)


def stk_warn_text(bad):
    return ("(提示：你上一轮发的表情「" + "」「".join(bad[:3]) +
            "」在表情库里不存在，已按文字显示给用户。"
            "发表情前先List表情库文件夹确认文件名，没有合适的就用emoji或文字动作。)")


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
            return v.strip().replace("\n", " ")[:200]
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
        u = obj.get("usage") or {}
        cr = u.get("cache_read_input_tokens") or 0
        cc = u.get("cache_creation_input_tokens") or 0
        tin = (u.get("input_tokens") or 0) + cr + cc
        tout = u.get("output_tokens") or 0
        if tout or obj.get("duration_ms"):
            # 额度敏感的用户想知道每轮花了多少——缓存命中要拆开给，才能自证成本
            yield {"kind": "stats", "tin": tin, "tout": tout, "cr": cr, "cc": cc,
                   "ms": obj.get("duration_ms") or 0}


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
        if path == "/platform":
            self._send_bytes(
                json.dumps({"os": "win" if IS_WIN else ("mac" if IS_MAC else "linux"),
                            "key_mod": KEY_MOD,
                            "voices": VOICE_NAMES,
                            "sep": os.sep,
                            "claude": bool(claude_bin())},
                           ensure_ascii=False).encode("utf-8"),
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
            msgs = read_jsonl(archive_of(e))
            if (q.get("stat") or [""])[0]:
                # 只要统计不要全文：字数计数在服务端算，档案再大前端也不用拉
                cu = sum(len(m.get("text") or "") for m in msgs if m.get("who") == "user")
                cb = sum(len(m.get("text") or "") for m in msgs
                         if m.get("who") not in ("user", "system") and not m.get("think"))
                self._send_bytes(json.dumps(
                    {"total": len(msgs), "chars_user": cu, "chars_bot": cb},
                    ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", {"Cache-Control": "no-store"})
                return
            for i, m in enumerate(msgs):
                m["idx"] = i          # 稳定锚点：档案里的行号（追加式文件，行号不漂）
            kw = (q.get("q") or [""])[0]
            if kw:
                kl = kw.lower()
                msgs = [m for m in msgs if kl in str(m.get("text", "")).lower()]
            total = len(msgs)
            try:
                off = max(0, int((q.get("offset") or ["0"])[0]))
                lim = int((q.get("limit") or ["0"])[0])
            except ValueError:
                off, lim = 0, 0
            if off:
                msgs = msgs[off:]
            if lim > 0:
                msgs = msgs[:lim]
            out = {"list": msgs, "total": total,
                   "type": e.get("type", "chat")}
            if e.get("type") == "group":
                av, mems = {}, {}
                for m in e.get("members", []):
                    rel = m.get("avatar") or ""
                    rel = rel if sticker_path(rel) else ""
                    av[m["name"]] = rel
                    if m.get("mid"):
                        mems[m["mid"]] = {"name": m["name"], "avatar": rel}
                out["avatars"] = av
                out["members"] = mems   # 按 mid 映射：改过昵称的成员，旧消息也显示新名字
                out["user_name"] = group_user_name(e)
            self._send_bytes(
                json.dumps(out, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return
        if path == "/voice/status":
            self._send_bytes(json.dumps({
                "ready": not voice_missing(),
                "missing": voice_missing(),
                "engine": VOICE["rec"] is not None,
                "calling": bool(CALLP["proc"]),
                "err": VOICE.get("err", ""),
            }, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return
        if path == "/voice/poll":
            with VOICE["lock"]:
                texts = VOICE["queue"][:]
                VOICE["queue"] = []
            self._send_bytes(json.dumps({
                "text": " ".join(texts), "err": VOICE.get("err", ""),
            }, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return
        if path == "/call/events":
            with CALLP["lock"]:
                evs = CALLP["events"][:]
                CALLP["events"] = []
            self._send_bytes(json.dumps({"events": evs},
                                        ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8", {"Cache-Control": "no-store"})
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
                rid = str(json.loads(decode_body(self.rfile.read(n))).get("rid", ""))
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
        if self.path == "/voice/feed":
            # 浏览器送来的16kHz int16 PCM块：VAD断句→SenseVoice→结果进队列
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 2 * 1024 * 1024:
                self.send_error(400)
                return
            raw = self.rfile.read(n)
            if VOICE["rec"] is None:
                self.send_error(409)   # 引擎没起
                return
            try:
                import numpy as np
                try:
                    sr = int(self.headers.get("X-Sample-Rate") or 16000)
                except ValueError:
                    sr = 16000
                samples = (np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16)
                           .astype(np.float32) / 32768.0)
                samples = _resample_to_16k(samples, sr)
                segs = []
                with VOICE["plock"]:
                    vad = VOICE["vad"]
                    ring = VOICE["ring"]
                    ring = samples if ring is None else np.concatenate([ring, samples])
                    if len(ring) > 16000 * 60:          # 只留最近 60 秒
                        VOICE["ring_off"] += len(ring) - 16000 * 60
                        ring = ring[-16000 * 60:]
                    VOICE["ring"] = ring
                    vad.accept_waveform(samples)
                    while not vad.empty():
                        seg = vad.front
                        a = seg.start - VOICE["ring_off"]       # 段起点在 ring 里的位置
                        b = a + len(seg.samples)
                        if 0 <= a <= len(ring) and b <= len(ring):
                            segs.append(ring[max(0, a - PREROLL):b].copy())
                        else:
                            segs.append(seg.samples)
                        vad.pop()
                if segs:
                    with VOICE["seg_cv"]:
                        VOICE["segs"].extend(segs)
                        VOICE["seg_cv"].notify()
            except Exception as ex:              # noqa: BLE001
                VOICE["err"] = "识别异常：" + str(ex)[:200]
            self._send_bytes(b'{"ok": true}', "application/json; charset=utf-8")
            return
        if self.path == "/call/start":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            miss = voice_missing()
            if miss or not CLAUDE:
                self._send_bytes(json.dumps({"ok": False, "missing": miss},
                                            ensure_ascii=False).encode("utf-8"),
                                 "application/json; charset=utf-8")
                return
            err = voice_engine_up()
            if err:
                self._send_bytes(json.dumps({"ok": False, "err": err},
                                            ensure_ascii=False).encode("utf-8"),
                                 "application/json; charset=utf-8")
                return
            with SESS_LOCK:
                d = load_sessions()
                sess = get_session(d, str(p.get("key", "")))
            if not sess or sess.get("type") == "group":
                self._send_bytes(json.dumps(
                    {"ok": False, "err": "\u7fa4\u804a\u6682\u4e0d\u652f\u6301\u901a\u8bdd"},
                    ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")
                return
            if CALLP["proc"]:
                call_stop_proc()
            cmd = [claude_bin(), "-p", "--input-format", "stream-json",
                   "--output-format", "stream-json", "--verbose",
                   "--include-partial-messages",
                   "--dangerously-skip-permissions"]
            if valid_model(sess.get("model") or ""):
                cmd += ["--model", sess["model"]]
            cmd += ["--effort", "low"]   # 通话要的是秒回：模型照用户选的，effort一律low
            if sess.get("sid"):
                cmd += ["--resume", sess["sid"]]
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=HOME,
                    encoding="utf-8", errors="replace", **SPAWN)
            except OSError as ex:
                self._send_bytes(json.dumps({"ok": False, "err": str(ex)[:200]},
                                            ensure_ascii=False).encode("utf-8"),
                                 "application/json; charset=utf-8")
                return
            with CALLP["lock"]:
                CALLP["events"] = []
            CALLP["proc"] = proc
            CALLP["skey"] = sess["key"]
            CALLP["turn"] = 0
            CALLP["sid"] = sess.get("sid") or None
            with VOICE["lock"]:
                VOICE["queue"] = []
            VOICE["err"] = ""
            th = threading.Thread(target=call_reader, daemon=True)
            CALLP["reader"] = th
            th.start()
            self._send_bytes(b'{"ok": true}', "application/json; charset=utf-8")
            return
        if self.path == "/call/say":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            text = str(p.get("text") or "").strip()
            proc = CALLP["proc"]
            if not text or not proc:
                self.send_error(409)
                return
            skey = CALLP["skey"]
            append_jsonl(history_path(skey),
                         {"who": "user", "name": "用户", "text": text,
                          "ts": int(time.time())})
            send_text = text
            vid = p.get("voice")
            if CALLP["turn"] == 0:
                if not CALLP.get("sid"):
                    send_text += "\n\n" + intro_text(skey, vid)   # 新会话链的开场白
                send_text += "\n\n" + CALL_HINT              # 每次开麦的第一句都带
            vchg = voice_change_text(skey, vid)
            if vchg:
                send_text += "\n\n" + vchg
            CALLP["turn"] += 1
            _call_emit({"kind": "status", "text": "已送达，等待Claude响应",
                        "turn": CALLP["turn"]})
            try:
                proc.stdin.write(json.dumps({
                    "type": "user",
                    "message": {"role": "user",
                                "content": [{"type": "text", "text": send_text}]},
                }, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except OSError:
                self.send_error(500)
                return
            self._send_bytes(json.dumps({"ok": True, "turn": CALLP["turn"]})
                             .encode("utf-8"), "application/json; charset=utf-8")
            return
        if self.path == "/call/stop":
            call_stop_proc()
            self._send_bytes(b'{"ok": true}', "application/json; charset=utf-8")
            return
        if self.path == "/group/new":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            gname = str(p.get("name", "")).strip()[:50] or "群聊"
            # 直接按型号拉人：[{model, count}]。不依赖私聊会话，成员各自是全新的会话链
            members, taken = [], set()
            for sp in (p.get("members") or []):
                if not isinstance(sp, dict):
                    continue
                model = str(sp.get("model") or "")
                try:
                    cnt = int(sp.get("count") or 0)
                except (TypeError, ValueError):
                    cnt = 0
                if not valid_model(model) or cnt <= 0:
                    continue
                cnt = min(cnt, 9)
                label = MODEL_LABELS.get(model, model)
                for i in range(cnt):
                    # 同型号拉多个：Fable 5.1-1、Fable 5.1-2；只拉一个不带编号
                    base = label if cnt == 1 else "%s-%d" % (label, i + 1)
                    nm, k = base, 2
                    while nm in taken:
                        nm = "%s-%d" % (base, k)
                        k += 1
                    taken.add(nm)
                    members.append({
                        "mid": "m%d%02d" % (int(time.time() * 1000), len(members)),
                        "name": nm, "model": model, "effort": "", "sid": "",
                        "read": 0, "avatar": "",
                    })
            if len(members) < 2:
                self.send_error(400)
                return
            with SESS_LOCK:
                d = load_sessions()
                g = new_session_entry(gname)
                g["type"] = "group"
                g["members"] = members
                g["owner"] = "user"       # 群主：初始是用户，可在成员名单里交给某个成员
                g["notice"] = ""
                g["user_name"] = "用户"
                d["list"].insert(0, g)
                d["active"] = g["key"]
                save_sessions(d)
            self._send_bytes(json.dumps(g, ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8")
            return
        if self.path == "/group/room/new":
            # 分组讨论室：从大厅群里挑成员进一个私密的子会话；成员链不断，记忆自然带进带出
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            topic = str(p.get("topic") or "").strip()[:100] or "分组讨论"
            mids = [str(x) for x in (p.get("mids") or []) if x]
            with SESS_LOCK:
                d = load_sessions()
                g = get_session(d, str(p.get("key", "")))
                if not g or g.get("type") != "group" or g.get("room"):
                    self._bad("只能在群聊大厅里建分组讨论室", 404)
                    return
                picked = [m for m in g.get("members", []) if m.get("mid") in mids]
                if not picked:
                    self._bad("至少选一个成员")
                    return
                for m in picked:
                    br = active_room_of(d, g["key"], m["mid"])
                    if br:
                        self._bad("「%s」已经在讨论室「%s」里了" % (
                            m["name"], (br.get("room") or {}).get("topic", "")))
                        return
                rm = new_session_entry((g.get("name") or "群聊") + " · 分组：" + topic)
                while get_session(d, rm["key"]):
                    rm["key"] = "s%d" % (int(rm["key"][1:]) + 1)
                rm["type"] = "group"
                rm["room"] = {"parent": g["key"], "topic": topic, "ended": False,
                              "mids": [m["mid"] for m in picked]}
                rm["members"] = [{"mid": m["mid"], "name": m["name"], "model": m.get("model", ""),
                                  "effort": "", "sid": m.get("sid", ""), "read": 0,
                                  "avatar": m.get("avatar", ""), "intro_v": INTRO_V}
                                 for m in picked]
                rm["owner"] = "user"
                rm["notice"] = ""
                rm["user_name"] = g.get("user_name") or "用户"
                try:
                    i = d["list"].index(g)
                except ValueError:
                    i = 0
                d["list"].insert(i + 1, rm)
                d["active"] = rm["key"]
                save_sessions(d)
                names = "、".join(m["name"] for m in picked)
            append_group_msg(g["key"], "system", "系统",
                             "%s把%s拉进了分组讨论室「%s」，讨论期间他们不在大厅"
                             % (group_user_name(g), names, topic), tell=True)
            self._send_bytes(json.dumps(rm, ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8")
            return
        if self.path == "/group/room/end":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            with SESS_LOCK:
                d = load_sessions()
                rm = get_session(d, str(p.get("key", "")))
                if not rm or not rm.get("room"):
                    self._bad("这不是分组讨论室", 404)
                    return
                if rm["room"].get("ended"):
                    self._bad("这个讨论已经结束过了")
                    return
                rm["room"]["ended"] = True
                if not str(rm.get("name", "")).startswith("【已结束】"):
                    rm["name"] = "【已结束】" + (rm.get("name") or "")
                topic = rm["room"].get("topic", "")
                pg = get_session(d, rm["room"].get("parent", ""))
                names = "、".join(m["name"] for m in rm.get("members", []))
                if pg:
                    for px in pg.get("members", []):
                        if px.get("mid") in (rm["room"].get("mids") or []):
                            px["back_from"] = topic   # 下次在大厅开口前先告诉它回来了
                    if rm.get("key") == d.get("active"):
                        d["active"] = pg["key"]
                save_sessions(d)
            append_group_msg(rm["key"], "system", "系统",
                             "%s结束了分组讨论，%s回到大厅" % (group_user_name(rm), names), tell=True)
            if pg:
                append_group_msg(pg["key"], "system", "系统",
                                 "分组讨论室「%s」结束，%s回到了大厅" % (topic, names), tell=True)
            self._send_bytes(json.dumps({"parent": pg["key"] if pg else ""}).encode("utf-8"),
                             "application/json; charset=utf-8")
            return
        if self.path in ("/group/notice", "/group/owner",
                         "/group/member_rename", "/group/user_name"):
            # 群管理：公告（仅群主是用户时可从界面改）、指定群主、改成员昵称、改用户昵称
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            key = str(p.get("key", ""))
            with SESS_LOCK:
                d = load_sessions()
                g = get_session(d, key)
                if not g or g.get("type") != "group":
                    self.send_error(404)
                    return
                msg = ""
                if self.path == "/group/notice":
                    if (g.get("owner") or "user") != "user":
                        self._bad("群主是「%s」，只有群主能改公告" % group_owner_name(g), 403)
                        return
                    notice = str(p.get("notice") or "").strip()[:500]
                    g["notice"] = notice
                    uname = group_user_name(g)
                    msg = ("%s更新了群公告：%s" % (uname, notice)) if notice \
                        else ("%s清空了群公告" % uname)
                elif self.path == "/group/owner":
                    mid = str(p.get("mid") or "user")
                    if mid != "user" and not any(
                            m.get("mid") == mid for m in g.get("members", [])):
                        self._bad("没有这个成员")
                        return
                    g["owner"] = mid
                    msg = "群主变更为「%s」" % group_owner_name(g)
                elif self.path == "/group/user_name":
                    name = str(p.get("name") or "").strip()
                    err = check_nickname(g, name, "user")
                    if err:
                        self._bad(err)
                        return
                    old = group_user_name(g)
                    g["user_name"] = name
                    msg = "用户「%s」把自己的群昵称改为「%s」" % (old, name)
                else:   # /group/member_rename
                    mid = str(p.get("mid") or "")
                    name = str(p.get("name") or "").strip()
                    mm = None
                    for x in g.get("members", []):
                        if x.get("mid") == mid:
                            mm = x
                            break
                    if not mm:
                        self._bad("没有这个成员")
                        return
                    err = check_nickname(g, name, mid)
                    if err:
                        self._bad(err)
                        return
                    old = mm["name"]
                    mm["name"] = name
                    msg = "%s把「%s」的群昵称改为「%s」" % (group_user_name(g), old, name)
                save_sessions(d)
                if msg:
                    append_group_msg(key, "system", "系统", msg, tell=True)
            self._send_bytes(b"ok", "text/plain")
            return
        if self.path == "/session/clone":
            # 复制会话：同一份记忆（sid）分叉成两个窗口，各走各的。
            # 下一轮各自 --resume 同一个旧sid，CLI给各自发新sid，从此互不影响
            n = int(self.headers.get("Content-Length", 0))
            try:
                pl = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            with SESS_LOCK:
                d = load_sessions()
                src = get_session(d, str(pl.get("key", "")))
                if not src:
                    self.send_error(404)
                    return
                e = json.loads(json.dumps(src, ensure_ascii=False))   # 深拷贝：群成员各自的sid一起带走
                base = int(time.time() * 1000)
                while get_session(d, "s%d" % base):
                    base += 1
                e["key"] = "s%d" % base
                e["name"] = (src.get("name") or "未命名") + "-副本"
                e["ts"] = int(time.time())
                e.pop("pin", None)
                try:
                    i = d["list"].index(src)
                except ValueError:
                    i = 0
                d["list"].insert(i + 1, e)   # 副本排在原会话正下方
                save_sessions(d)
            # 聊天档案和外观文件跟着拷，副本才有完整的回放和换装状态
            for pf in (history_path, group_log_path, look_path):
                sp, dp = pf(src["key"]), pf(e["key"])
                if sp and dp and os.path.isfile(sp):
                    try:
                        shutil.copyfile(sp, dp)
                    except OSError:
                        pass
            self._send_bytes(json.dumps(e, ensure_ascii=False).encode("utf-8"),
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
        if self.path == "/session/delete_all":
            # 一键清空：所有会话连同它们的聊天档案、群转录、外观文件一起删，
            # 只留一个全新的空会话。Claude Code 那边的会话档案不动。
            if CALLP["proc"]:
                call_stop_proc()
            with SESS_LOCK:
                d = load_sessions()
                for s0 in d["list"]:
                    k0 = s0.get("key") or ""
                    for pth in (look_path(k0), group_log_path(k0), history_path(k0)):
                        if pth and os.path.exists(pth):
                            try:
                                os.remove(pth)
                            except OSError:
                                pass
                e = new_session_entry()
                d["list"] = [e]
                d["active"] = e["key"]
                save_sessions(d)
            self._send_bytes(json.dumps(e, ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8")
            return
        if self.path in ("/session/switch", "/session/rename",
                         "/session/delete", "/session/pin"):
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
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
                        old_name = e.get("name") or ""
                        e["name"] = name
                        if e.get("type") == "group":
                            if old_name != name:   # 成员下一轮从转录里知道群改名了
                                append_group_msg(key, "system", "系统",
                                                 "群名已改为「%s」" % name, tell=True)
                        else:
                            # 同步群里分身的登记名：改完名@新名字才叫得到人
                            for g2 in d["list"]:
                                for mm in (g2.get("members") or []):
                                    if mm.get("srckey") != key:
                                        continue
                                    # 走同一套昵称校验：重名加编号；保留词/超长/含@就不同步
                                    nn = None
                                    for i2 in range(1, 10):
                                        cand = name if i2 == 1 else name + str(i2)
                                        if not check_nickname(g2, cand, mm.get("mid")):
                                            nn = cand
                                            break
                                    if nn and nn != mm.get("name"):
                                        old_mn = mm.get("name") or ""
                                        mm["name"] = nn
                                        append_group_msg(
                                            g2["key"], "system", "系统",
                                            "「%s」随私聊会话改名，群昵称变为「%s」" % (old_mn, nn),
                                            tell=True)
                elif self.path == "/session/pin":
                    if "pin" not in p:
                        self.send_error(400)   # 缺字段就报错，别把漏写当"取消置顶"
                        return
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
                p = json.loads(decode_body(self.rfile.read(n)))
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
        if self.path == "/export_html":
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 100 * 1024 * 1024:
                self.send_error(400)
                return
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            with SESS_LOCK:
                d = load_sessions()
                e = get_session(d, str(p.get("key", "")))
            if not e:
                self.send_error(404)
                return
            html = str(p.get("html") or "")
            if not html:
                self.send_error(400)
                return
            name = re.sub(r'[\\/:*?"<>|]', "_", e.get("name") or "会话").strip() or "会话"
            base = "%s-%s" % (name, time.strftime("%Y%m%d-%H%M%S"))
            try:
                os.makedirs(EXPORT_DIR, exist_ok=True)
                hp = os.path.join(EXPORT_DIR, base + ".html")
                with open(hp, "w", encoding="utf-8") as f:
                    f.write(html)
            except OSError:
                self.send_error(500)
                return
            pdf = ""
            if BROWSER:
                # 无头打印成真文本PDF：文字可选中/可搜索/可编辑，不是截图。
                # 单独的user-data-dir防止和正在开着的Edge窗口抢配置目录
                pp = os.path.join(EXPORT_DIR, base + ".pdf")
                prof = os.path.join(tempfile.gettempdir(), "softshell-pdf-profile")
                try:
                    subprocess.run(
                        [BROWSER, "--headless", "--disable-gpu",
                         "--user-data-dir=" + prof,
                         "--no-pdf-header-footer",
                         "--print-to-pdf=" + pp,
                         "file:///" + os.path.abspath(hp).replace("\\", "/").lstrip("/")],
                        **SPAWN, capture_output=True, timeout=90)
                    if os.path.isfile(pp) and os.path.getsize(pp) > 0:
                        pdf = pp
                except (OSError, subprocess.SubprocessError):
                    pass
            self._send_bytes(
                json.dumps({"html": hp, "pdf": pdf}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8")
            return
        if self.path == "/speak":
            # 本机嗓音合成（康康/慧慧），前端拿 wav 用音频元素播——回声消除才管用
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            text = str(p.get("text") or "").strip()[:600]
            voice = "male" if p.get("voice") == "local-male" else "female"
            try:
                rate = float(p.get("rate") or 1.3)
            except (TypeError, ValueError):
                rate = 1.3
            if not text:
                self.send_error(400)
                return
            data = tts_synth(text, voice, max(0.5, min(3.0, rate)))
            if not data:
                self.send_error(501)   # 合成不可用：前端退回浏览器嗓音
                return
            self._send_bytes(data, "audio/wav", {"Cache-Control": "no-store"})
            return
        if self.path == "/tts":
            # 在线神经嗓音（可选升舱）：桥接代理 edge-tts，前端拿mp3来播
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            text = str(p.get("text") or "").strip()[:600]
            voice = str(p.get("voice") or "")
            rate = str(p.get("rate") or "+30%")   # 默认+30%：正常聊天的爽快语速
            if not re.match(r"^[+-]\d{1,3}%$", rate):
                rate = "+30%"
            if not text or not re.match(
                    r"^[a-z]{2,3}-[A-Z]{2}(-[a-z]+)?-[A-Za-z]{2,40}Neural$", voice):
                self.send_error(400)
                return
            try:
                import edge_tts
            except ImportError:
                self.send_error(501)   # 没装升舱包：前端自动降级本地嗓音
                return
            import asyncio

            async def _gen():
                buf = bytearray()
                async for ch in edge_tts.Communicate(text, voice, rate=rate).stream():
                    if ch["type"] == "audio":
                        buf.extend(ch["data"])
                return bytes(buf)

            try:
                data = asyncio.run(asyncio.wait_for(_gen(), timeout=25))
            except Exception:            # noqa: BLE001 断网/限流一律降级，不能砸通话
                self.send_error(502)
                return
            if not data:
                self.send_error(502)
                return
            self._send_bytes(data, "audio/mpeg", {"Cache-Control": "no-store"})
            return
        if self.path == "/prefs":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(decode_body(self.rfile.read(n)))
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
            payload = json.loads(decode_body(self.rfile.read(n)))
        except (ValueError, UnicodeDecodeError):
            self._ndjson_error("请求体不是有效的JSON")
            return
        rid = str(payload.get("rid", "")) or ("r%d" % int(time.time() * 1000))
        text = str(payload.get("text", "")).strip()
        if not re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\s]+", "", text):
            text = ""   # 只剩零宽/空白字符＝空消息，别让隐形字符白烧一轮
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
            self._ndjson_error("空消息：没有可发送的内容")
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
        if not self._claim_send(skey):
            return
        try:
            if sess.get("type") == "group":
                self._handle_group(sess, text, images, up_names, stk_rels, rid)
            else:
                self._handle_chat(sess, skey, text, images, up_names, stk_rels,
                                  n_stickers, rid, payload.get("voice"))
        finally:
            with SENDING_LOCK:
                SENDING.discard(skey)
        return

    def _bad(self, msg, code=400):
        """带原因的失败：纯文本正文，前端原样给用户看。"""
        data = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ndjson_error(self, msg):
        """/send 是流式接口：出错也按流的约定回，别甩HTML错误页给脚本"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.emit_lock = threading.Lock()
        self._emit({"kind": "error", "text": msg})
        self._emit({"kind": "done"})

    def _claim_send(self, skey):
        """同一会话一次只许一轮在跑。抢不到锁就体面拒绝，消息不落档不烧额度。"""
        if CALLP["proc"] and CALLP["skey"] == skey:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.emit_lock = threading.Lock()
            self._emit({"kind": "error",
                        "text": "这个会话正在语音通话中，先挂断再打字。"})
            self._emit({"kind": "done"})
            return False
        with SENDING_LOCK:
            if skey in SENDING:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.emit_lock = threading.Lock()
                self._emit({"kind": "error",
                            "text": "这个会话上一轮还没说完，这条没有发出去。"
                                    "等它说完或按■停止后再发一次。"})
                self._emit({"kind": "done"})
                return False
            SENDING.add(skey)
            return True

    def _handle_chat(self, sess, skey, text, images, up_names, stk_rels,
                     n_stickers, rid, vid=None):
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
        bad_prev = STK_WARN.pop(skey, None)
        if bad_prev:
            text += "\n\n" + stk_warn_text(bad_prev)
        if skey in CALL_ENDED:
            CALL_ENDED.discard(skey)
            text += "\n\n(刚才的语音对讲已关闭，现在回到文字聊天，可以正常排版。)"
        cur_sig = look_state_line(skey)
        if not sid:
            text += "\n\n" + intro_text(skey, vid)
        elif cur_sig != LAST_LOOK_SIG.get(skey):
            # 外观变了（多半是Claude自己上一轮改的）：给它一行回读确认。
            # 桥接刚重启时也会注入一次，保证停机期间的变化不被错过。
            text += "\n\n(" + cur_sig + ")"
        LAST_LOOK_SIG[skey] = cur_sig
        vchg = voice_change_text(skey, vid)
        if vchg:
            text += "\n\n" + vchg
        echg = effort_change_text(skey, sess.get("effort") or "")
        if echg:
            text += "\n\n" + echg

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        if not claude_bin():
            self._emit({"kind": "error", "text":
                        "没找到 claude 命令。请先安装 Claude Code CLI："
                        + INSTALL_HINT})
            self._emit({"kind": "done"})
            return

        base = [claude_bin(), "-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages",
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
            t_round = time.time()
            cmd = list(base)
            if sid:
                cmd += ["--resume", sid]
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, cwd=HOME,
                    encoding="utf-8", errors="replace", **SPAWN,
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
            round_texts = []
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if obj.get("type") == "stream_event":
                        # 逐段流：text_delta转发给前端打字机显示；完整text块随后照旧落档
                        d = (obj.get("event") or {})
                        if d.get("type") == "content_block_delta":
                            dd = d.get("delta") or {}
                            if dd.get("type") == "text_delta" and dd.get("text"):
                                self._emit({"kind": "delta", "text": dd["text"]})
                        continue
                    for ev in translate(obj):
                        if ev.get("kind") == "session" and ev.get("id"):
                            new_sid = ev["id"]
                        elif ev.get("kind") == "thinking":
                            append_jsonl(history_path(skey),
                                         {"who": "claude", "name": "Claude", "think": True,
                                          "text": ev.get("text") or "",
                                          "ts": int(time.time())})
                        elif ev.get("kind") == "text":
                            last_text = ev["text"]
                            round_texts.append(ev["text"])
                            append_jsonl(history_path(skey),
                                         {"who": "claude", "name": "Claude",
                                          "text": canonicalize_stickers(ev["text"]),
                                          "ts": int(time.time())})
                        elif ev.get("kind") == "tool":
                            append_jsonl(history_path(skey),
                                         {"who": "system", "name": "系统",
                                          "text": "⚙ " + ev.get("name", "") +
                                                  ((" · " + ev["detail"]) if ev.get("detail") else ""),
                                          "ts": int(time.time())})
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
            bad_stk = unresolved_stickers("\n".join(round_texts))
            if bad_stk:
                STK_WARN[skey] = bad_stk   # 下一轮开头悄悄提醒它
            if was_stopped:
                # 用户按了停止：不要当成会话失效去重试，否则中断白按。
                # CLI被杀就不报usage了，至少把耗时和"计量不到"如实交代
                self._emit({"kind": "stats", "tin": 0, "tout": 0, "cr": 0, "cc": 0,
                            "ms": int((time.time() - t_round) * 1000), "cut": True})
                self._finish_session(skey, new_sid, raw_text, last_text)
                # 打断时机决定这条消息有没有进模型记忆——两种结果要说清楚
                stop_note = ("已停止（这条消息已进入会话记忆，下轮它可能还记得）"
                             if (emitted or new_sid) else
                             "已停止（进程还没跑起来，这条消息大概率没进记忆，需要可重发）")
                append_jsonl(history_path(skey),
                             {"who": "system", "name": "系统",
                              "text": stop_note, "ts": int(time.time())})
                self._emit({"kind": "note", "text": stop_note})
                self._emit({"kind": "done"})
                return
            if proc.returncode != 0 and not last_text:
                if sid and attempt == 1:
                    # 旧会话接不上（被清理/ID损坏）：自动开新对话重试一次。
                    # 新链的第一条消息要补开场白，不然它不认识表情包和换装协议
                    sid = None
                    text += "\n\n" + intro_text(skey, vid)
                    self._emit({"kind": "note", "text": "旧会话接不上了，已自动开新对话"})
                    continue
                err = ("".join(err_buf)).strip()[-800:]
                eobj = err_ev or {"kind": "error",
                                  "text": err or ("claude退出码 %s" % proc.returncode)}
                self._emit(eobj)
                append_jsonl(history_path(skey),
                             {"who": "system", "name": "系统",
                              "text": "错误：" + str(eobj.get("text", ""))[:300],
                              "ts": int(time.time())})
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
        cmd = [claude_bin(), "-p", "--output-format", "stream-json", "--verbose",
               "--dangerously-skip-permissions"]
        if valid_model(member.get("model") or ""):
            cmd += ["--model", member["model"]]
        if (member.get("effort") or "") in EFFORTS:
            cmd += ["--effort", member["effort"]]
        if member.get("sid"):
            cmd += ["--resume", member["sid"]]
        rel = member.get("avatar") or ""
        minfo = {"name": member["name"], "avatar": rel if sticker_path(rel) else "",
                 "mid": member.get("mid") or ""}
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=HOME,
                encoding="utf-8", errors="replace", **SPAWN,
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
        thinks = []
        t0 = time.time()
        saw_stats = False
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
                    if ev.get("kind") == "stats":
                        saw_stats = True
                    if ev.get("kind") == "thinking":
                        thinks.append(ev.get("text") or "")
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
        if stopped and not saw_stats:
            try:
                self._emit({"kind": "stats", "tin": 0, "tout": 0, "cr": 0, "cc": 0,
                            "ms": int((time.time() - t0) * 1000), "cut": True,
                            "member": minfo})
            except (ConnectionError, OSError):
                pass
        return {"text": "\n".join(texts).strip(), "sid": new_sid,
                "thinks": thinks, "stopped": stopped, "rc": proc.returncode,
                "err": ("".join(err_buf)).strip()[-800:]}

    def _member_prompt(self, g, member, images, fresh, parent=None):
        """拼一个成员这轮看到的内容：新成员给开场白，老成员给转录增量。
        fresh=True 表示会话链刚重开，把全量转录补给它。
        parent 非空表示 g 是分组讨论室，parent 是它所属的大厅群。"""
        gkey = g["key"]
        start = 0 if fresh else member.get("read", 0)
        msgs, total = read_group_msgs(gkey, start)
        member["_seen"] = total   # 拼提示词时转录的总条数：落账推已读指针时要对照
        parts = []
        if parent is not None and not member.get("room_intro"):
            # 第一次在讨论室里开口：先说明身在何处（链没断，大厅记忆它本来就有）
            if fresh:
                parts.append(group_intro(parent, member))
            parts.append(room_intro(parent, g, member))
            member["_room_intro_due"] = True
        elif fresh:
            parts.append(group_intro(g, member))
        if parent is None and member.get("back_from"):
            parts.append(back_note(g, member["back_from"]))
            member["_back_done"] = True
        if fresh:
            if msgs:
                parts.append("[群聊转录]\n" + transcript_text(msgs))
        else:
            if (member.get("intro_v") or 0) < INTRO_V:
                # 群功能升级前入群的老成员：补一次群状态和新能力，只补这一回
                parts.append("[群状态补课]\n" + group_state_text(g) +
                             "\n（你可以在回复里单独一行写 [[改名:新昵称]] 改自己的群昵称；"
                             "群主可写 [[公告:内容]] 改群公告；标题栏⋯菜单里有公告、成员名单和分组讨论。）\n" +
                             RELAY_RULE)
            if msgs:
                parts.append("[群聊转录·你上次发言之后的新消息]\n" + transcript_text(msgs))
        if images:
            parts.append("(用户发的图片，请先用Read工具查看: " + " ; ".join(images) + ")")
        bad_prev = STK_WARN.pop(gkey + "/" + (member.get("mid") or member["name"]), None)
        if bad_prev:
            parts.append(stk_warn_text(bad_prev))
        parts.append("请以「" + member["name"] + "」的身份就上面的对话发言。"
                     "（要点人接茬就在最后单独一行写 @名字，只能点一个。）")
        return "\n\n".join(parts)

    def _handle_group(self, sess, text, images, up_names, stk_rels, rid):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.emit_lock = threading.Lock()
        if not claude_bin():
            self._emit({"kind": "error", "text":
                        "没找到 claude 命令。请先安装 Claude Code CLI："
                        + INSTALL_HINT})
            self._emit({"kind": "done"})
            return
        gkey = sess["key"]
        members = sess.get("members") or []
        all_names = [m["name"] for m in members]
        room = sess.get("room") or None
        parent = None
        if room:
            if room.get("ended"):
                self._emit({"kind": "error", "text": "这个分组讨论已经结束，不能再发言（记录仍可查看和导出）"})
                self._emit({"kind": "done"})
                return
            with SESS_LOCK:
                parent = get_session(load_sessions(), room.get("parent", ""))
            if not parent:
                self._emit({"kind": "error", "text": "这个讨论室所属的群已经不在了"})
                self._emit({"kind": "done"})
                return
        append_group_msg(gkey, "user", group_user_name(sess), text, imgs=up_names, stk=stk_rels)
        # @路由：长名字先匹配防前缀重叠；按@出现的位置排队——@谁在前谁先说
        tmp = text.replace("＠", "@")   # 中文输入法的全角＠一视同仁
        if re.search(r"(?<![A-Za-z0-9._%+-])@(所有人|全体成员|全员)", tmp):
            targets = list(members)   # 微信肌肉记忆：@所有人＝全员按入群顺序依次发言
        else:
            hits = []
            for m in sorted(members, key=lambda x: -len(x["name"])):
                # @前面若是邮箱式字符（test@阿甲.com）就不算点名
                pat = re.compile(r"(?<![A-Za-z0-9._%+-])@" + re.escape(m["name"]))
                mt = pat.search(tmp)
                if mt:
                    hits.append((mt.start(), m))
                    tmp = pat.sub(lambda mo: "\x00" * len(mo.group(0)), tmp)
            targets = [m for _, m in sorted(hits, key=lambda x: x[0])]
        if not targets:
            if "@" in text.replace("＠", "@"):
                self._emit({"kind": "note",
                            "text": "@的名字不是本群成员（本群成员：" + "、".join(all_names) +
                                    "）。话已记进群，但没有人被叫到。"})
            else:
                self._emit({"kind": "note",
                            "text": "没有@任何成员：话已记进群，他们下次发言时会看到"})
            self._touch_group(gkey, text)
            self._emit({"kind": "done"})
            return
        last_reply = ""
        # 接茬接力：先把用户@到的人按顺序说完，再按回复顺序逐条看最后一行的@，
        # 有效的排进队列接着说；最多接力3次，同一成员不重复叫起
        queue = list(targets)
        replies = []          # [(成员, 回复正文)] 按产生顺序
        scan_idx = 0
        relay_used = 0
        relayed = set()
        uname = group_user_name(sess)
        try:
            while True:
                if not queue:
                    while scan_idx < len(replies) and relay_used < 3:
                        spk, txt = replies[scan_idx]
                        scan_idx += 1
                        with SESS_LOCK:
                            g_scan = get_session(load_sessions(), gkey) or sess
                        tgt = relay_target(txt, g_scan.get("members", []), spk, uname)
                        if not tgt:
                            continue
                        tid = tgt.get("mid") or tgt["name"]
                        if tid in relayed:
                            continue
                        relayed.add(tid)
                        relay_used += 1
                        queue.append(dict(tgt))
                        self._emit({"kind": "note",
                                    "text": "「%s」点名「%s」接茬（接力 %d/3）"
                                            % (spk["name"], tgt["name"], relay_used)})
                        break
                    if not queue:
                        break
                m = queue.pop(0)
                # 每次发言前重读群条目：前面的成员（或用户在界面上）可能刚改了名/公告，
                # 后面的要看到最新的；自己的昵称也以盘上为准，别拿 /send 时的快照
                with SESS_LOCK:
                    d_now = load_sessions()
                    g_now = get_session(d_now, gkey) or sess
                    p_now = get_session(d_now, room["parent"]) if room else None
                    # 讨论室：成员真身在大厅群里，昵称和会话链以大厅为准
                    src = p_now if p_now else g_now
                for x in src.get("members", []):
                    if (m.get("mid") and x.get("mid") == m.get("mid")) or \
                            (not m.get("mid") and x.get("name") == m["name"]):
                        m["name"] = x["name"]
                        if room:
                            m["sid"] = x.get("sid") or ""
                        break
                if not room and m.get("mid"):
                    with SESS_LOCK:
                        busy_room = active_room_of(load_sessions(), gkey, m["mid"])
                    if busy_room:
                        gnote = ("「%s」正在分组讨论室「%s」里，这轮没叫到"
                                 % (m["name"], (busy_room.get("room") or {}).get("topic", "")))
                        append_group_msg(gkey, "system", "系统", gnote)
                        self._emit({"kind": "note", "text": gnote})
                        continue
                self._emit({"kind": "status", "text": "「" + m["name"] + "」正在回复"})
                fresh = not m.get("sid")
                run_m = dict(m)
                # 群级 effort 对本群所有成员统一；空=跟 CLI 默认。老群成员建群时冻结的
                # 成员级 effort 不再单独生效，否则状态栏显示「默认」实际却按旧值跑
                eff = (p_now or g_now).get("effort") or ""
                run_m["effort"] = eff if eff in EFFORTS else ""
                r = self._run_member(
                    run_m, self._member_prompt(g_now, m, images, fresh, parent=p_now), rid)
                if r["rc"] != 0 and not r["text"] and not r["stopped"] and not fresh:
                    # 旧会话链接不上：重开一条，把全量转录补给它，重试一次
                    m["sid"] = ""
                    run_m["sid"] = ""
                    self._emit({"kind": "note",
                                "text": "「" + m["name"] + "」的会话接不上了，已重开并补发群记录"})
                    r = self._run_member(
                        run_m, self._member_prompt(g_now, m, images, True, parent=p_now), rid)
                if r["rc"] != 0 and not r["text"] and not r["stopped"]:
                    etxt = ("「" + m["name"] + "」响应失败：" +
                            (r["err"] or ("claude退出码 %s" % r["rc"])))
                    self._emit({"kind": "error", "text": etxt})
                    append_group_msg(gkey, "system", "系统", etxt[:300])
                # 回复里的 [[改名:x]] / [[公告:x]] 标签：摘出来单独处理，不进转录正文
                new_name = new_notice = None
                if r["text"]:
                    r["text"], new_name, new_notice = extract_group_tags(r["text"])
                # 落账：按 mid 匹配（老群没 mid 的按名字）。这轮失败就不推进已读指针，
                # 没送达的消息下一轮还能补给他，群内认知不错位
                ok = bool(r["text"]) or (r["rc"] == 0 and not r["stopped"])
                notes = []
                with SESS_LOCK:
                    d = load_sessions()
                    g = get_session(d, gkey)
                    pg = get_session(d, room["parent"]) if room else None
                    if g:
                        for mm in g.get("members", []):
                            same = (mm.get("mid") == m.get("mid")) if m.get("mid") \
                                else (mm.get("name") == m["name"])
                            if not same:
                                continue
                            if r["sid"]:
                                mm["sid"] = r["sid"]
                                if pg:   # 讨论室：会话链的真身在大厅群里
                                    for px in pg.get("members", []):
                                        if px.get("mid") == mm.get("mid"):
                                            px["sid"] = r["sid"]
                            if m.get("_room_intro_due") and ok:
                                mm["room_intro"] = True
                            if m.get("_back_done") and ok:
                                mm.pop("back_from", None)
                            _, before = read_group_msgs(gkey, 0)
                            for tk in r.get("thinks") or []:
                                append_group_msg(gkey, "member", mm["name"],
                                                 tk, think=True, mid=mm.get("mid"))
                            if r["text"]:
                                append_group_msg(gkey, "member", mm["name"],
                                                 canonicalize_stickers(r["text"]),
                                                 mid=mm.get("mid"))
                            if ok:
                                _, tot = read_group_msgs(gkey, 0)
                                seen = m.get("_seen")
                                # 它生成期间若有人从界面写了公告/改名通知（落在它读过的
                                # 之后、它自己发言之前），指针停在那些通知前，下轮补看
                                mm["read"] = tot if (seen is None or before <= seen) else seen
                                mm["intro_v"] = INTRO_V   # 已经知道群规则（含接茬/分组讨论）
                            if new_name is not None and new_name == mm["name"]:
                                new_name = None   # 改成自己现在的名字：不算改名，不发通知
                            if new_name is not None:
                                err = check_nickname(g, new_name, mm.get("mid"))
                                if err:
                                    notes.append("「%s」想改名为「%s」，没成功：%s"
                                                 % (mm["name"], new_name, err))
                                else:
                                    notes.append("「%s」改名为「%s」" % (mm["name"], new_name))
                                    mm["name"] = new_name
                                    m["name"] = new_name
                                    if pg:   # 室内改名，大厅真身同步
                                        for px in pg.get("members", []):
                                            if px.get("mid") == mm.get("mid"):
                                                px["name"] = new_name
                            if new_notice is not None:
                                if (g.get("owner") or "user") == mm.get("mid"):
                                    g["notice"] = new_notice[:500]
                                    notes.append(
                                        ("群主「%s」更新了群公告：%s" % (mm["name"], g["notice"]))
                                        if g["notice"] else "群主「%s」清空了群公告" % mm["name"])
                                else:
                                    notes.append("「%s」不是群主，改公告没有生效" % mm["name"])
                            break
                        # 通知条落在成员自己的发言之后；它的已读指针停在通知之前，
                        # 下一轮从转录里看到结果，知道改名成没成
                        for nt in notes:
                            append_group_msg(gkey, "system", "系统", nt, tell=True)
                        save_sessions(d)
                for nt in notes:
                    self._emit({"kind": "note", "text": nt})
                if r["text"]:
                    last_reply = r["text"]
                    replies.append((dict(m), r["text"]))
                    bad_stk = unresolved_stickers(r["text"])
                    if bad_stk:
                        STK_WARN[gkey + "/" + (m.get("mid") or m["name"])] = bad_stk
                if r["stopped"]:
                    gnote = "已停止，后面的成员不再发言" if (len(targets) > 1 or queue) else "已停止"
                    append_group_msg(gkey, "system", "系统", gnote)
                    self._emit({"kind": "note", "text": gnote})
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
    # macOS/Linux 上 SO_REUSEADDR 不允许两个进程同时监听，只是让刚退出后能马上重开，
    # 所以留着。
    allow_reuse_address = not IS_WIN


def native_window():
    """macOS 原生窗口（native/ 目录编译出的 SoftshellWindow.app）。
    麦克风权限记在这个 App 名下，系统只问一次；没编译过就返回 None。"""
    if not IS_MAC:
        return None
    exe = os.path.join(DIR, "SoftshellWindow.app", "Contents", "MacOS", "SoftshellWindow")
    return exe if os.path.isfile(exe) else None


def open_window():
    url = "http://127.0.0.1:%d/" % PORT
    nw = native_window()
    if nw:
        subprocess.Popen([nw, url], **SPAWN)
    elif BROWSER:
        subprocess.Popen(
            [BROWSER, "--app=" + url, "--window-size=900,860"],
            **SPAWN,
        )
    else:
        # 没找到 Edge/Chrome：用系统默认浏览器，会带地址栏但功能一样
        webbrowser.open(url)


def main():
    ensure_sticker_dir()
    ensure_sessions()
    migrate_member_avatars()
    migrate_group_fields()
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
