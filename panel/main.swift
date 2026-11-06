// Floating, always-on-top skills panel. Reruns tracker.py every minute and
// pushes stats.json into panel/index.html.
import Cocoa
import CoreAudio
import WebKit

let env = ProcessInfo.processInfo.environment
let home = URL(fileURLWithPath: env["CLAUDE_SKILLS_HOME"]
    ?? URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        .deletingLastPathComponent().deletingLastPathComponent().path)
let python = env["CLAUDE_SKILLS_PYTHON"] ?? "/usr/bin/python3"
let dataDir = URL(fileURLWithPath: env["CLAUDE_SKILLS_DATA"]
    ?? NSHomeDirectory() + "/Library/Application Support/ClaudeSkills")
let sampleSeconds: Double = 15
let idleLimit = Double(env["CLAUDE_SKILLS_IDLE_LIMIT"] ?? "60") ?? 60
// A call (Meet, Zoom, Teams, a huddle) = some app holding a microphone this long; dictation is shorter.
let meetingAfter = Double(env["CLAUDE_SKILLS_MEETING_AFTER"] ?? "120") ?? 120
let refreshSeconds = Double(env["CLAUDE_SKILLS_REFRESH"] ?? "60") ?? 60
let scale = CGFloat(Double(env["CLAUDE_SKILLS_SCALE"] ?? "1.5") ?? 1.5)
let width: CGFloat = 230 * scale

final class Panel: NSPanel {
    override var canBecomeKey: Bool { true }
}

/// Drag the window by its title strip; everything else goes to the page.
final class DragWebView: WKWebView {
    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        if p.y < 22 * scale && p.x < 70 * scale { window?.performDrag(with: event) } else { super.mouseDown(with: event) }
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
        config.mediaTypesRequiringUserActionForPlayback = []  // level-up and chest sounds
        config.userContentController.add(self, name: "panel")
        web = DragWebView(frame: NSRect(origin: .zero, size: frame.size), configuration: config)
        web.setValue(false, forKey: "drawsBackground")
        web.pageZoom = scale
        web.autoresizingMask = [.width, .height]
        panel.contentView = web
        let page = home.appendingPathComponent("panel/index.html")
        web.loadFileURL(page, allowingReadAccessTo: home)
        keepOnScreen()
        panel.orderFrontRegardless()

        Timer.scheduledTimer(withTimeInterval: refreshSeconds, repeats: true) { _ in self.refresh() }
        Timer.scheduledTimer(withTimeInterval: sampleSeconds, repeats: true) { _ in self.sampleFrontApp() }
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
        case "action":  // open a chest, use an item, equip, claim, buy: appended, then the tracker recounts
            if var act = body["data"] as? [String: Any] {
                act["t"] = Date().timeIntervalSince1970
                appendLine(act, to: "actions.jsonl")
                refresh(force: true)
            }
        case "hop":  // level-up or new chest: the window jumps
            if let hs = body["heights"] as? [NSNumber] { hop(hs.map { CGFloat($0.doubleValue) }, base: hopTimer == nil ? panel.frame.origin : hopBase) }
        case "sound":  // fallback when the page may not play audio
            if let name = body["name"] as? String {
                NSLog("ClaudeSkills: page audio blocked, playing system sound \(name)")
                NSSound(named: NSSound.Name(name))?.play()
            }
        case "size":
            if let h = body["height"] as? Double { resize(CGFloat(h) * scale) }
        default: break
        }
    }

    var hopTimer: Timer?
    var hopBase = NSPoint.zero
    /// Bounce the window: one parabolic jump per height, about 0.3 s each, driven at 60 fps.
    func hop(_ heights: [CGFloat], base: NSPoint) {
        hopTimer?.invalidate()
        hopBase = base
        let jump = 0.3, start = Date()
        hopTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60, repeats: true) { t in
            let elapsed = Date().timeIntervalSince(start)
            let i = Int(elapsed / jump)
            guard i < heights.count else { self.panel.setFrameOrigin(base); t.invalidate(); self.hopTimer = nil; return }
            let x = (elapsed - Double(i) * jump) / jump        // 0...1 within this jump
            let y = heights[i] * CGFloat(4 * x * (1 - x))     // parabola: up, then down
            self.panel.setFrameOrigin(NSPoint(x: base.x, y: base.y + y))
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

    func appendLine(_ obj: [String: Any], to name: String) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        let url = dataDir.appendingPathComponent(name)
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
        guard let fh = try? FileHandle(forWritingTo: url) else { return }
        defer { try? fh.close() }
        _ = try? fh.seekToEnd()
        try? fh.write(contentsOf: data + Data("\n".utf8))
    }

    var pending = false
    var demoRan = false
    func refresh(force: Bool = false) {
        if force && running != nil { pending = true; return }  // run again as soon as this one ends
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
            DispatchQueue.main.async {
                if self.running === proc { self.running = nil }
                self.push()
                if self.pending { self.pending = false; self.refresh() }
            }
        }
        do { try p.run(); running = p; startedAt = Date() } catch { NSLog("ClaudeSkills: cannot start \(python): \(error)") }
    }

    /// One line per 15s in activity.jsonl. In a call you often touch nothing, so a microphone held
    /// for over meetingAfter seconds is logged as "meeting" (with the minutes before it, once);
    /// otherwise the front app, only if you touched keyboard or mouse in the last minute.
    var micSince: Date?
    var micCredited = false
    func sampleFrontApp() {
        let now = Date()
        if micInUse() {
            micSince = micSince ?? now
            let held = now.timeIntervalSince(micSince!)
            if held >= meetingAfter {
                append(app: "meeting", seconds: micCredited ? sampleSeconds : held, at: now)
                micCredited = true
                return
            }
        } else {
            micSince = nil
            micCredited = false
        }
        let anyInput = CGEventType(rawValue: ~0)!
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: anyInput)
        guard idle < idleLimit, let app = NSWorkspace.shared.frontmostApplication?.bundleIdentifier else { return }
        append(app: app, seconds: sampleSeconds, at: now)
    }

    func append(app: String, seconds: Double, at t: Date) {
        let line = "{\"t\":\(Int(t.timeIntervalSince1970)),\"app\":\"\(app)\",\"s\":\(Int(seconds))}\n"
        let url = dataDir.appendingPathComponent("activity.jsonl")
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
        guard let fh = try? FileHandle(forWritingTo: url) else { return }
        defer { try? fh.close() }
        _ = try? fh.seekToEnd()
        try? fh.write(contentsOf: Data(line.utf8))
    }

    /// True while any app records from any input device (built-in mic, AirPods, a USB headset).
    func micInUse() -> Bool {
        let system = AudioObjectID(kAudioObjectSystemObject)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                              mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr else { return false }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return false }
        return ids.contains { id in
            var streams = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                                     mScope: kAudioObjectPropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
            var n: UInt32 = 0
            guard AudioObjectGetPropertyDataSize(id, &streams, 0, nil, &n) == noErr, n > 0 else { return false }
            var running = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
                                                     mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            var on: UInt32 = 0
            var sz = UInt32(MemoryLayout<UInt32>.size)
            return AudioObjectGetPropertyData(id, &running, 0, nil, &sz, &on) == noErr && on != 0
        }
    }

    func push() {
        guard let data = try? Data(contentsOf: dataDir.appendingPathComponent("stats.json")),
              let json = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.render && window.render(\(json))")
        if let js = env["CLAUDE_SKILLS_DEMO_JS"], !demoRan {  // dev only: script a click-through for screenshots
            demoRan = true
            web.evaluateJavaScript(js)
        }
        if let key = env["CLAUDE_SKILLS_HOVER"] {  // dev: show one hover state, e.g. "skill:afk"
            web.evaluateJavaScript("window.showInfo('\(key)')")
        }
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)  // no Dock icon, no menu bar
app.run()
