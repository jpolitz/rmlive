// rmlive viewer as a Mac app: one window around a WKWebView showing the
// streamer's page. Built by ./run with swiftc (no Xcode project).
//
// The page talks to the app through window.webkit.messageHandlers.rmlive;
// in a plain browser that object is missing and the page falls back to web
// APIs. Add new native features as more message types in `userContentController`.
import AppKit
import WebKit

let defaultURL = "http://10.11.99.1:8099/"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var url: URL!

    func applicationDidFinishLaunching(_ note: Notification) {
        url = URL(string: UserDefaults.standard.string(forKey: "url") ?? defaultURL)!

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "rmlive")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.isInspectable = true // Safari > Develop > (this Mac) > rmlive

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "rmlive"
        window.contentView = web
        window.collectionBehavior = .fullScreenPrimary
        window.center()
        window.setFrameAutosaveName("main")
        window.makeKeyAndOrderFront(nil)

        buildMenu()
        load()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    // `open` on an already-running app lands here. ./run just restarted the
    // streamer, possibly with a new page, so reload rather than reconnect.
    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        load()
        return true
    }

    func load() { web.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)) }

    // The page retries its stream on its own, but if the page itself never
    // loaded (streamer not up yet) there's no page to do that, so retry here.
    func webView(_ w: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError err: Error) {
        window.title = "rmlive: waiting for \(url.host ?? "tablet")…"
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.load() }
    }

    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) { window.title = "rmlive" }

    func userContentController(_ c: WKUserContentController, didReceive msg: WKScriptMessage) {
        guard let body = msg.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "fullscreen": window.toggleFullScreen(nil)
        default: NSLog("rmlive: unknown message \(type)")
        }
    }

    @objc func reload(_ sender: Any?) { load() }

    func buildMenu() {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.submenu = NSMenu(title: title)
            items.forEach(item.submenu!.addItem)
            main.addItem(item)
        }
        func item(_ title: String, _ action: Selector, _ key: String, _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            return i
        }
        submenu("rmlive", [
            item("Hide rmlive", #selector(NSApplication.hide(_:)), "h"),
            .separator(),
            item("Quit rmlive", #selector(NSApplication.terminate(_:)), "q"),
        ])
        // Edit gives the panel's text fields copy/paste.
        submenu("Edit", [
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        submenu("View", [
            item("Reload", #selector(reload(_:)), "r"),
            item("Toggle Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
