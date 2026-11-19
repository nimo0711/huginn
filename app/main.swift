import AppKit
import WebKit
import UserNotifications

let boardURL = URL(string: "http://127.0.0.1:8790/")!
let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Huginn"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var retry: Timer?
    var badgeTimer: Timer?
    var asking: Set<String> = []
    var primed = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = appName
        window.backgroundColor = NSColor(srgbRed: 23/255, green: 23/255, blue: 20/255, alpha: 1)
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 420, height: 400)
        window.contentView = web
        window.setFrameAutosaveName("WorkBoardMain")
        if !window.setFrameUsingName("WorkBoardMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        load()
        badgeTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.updateBadge() }
        updateBadge()
    }

    func load() {
        web.load(URLRequest(url: boardURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5))
    }

    func scheduleRetry() {
        retry?.invalidate()
        let html = """
        <html><body style="font:16px -apple-system;color:#c4bcad;background:#171714;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
        \(appName) 서비스에 연결하는 중…</body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
        retry = Timer.scheduledTimer(withTimeInterval: 3, repeats: false) { [weak self] _ in self?.load() }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, navigationAction.navigationType == .linkActivated, url.host != boardURL.host {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        scheduleRetry()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        scheduleRetry()
    }

    func updateBadge() {
        var req = URLRequest(url: boardURL.appendingPathComponent("api/sessions"))
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let sessions = obj["sessions"] as? [[String: Any]] else { return }
            let waiting = sessions.filter { ($0["state"] as? String) == "입력 대기" }
            DispatchQueue.main.async {
                NSApp.dockTile.badgeLabel = waiting.isEmpty ? nil : String(waiting.count)
                self.announce(waiting)
            }
        }.resume()
    }

    func announce(_ waiting: [[String: Any]]) {
        let ids = Set(waiting.compactMap { $0["id"] as? String })
        let fresh = waiting.filter { !asking.contains(($0["id"] as? String) ?? "") }
        asking = ids
        if !primed { primed = true; return }
        for s in fresh {
            let name = (s["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "AI 세션"
            let wait = s["wait"] as? [String: Any] ?? [:]
            let kind = ["ask": "질문", "perm": "권한 승인 대기", "plan": "계획 승인 대기", "auto": "자동 모드 차단", "other": "응답 필요"][(wait["kind"] as? String) ?? ""] ?? "나를 기다림"
            let text = (wait["text"] as? String) ?? ""
            let content = UNMutableNotificationContent()
            content.title = "\(name) \(kind)"
            content.body = text.isEmpty ? "응답을 기다리고 있습니다" : text
            content.sound = .default
            content.userInfo = ["sid": (s["id"] as? String) ?? ""]
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
            NSSound(named: "Glass")?.play()
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let sid = (response.notification.request.content.userInfo["sid"] as? String) ?? ""
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if !sid.isEmpty {
            if web.url?.host == boardURL.host && web.url?.path == "/" {
                web.evaluateJavaScript("ui.sid = \(String(reflecting: sid)); ui.stage = null; ui.req = null; store.set(\"wb.sid\", ui.sid); render();", completionHandler: nil)
            } else {
                var parts = URLComponents(url: boardURL, resolvingAgainstBaseURL: false)!
                parts.queryItems = [URLQueryItem(name: "sid", value: sid)]
                web.load(URLRequest(url: parts.url!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 5))
            }
        }
        completionHandler()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    @objc func reload(_ sender: Any?) { load() }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "\(appName) 숨기기", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "\(appName) 종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let viewMenu = NSMenu(title: "보기")
        let reloadItem = NSMenuItem(title: "새로 고침", action: #selector(reload(_:)), keyEquivalent: "r")
        reloadItem.target = self
        viewMenu.addItem(reloadItem)
        viewItem.submenu = viewMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "편집")
        editMenu.addItem(withTitle: "복사", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "전체 선택", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu

        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let windowMenu = NSMenu(title: "윈도우")
        windowMenu.addItem(withTitle: "닫기", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowMenu.addItem(withTitle: "최소화", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        windowItem.submenu = windowMenu
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
