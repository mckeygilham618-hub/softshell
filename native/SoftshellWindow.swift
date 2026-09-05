// Softshell 软壳 · macOS 原生窗口
// 用系统 WebKit 开一个无地址栏的独立窗口加载聊天页，麦克风权限由本 App 自己持有，
// 系统只问一次就永久记住（不再走 Safari 那套每次都问的逻辑）。
import Cocoa
import WebKit
import Speech
import AVFoundation

// ── 系统语音识别（Speech.framework）──
// Mac 上不必再下 SenseVoice 模型：网页把已经做过回声消除的 PCM 递进来，
// 这里交给系统的本机离线识别，转写结果回吐给网页。
// 必须在 .app 包里跑且 Info.plist 带 NSSpeechRecognitionUsageDescription，
// 否则一调用就被 TCC 打死（裸进程会 SIGABRT）。
final class ASRBridge: NSObject, WKScriptMessageHandler {
    weak var webView: WKWebView?
    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var fmt: AVAudioFormat?
    private var running = false
    private var gen = 0                 // 任务代号：reset 之后旧任务的迟到回调要丢掉
    private var lastStart = Date.distantPast
    private var carry = ""              // 系统自己把上一轮收尾了：已认下的字要接着往下续
    private var lastText = ""           // 当前这一轮认到的字

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let d = m.body as? [String: Any], let cmd = d["cmd"] as? String else { return }
        switch cmd {
        case "start": start(locale: (d["locale"] as? String) ?? "zh-CN")
        case "feed":  feed(d["pcm"] as? String, d["sr"] as? Double)
        case "reset": if running { newTask(carryOver: false) }
        case "stop":  stop()
        default: break
        }
    }

    private func emit(_ obj: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              let json = String(data: data, encoding: .utf8) else { return }
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript("window.__asrEvent && window.__asrEvent(\(json))")
        }
    }

    private func start(locale: String) {
        if running { emit(["type": "ready"]); return }
        SFSpeechRecognizer.requestAuthorization { [weak self] st in
            guard let self = self else { return }
            guard st == .authorized else {
                self.emit(["type": "error",
                           "msg": "系统没给语音识别权限：到 系统设置 › 隐私与安全性 › 语音识别 里打开 Softshell"])
                return
            }
            guard let r = SFSpeechRecognizer(locale: Locale(identifier: locale)) else {
                self.emit(["type": "error", "msg": "系统不支持这个语种：\(locale)"])
                return
            }
            guard r.supportsOnDeviceRecognition else {
                // 不退回联网识别：软壳承诺整条语音链路离线，宁可报错让用户去装语料
                self.emit(["type": "error",
                           "msg": "这台机器还没装 \(locale) 的本机听写语料：到 系统设置 › 键盘 › 听写 里把语言加上（加完这里就能离线识别）"])
                return
            }
            r.queue = OperationQueue()   // 别投主队列：主线程要给窗口用
            // 授权回调在任意队列上，而 running/gen/request 都由主线程读写，统一回主线程改
            DispatchQueue.main.async {
                self.recognizer = r
                self.running = true
                self.newTask(carryOver: false)
                self.emit(["type": "ready"])
            }
        }
    }

    // 开一轮新的识别任务。一句说完（或被打断）就换一轮，转写从头开始，
    // 顺带绕开 SFSpeechRecognitionTask 本身的单轮时长上限。
    // carryOver=true：不是用户说完一句，是系统自己把这轮收尾了（到时长上限之类），
    // 已经认下的字要接着算同一句，否则用户中间一停顿，前半句就被后半句顶掉了。
    private func newTask(carryOver: Bool) {
        task?.cancel()
        request?.endAudio()
        if carryOver {
            carry = carry.isEmpty ? lastText
                  : (lastText.isEmpty ? carry : carry + " " + lastText)
        } else {
            carry = ""
        }
        lastText = ""
        gen += 1
        let my = gen
        lastStart = Date()
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.requiresOnDeviceRecognition = true
        req.taskHint = .dictation
        if #available(macOS 13.0, *) { req.addsPunctuation = true }
        request = req
        task = recognizer?.recognitionTask(with: req) { [weak self] res, err in
            guard let self = self else { return }
            let t = (res?.bestTranscription.formattedString) ?? ""
            let ended = (err != nil) || (res?.isFinal ?? false)
            // 状态一律回主线程改，识别回调本身在别的队列上
            DispatchQueue.main.async {
                guard my == self.gen else { return }
                if res != nil {
                    self.lastText = t
                    let full = self.carry.isEmpty ? t
                             : (t.isEmpty ? self.carry : self.carry + " " + t)
                    self.emit(["type": "text", "text": full])
                }
                // 任务自己结束了而通话还在：续一轮，接着上一轮的字往下认。
                // 加时间闸，免得它一直失败就在这里空转。
                if ended && self.running &&
                   Date().timeIntervalSince(self.lastStart) > 1.0 {
                    self.newTask(carryOver: true)
                }
            }
        }
    }

    private func feed(_ b64: String?, _ sr: Double?) {
        guard running, let b64 = b64, let sr = sr, sr > 0, let req = request,
              let data = Data(base64Encoded: b64) else { return }
        let n = data.count / 2
        if n == 0 { return }
        if fmt == nil || fmt!.sampleRate != sr {
            fmt = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                sampleRate: sr, channels: 1, interleaved: false)
        }
        guard let f = fmt,
              let buf = AVAudioPCMBuffer(pcmFormat: f, frameCapacity: AVAudioFrameCount(n)),
              let out = buf.floatChannelData?[0] else { return }
        buf.frameLength = AVAudioFrameCount(n)
        data.withUnsafeBytes { raw in
            let p = raw.bindMemory(to: Int16.self)
            for i in 0..<n { out[i] = Float(p[i]) / 32768.0 }
        }
        req.append(buf)
    }

    private func stop() {
        running = false
        carry = ""
        lastText = ""
        gen += 1
        task?.cancel(); task = nil
        request?.endAudio(); request = nil
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    let asr = ASRBridge()
    let url: URL

    init(url: URL) { self.url = url }

    func applicationDidFinishLaunching(_ note: Notification) {
        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        cfg.websiteDataStore = .default()
        // 系统语音识别的通道：网页发 PCM 进来，转写结果由 __asrEvent 回吐
        let ucc = WKUserContentController()
        ucc.add(asr, name: "asr")
        ucc.addUserScript(WKUserScript(source: "window.__asrNative = true;",
                                       injectionTime: .atDocumentStart,
                                       forMainFrameOnly: true))
        cfg.userContentController = ucc
        webView = WKWebView(frame: .zero, configuration: cfg)
        asr.webView = webView
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = false

        let rect = NSRect(x: 0, y: 0, width: 900, height: 860)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Softshell 软壳"
        window.contentView = webView
        window.delegate = self
        window.setFrameAutosaveName("SoftshellMain")
        window.minSize = NSSize(width: 480, height: 400)
        window.center()
        window.makeKeyAndOrderFront(nil)

        buildMenu()
        NSApp.activate(ignoringOtherApps: true)
        webView.load(URLRequest(url: url))
    }

    // 关窗口就退出（和以前浏览器 --app 窗口一个感觉）
    func windowWillClose(_ n: Notification) { NSApp.terminate(nil) }

    // ── 麦克风 / 摄像头：页面一请求就直接放行，系统级授权由 TCC 记住 ──
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(.grant)
    }

    // ── 文件选择（📎按钮）──
    func webView(_ webView: WKWebView, runOpenPanelWith p: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = p.allowsDirectories
        panel.allowsMultipleSelection = p.allowsMultipleSelection
        panel.beginSheetModal(for: window) { r in
            completionHandler(r == .OK ? panel.urls : nil)
        }
    }

    // ── target=_blank 的链接交给系统默认浏览器 ──
    func webView(_ webView: WKWebView, createWebViewWith cfg: WKWebViewConfiguration,
                 for nav: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = nav.request.url { NSWorkspace.shared.open(u) }
        return nil
    }

    // 页内跳到别的站点也交给系统浏览器，本窗口只留本机地址
    func webView(_ webView: WKWebView, decidePolicyFor nav: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = nav.request.url, let h = u.host,
           u.scheme?.hasPrefix("http") == true, h != url.host {
            NSWorkspace.shared.open(u)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // ── JS 弹窗 ──
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage msg: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = msg; a.runModal(); completionHandler()
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage msg: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = msg
        a.addButton(withTitle: "好"); a.addButton(withTitle: "取消")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert(); a.messageText = prompt
        let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        tf.stringValue = defaultText ?? ""
        a.accessoryView = tf
        a.addButton(withTitle: "好"); a.addButton(withTitle: "取消")
        completionHandler(a.runModal() == .alertFirstButtonReturn ? tf.stringValue : nil)
    }

    // 桥接没起来时给个提示，不要白屏
    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        let html = "<meta charset=utf-8><body style='font:15px -apple-system;padding:40px;color:#444'>" +
            "<h3>软壳的后台没响应</h3><p>请双击 Softshell.command 重新启动。</p>" +
            "<p style='color:#999;font-size:12px'>\(error.localizedDescription)</p>"
        webView.loadHTMLString(html, baseURL: nil)
    }

    // 最小菜单：让 ⌘C/⌘V/⌘Q 这些快捷键能用
    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "隐藏 Softshell", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 Softshell", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(NSMenuItem.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "显示")
        view.addItem(withTitle: "重新载入", action: #selector(WKWebView.reload(_:)), keyEquivalent: "r")
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = win
        NSApp.mainMenu = main
    }
}

let args = CommandLine.arguments
let target = URL(string: args.count > 1 ? args[1] : "http://127.0.0.1:8618/")!
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate(url: target)
app.delegate = delegate
app.run()
