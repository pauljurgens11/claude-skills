// Floating, always-on-top skills panel. Reruns tracker.py every minute and
// pushes stats.json into panel/index.html.
import Cocoa
import WebKit

let env = ProcessInfo.processInfo.environment
let home = URL(fileURLWithPath: env["CLAUDE_SKILLS_HOME"]
    ?? URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        .deletingLastPathComponent().deletingLastPathComponent().path)
let python = env["CLAUDE_SKILLS_PYTHON"] ?? "/usr/bin/python3"
let dataDir = URL(fileURLWithPath: env["CLAUDE_SKILLS_DATA"]
    ?? NSHomeDirectory() + "/Library/Application Support/ClaudeSkills")
let refreshSeconds = Double(env["CLAUDE_SKILLS_REFRESH"] ?? "60") ?? 60
let width: CGFloat = 230

final class Panel: NSPanel {
    override var canBecomeKey: Bool { true }
}

/// Drag the window by its title strip; everything else goes to the page.
final class DragWebView: WKWebView {
    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        if p.y < 22 && p.x < bounds.width - 34 { window?.performDrag(with: event) } else { super.mouseDown(with: event) }
    }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

final class App: NSObject, NSApplicationDelegate, WKScriptMessageHandler {
    var panel: Panel!
    var web: DragWebView!
    var running: Process?
    var startedAt = Date()

    func applicationDidFinishLaunching(_ note: Notification) {
        let screen = NSScreen.main!.visibleFrame
        let frame = NSRect(x: screen.maxX - width - 12, y: screen.maxY - 320 - 12, width: width, height: 320)
        panel = Panel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.acceptsMouseMovedEvents = true
        panel.setFrameAutosaveName("ClaudeSkillsPanel")

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "panel")
        web = DragWebView(frame: NSRect(origin: .zero, size: frame.size), configuration: config)
        web.setValue(false, forKey: "drawsBackground")
        web.autoresizingMask = [.width, .height]
        panel.contentView = web
        let page = home.appendingPathComponent("panel/index.html")
        web.loadFileURL(page, allowingReadAccessTo: home)
        keepOnScreen()
        panel.orderFrontRegardless()

        Timer.scheduledTimer(withTimeInterval: refreshSeconds, repeats: true) { _ in self.refresh() }
        let nc = NotificationCenter.default, ws = NSWorkspace.shared.notificationCenter
        nc.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { _ in self.keepOnScreen() }
        ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in self.refresh() }
        ws.addObserver(forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main) { _ in self.panel.orderFrontRegardless() }
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "ready":
            push()      // show the last numbers at once,
            refresh()   // then catch up on new transcript lines
        case "size":
            if let h = body["height"] as? Double { resize(CGFloat(h)) }
        default: break
        }
    }

    func resize(_ h: CGFloat) {
        var f = panel.frame
        let top = f.maxY
        f.size.height = h
        f.origin.y = top - h  // grow downward, keep the top edge put
        panel.setFrame(f, display: true)
    }

    /// A monitor was unplugged or rearranged: never leave the panel where nobody can see it.
    func keepOnScreen() {
        let f = panel.frame
        if NSScreen.screens.contains(where: { $0.visibleFrame.intersects(f.insetBy(dx: 40, dy: 40)) }) { return }
        let s = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        panel.setFrameOrigin(NSPoint(x: s.maxX - f.width - 12, y: s.maxY - f.height - 12))
    }

    func refresh() {
        if let p = running {
            if Date().timeIntervalSince(startedAt) < 300 { return }
            NSLog("ClaudeSkills: tracker hung for 5 min, killing it")
            p.terminate()
            running = nil
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [home.appendingPathComponent("tracker.py").path]
        p.terminationHandler = { proc in
            if proc.terminationStatus != 0 { NSLog("ClaudeSkills: tracker exited \(proc.terminationStatus)") }
            DispatchQueue.main.async { if self.running === proc { self.running = nil }; self.push() }
        }
        do { try p.run(); running = p; startedAt = Date() } catch { NSLog("ClaudeSkills: cannot start \(python): \(error)") }
    }

    func push() {
        guard let data = try? Data(contentsOf: dataDir.appendingPathComponent("stats.json")),
              let json = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.render && window.render(\(json))")
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)  // no Dock icon, no menu bar
app.run()
