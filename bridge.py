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
SESSION_FILE = os.path.join(DIR, "session.txt")
UPLOAD_DIR = os.path.join(DIR, "uploads")
STICKER_DIR = os.path.join(DIR, "stickers")
NO_WINDOW = subprocess.CREATE_NO_WINDOW

IMG_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

# 每段新对话的第一条消息带上这句。不带的话，新会话里的Claude不知道自己也能发表情包。
# 这里故意不列出有哪些表情包：判断权交给模型，它不想发表情包就不必花token去翻文件夹。
INTRO = (
    "(这是「Softshell」窗口。" + STICKER_DIR + " 里有表情包，如果你认为需要发表情包，"
    "可以去翻看那个文件夹，以写 [[表情:xxx]] 来输出表情包给用户，"
    "xxx 是图片的文件名（带不带后缀都认）。直接打 Unicode emoji 也可以。"
    "如果文件夹里没有你想要的那个表情，你可以用文字写出自己的表情和动作，像RP那样；"
    "也可以直接跟用户说，能不能在表情包文件夹里给你加一个XX表情。)"
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


def load_sid():
    try:
        s = open(SESSION_FILE, encoding="utf-8-sig").read()
    except OSError:
        return None
    s = s.replace("\ufeff", "").strip()
    # 只接受像会话ID的内容，防止文件被别的编辑器塞进隐形字符
    if re.match(r"^[0-9a-fA-F-]{8,}$", s):
        return s
    return None


def save_sid(sid):
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            f.write(sid or "")
    except OSError:
        pass


def ensure_sticker_dir():
    """确保表情包文件夹存在。用户可能只下载了脚本、没克隆整个仓库，
    缺了这个文件夹，表情面板会是一片空白，连提示都看不到。"""
    d = os.path.join(STICKER_DIR, "用户专用")
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
    """把 用户专用/开心.png 这样的相对路径解成绝对路径。
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


def translate(obj):
    """把CLI的stream-json事件翻译成前端认识的简化事件"""
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        sid = obj.get("session_id")
        if sid:
            save_sid(sid)
        yield {"kind": "session", "id": sid}
    elif t == "assistant":
        for block in obj.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text" and block.get("text"):
                yield {"kind": "text", "text": block["text"]}
            elif bt == "thinking" and block.get("thinking"):
                yield {"kind": "thinking", "text": block["thinking"]}
            elif bt == "tool_use":
                yield {"kind": "tool", "name": block.get("name", "?")}
    elif t == "result":
        sid = obj.get("session_id")
        if sid:
            save_sid(sid)
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

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(DIR, "index.html"), "rb") as f:
                    self._send_bytes(f.read(), "text/html; charset=utf-8")
            except OSError:
                self.send_error(404)
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
        self.send_error(404)

    def do_POST(self):
        if self.path == "/stop":
            n = int(self.headers.get("Content-Length", 0))
            rid = ""
            try:
                rid = str(json.loads(self.rfile.read(n).decode("utf-8")).get("rid", ""))
            except (ValueError, UnicodeDecodeError):
                pass
            killed = 0
            with ACTIVE_LOCK:
                targets = [rid] if rid and rid in ACTIVE else list(ACTIVE.keys())
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
        if self.path == "/new":
            save_sid("")
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
        images = [p for p in (payload.get("images") or []) if isinstance(p, str)]
        n_stickers = 0
        for rel in (payload.get("stickers") or []):
            p = sticker_path(rel)
            if p:
                images.append(p)
                n_stickers += 1
        if not text and not images:
            self.send_error(400)
            return

        sid = load_sid()
        if images:
            what = "表情包" if n_stickers == len(images) else "图片"
            text = (text or ("我发了一个" + what + "。")) + \
                "\n\n(我发了" + what + "，请先用Read工具查看这些文件再回答: " + " ; ".join(images) + ")"
        if not sid:
            text += "\n\n" + INTRO

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

        base = [CLAUDE, "-p", text, "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions"]
        attempt = 0
        while True:
            attempt += 1
            cmd = list(base)
            if sid:
                cmd += ["--resume", sid]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=HOME, encoding="utf-8", errors="replace",
                creationflags=NO_WINDOW,
            )
            with ACTIVE_LOCK:
                ACTIVE[rid] = {"proc": proc, "stopped": False}
            err_buf = []
            drainer = threading.Thread(
                target=lambda: err_buf.append(proc.stderr.read()), daemon=True
            )
            drainer.start()
            emitted = False
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
                self._emit({"kind": "note", "text": "已停止"})
                self._emit({"kind": "done"})
                return
            if proc.returncode != 0 and not emitted:
                if sid and attempt == 1:
                    # 旧会话接不上（被清理/ID损坏）：自动开新对话重试一次
                    sid = None
                    save_sid("")
                    self._emit({"kind": "note", "text": "旧会话接不上了，已自动开新对话"})
                    continue
                err = ("".join(err_buf)).strip()[-800:]
                self._emit({"kind": "error", "text": err or ("claude退出码 %s" % proc.returncode)})
            self._emit({"kind": "done"})
            return

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

    def _emit(self, obj):
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()


class SingleInstanceServer(ThreadingHTTPServer):
    # Windows 默认允许多进程绑同一端口，会导致每次双击都起一个新服务。
    # 关掉端口复用，第二次绑定才会失败，防重复启动的逻辑才生效。
    allow_reuse_address = False


def open_window():
    url = "http://127.0.0.1:%d/" % PORT
    if BROWSER:
        subprocess.Popen(
            [BROWSER, "--app=" + url, "--window-size=520,860"],
            creationflags=NO_WINDOW,
        )
    else:
        # 没找到 Edge/Chrome：用系统默认浏览器，会带地址栏但功能一样
        webbrowser.open(url)


def main():
    ensure_sticker_dir()
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
