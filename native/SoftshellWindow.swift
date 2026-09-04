// Softshell 软壳 · macOS 原生窗口
// 用系统 WebKit 开一个无地址栏的独立窗口加载聊天页，麦克风权限由本 App 自己持有，
// 系统只问一次就永久记住（不再走 Safari 那套每次都问的逻辑）。
import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    let url: URL

    init(url: URL) { self.url = url }

    func applicationDidFinishLaunching(_ note: Notification) {
        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        cfg.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: cfg)
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
