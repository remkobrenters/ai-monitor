// macOS menu bar front end. It talks to the local service through `ai-monitor watch`,
// the same JSON bridge the GNOME extension uses, and never simulates keystrokes.
import AppKit
import CoreServices
import SwiftUI
import UserNotifications

let DEFAULTS: [String: Any] = ["sound-enabled": false, "sound-file": "", "sound-effect": 1, "sound-repeat": "once",
                               "sound-volume": 70, "notify-ready": true, "notifications-enabled": true,
                               "pulse-enabled": true, "show-percent": true, "show-token-counts": true, "show-cache-info": false]
let REPOSITORY = URL(string: "https://github.com/remkobrenters/ai-monitor")!

extension Color {
    init(hex: String) { self.init(nsColor: NSColor(hex: hex)) }
}

extension NSColor {
    convenience init(hex: String) {
        let value = Int(hex.dropFirst(), radix: 16) ?? 0
        self.init(srgbRed: CGFloat(value >> 16 & 255) / 255, green: CGFloat(value >> 8 & 255) / 255,
                  blue: CGFloat(value & 255) / 255, alpha: 1)
    }
}

// MARK: - Terminal apps

/// Terminals whose AppleScript dictionaries expose each tab's tty, so a tab can be selected exactly.
struct TabScripts {
    let select: (String) -> String
    let visible: String
}

func validTty(_ tty: String?) -> String? {
    guard let tty, tty.range(of: #"^/dev/ttys[0-9]{1,6}$"#, options: .regularExpression) != nil else { return nil }
    return tty
}

let EXACT_TABS: [String: TabScripts] = [
    "com.apple.Terminal": TabScripts(select: { tty in """
        with timeout of 3 seconds
            tell application id "com.apple.Terminal"
                repeat with w in windows
                    repeat with t in tabs of w
                        if tty of t is "\(tty)" then
                            set selected of t to true
                            set index of w to 1
                            activate
                            return true
                        end if
                    end repeat
                end repeat
            end tell
        end timeout
        return false
        """ }, visible: """
        with timeout of 2 seconds
            tell application id "com.apple.Terminal" to return tty of selected tab of front window
        end timeout
        """),
    "com.googlecode.iterm2": TabScripts(select: { tty in """
        with timeout of 3 seconds
            tell application id "com.googlecode.iterm2"
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if tty of s is "\(tty)" then
                                select w
                                select t
                                select s
                                activate
                                return true
                            end if
                        end repeat
                    end repeat
                end repeat
            end tell
        end timeout
        return false
        """ }, visible: """
        with timeout of 2 seconds
            tell application id "com.googlecode.iterm2" to return tty of current session of current window
        end timeout
        """),
]

/// Whether the user already allowed AI Monitor to script this app. Never prompts.
func automationAllowed(_ bundle: String) -> Bool {
    let target = NSAppleEventDescriptor(bundleIdentifier: bundle)
    guard let address = target.aeDesc else { return false }
    return AEDeterminePermissionToAutomateTarget(address, typeWildCard, typeWildCard, false) == noErr
}

// MARK: - Bridge to `ai-monitor watch`

final class Bridge {
    var onLine: (Data) -> Void = { _ in }
    var onExit: () -> Void = {}
    private var process: Process?
    private var input: FileHandle?
    private var buffer = Data()
    private let writer = DispatchQueue(label: "ai-monitor.bridge")

    var running: Bool { process?.isRunning == true }

    func start() {
        guard let python = Bundle.main.object(forInfoDictionaryKey: "AIMonitorPython") as? String,
              let entry = Bundle.main.object(forInfoDictionaryKey: "AIMonitorEntry") as? String else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [entry, "watch"]
        let stdin = Pipe(), stdout = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = FileHandle.nullDevice
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil }
            DispatchQueue.main.async { self?.receive(data, from: process) }
        }
        process.terminationHandler = { [weak self] ended in
            DispatchQueue.main.async {
                guard let self, self.process === ended else { return }
                self.process = nil
                self.input = nil
                self.onExit()
            }
        }
        do { try process.run() } catch { return }
        self.process = process
        input = stdin.fileHandleForWriting
        buffer = Data()
    }

    private func receive(_ data: Data, from source: Process) {
        guard process === source else { return }
        buffer.append(data)
        while let end = buffer.firstIndex(of: 10) {
            let line = buffer[buffer.startIndex..<end]
            buffer.removeSubrange(buffer.startIndex...end)
            if !line.isEmpty { onLine(Data(line)) }
        }
        if buffer.count > 4 * 1024 * 1024 { stop() }
    }

    func send(_ message: [String: Any]) {
        guard let input, running, let data = try? JSONSerialization.data(withJSONObject: message) else { return }
        writer.async { try? input.write(contentsOf: data + Data([10])) }
    }

    func stop() { process?.terminate() }
}

// MARK: - Sound

final class SoundPlayer: NSObject, NSSoundDelegate {
    private var sound: NSSound?
    private var timer: Timer?
    private var last = -Double.infinity
    private let error: (String) -> Void

    init(error: @escaping (String) -> Void) { self.error = error }

    static func path() -> String {
        let defaults = UserDefaults.standard
        let custom = defaults.string(forKey: "sound-file") ?? ""
        if !custom.isEmpty { return custom }
        let index = defaults.integer(forKey: "sound-effect") - 1
        let effect = SOUND_EFFECTS.indices.contains(index) ? SOUND_EFFECTS[index] : SOUND_EFFECTS[0]
        return Bundle.main.resourceURL?.appendingPathComponent("sounds/\(effect.file).mp3").path ?? ""
    }

    func play(preview: Bool = false) {
        let defaults = UserDefaults.standard
        let volume = defaults.integer(forKey: "sound-volume")
        if (!preview && !defaults.bool(forKey: "sound-enabled")) || volume <= 0 { return }
        let now = ProcessInfo.processInfo.systemUptime
        if !preview && now - last < 3 { return }
        last = now
        let path = SoundPlayer.path()
        var directory: ObjCBool = false
        guard path.hasPrefix("/"), FileManager.default.fileExists(atPath: path, isDirectory: &directory),
              !directory.boolValue, FileManager.default.isReadableFile(atPath: path) else {
            error("Choose a readable local sound file in Settings.")
            return
        }
        stop()
        guard let sound = NSSound(contentsOf: URL(fileURLWithPath: path), byReference: true) else {
            error("Could not play the selected sound. Check its format and output device.")
            return
        }
        sound.volume = Float(min(volume, 100)) / 100
        sound.delegate = self
        self.sound = sound
        guard sound.play() else {
            self.sound = nil
            error("Could not play the selected sound. Check its format and output device.")
            return
        }
        let timer = Timer(timeInterval: 15, repeats: false) { [weak self] _ in self?.stop() }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        sound?.stop()
        sound = nil
    }

    func sound(_ sound: NSSound, didFinishPlaying _: Bool) {
        guard sound === self.sound else { return }
        self.sound = nil
        timer?.invalidate()
        timer = nil
    }
}

// MARK: - Dashboard

final class Model: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var visibleTools: [String] = ["codex"]
}

struct Actions {
    var focus: (String) -> Void
    var acknowledge: () -> Void
    var toggleSilent: () -> Void
    var settings: () -> Void
}

struct Meter: View {
    var fraction: Double
    var color: String
    var stale: Bool
    var height: CGFloat = 9

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.gray.opacity(0.2))
                if fraction > 0 {
                    Capsule().fill(Color(hex: color).opacity(stale ? 0.5 : 1))
                        .frame(width: max(height, min(1, fraction) * geometry.size.width))
                }
            }
        }
        .frame(height: height)
    }
}

struct HeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

struct HoverRow<Content: View>: View {
    var tint: Color = .clear
    @ViewBuilder var content: Content
    @State private var hovering = false

    var body: some View {
        content
            .background(RoundedRectangle(cornerRadius: 7).fill(hovering ? Color.gray.opacity(0.16) : tint))
            .onHover { hovering = $0 }
    }
}

let PERIODS = [("today", "Today"), ("week", "Week"), ("month", "Month"), ("ytd", "YTD")]
let ATTENTION = Color(hex: Palette.attention)

struct DashboardView: View {
    @ObservedObject var model: Model
    var actions: Actions
    var maxHeight: () -> CGFloat
    @AppStorage("notify-ready") private var notifyReady = true
    @AppStorage("show-token-counts") private var showTokenCounts = true
    @AppStorage("show-cache-info") private var showCacheInfo = false
    @AppStorage("sound-enabled") private var soundEnabled = false
    @State private var contentHeight: CGFloat = 0

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                content
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
                    .background(GeometryReader { Color.clear.preference(key: HeightKey.self, value: $0.size.height) })
            }
            .frame(height: min(max(contentHeight, 60), maxHeight()))
            .onPreferenceChange(HeightKey.self) { contentHeight = $0 }
            Divider()
            footer
        }
        .frame(width: 500)
    }

    private var content: some View {
        let sessions = model.snapshot?.sessions ?? []
        let working = sessions.filter { $0.state == "working" }.count
        let waiting = sessions.filter { attention($0, notifyReady: notifyReady) }.count
        return VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("AI monitor").font(.system(size: 15, weight: .bold))
                Spacer()
                Text("\(working) working\(waiting > 0 ? " · \(waiting) waiting" : "")")
                    .font(.callout).foregroundStyle(waiting > 0 ? ATTENTION : .secondary)
            }
            if let snapshot = model.snapshot {
                HStack(alignment: .top, spacing: 12) {
                    ForEach(model.visibleTools, id: \.self) { tool in
                        QuotaCard(tool: tool, limits: snapshot.usage.limits?[tool] ?? nil)
                    }
                }
                Divider()
                sessionList(sessions)
                if showTokenCounts {
                    Divider()
                    tokenTable(snapshot.usage)
                }
                if let error = snapshot.collectorError, !error.isEmpty {
                    Text(error).font(.callout).foregroundStyle(.secondary)
                }
            } else {
                Text("Service unavailable. Run ./status.sh to diagnose.").foregroundStyle(.secondary)
            }
        }
    }

    private func sessionList(_ sessions: [Session]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Sessions").font(.headline)
            ForEach(Array(model.visibleTools.enumerated()), id: \.element) { index, tool in
                let own = sessions.filter { $0.tool == tool }
                Text(TITLES[tool] ?? tool).font(.callout.bold()).padding(.top, index > 0 ? 14 : 4)
                if own.isEmpty {
                    Text("No active sessions").font(.callout).foregroundStyle(.secondary)
                }
                ForEach(own) { row in sessionButton(row) }
            }
        }
    }

    private func sessionButton(_ row: Session) -> some View {
        let waiting = attention(row, notifyReady: notifyReady)
        let status = sessionStatus(row)
        return Button { actions.focus(row.id) } label: {
            HoverRow(tint: waiting ? Color(.sRGB, red: 160 / 255, green: 110 / 255, blue: 220 / 255, opacity: 0.08) : .clear) {
                HStack(alignment: .top, spacing: 18) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(row.title).fontWeight(.medium).lineLimit(1)
                        if let project = row.project, !project.isEmpty {
                            Text(project).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 0)
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(status).font(.callout).foregroundStyle(waiting ? ATTENTION : .primary)
                            .multilineTextAlignment(.trailing).frame(maxWidth: 200, alignment: .trailing)
                        Text(duration(Date().timeIntervalSince1970 - row.since)).font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(.horizontal, 9)
                .padding(.vertical, 7)
                .contentShape(Rectangle())
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(TITLES[row.tool] ?? row.tool): \(row.title), \(status)")
    }

    private func tokenTable(_ usage: Usage) -> some View {
        let hasUsage = !(usage.tokens ?? [:]).isEmpty
        let values = { (period: String) in TOOLS.map { usage.tokens?[$0]?[period] } }
        var rows: [(String, [String], Bool)] = model.visibleTools.map { tool in
            (TITLES[tool] ?? tool, PERIODS.map { hasUsage ? formatTokens(tokenTotal(usage.tokens?[tool]?[$0.0])) : "—" }, false)
        }
        rows.append(("Total", PERIODS.map { period in
            hasUsage ? formatTokens(values(period.0).reduce(0) { $0 + tokenTotal($1) }) : "—" }, true))
        if showCacheInfo {
            rows.append(("Cached", PERIODS.map { period in
                hasUsage ? formatTokens(values(period.0).reduce(0) { $0 + ($1?.cached ?? 0) }) : "—" }, false))
            rows.append(("Cache hit", PERIODS.map { period in
                cacheShare(values(period.0)).map { String(format: "%.1f%%", $0) } ?? "—" }, false))
        }
        return VStack(alignment: .leading, spacing: 2) {
            Text("Tokens").font(.headline).padding(.bottom, 6)
            tableRow("", PERIODS.map(\.1), bold: false, heading: true)
            ForEach(rows, id: \.0) { row in
                HoverRow { tableRow(row.0, row.1, bold: row.2, heading: false) }
            }
            ForEach(model.visibleTools, id: \.self) { tool in
                if let error = usage.errors?[tool], !error.isEmpty {
                    Text(error).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func tableRow(_ title: String, _ cells: [String], bold: Bool, heading: Bool) -> some View {
        HStack(spacing: 0) {
            Text(title).fontWeight(bold ? .bold : .regular)
            Spacer(minLength: 8)
            ForEach(Array(cells.enumerated()), id: \.offset) { _, value in
                Text(value).fontWeight(bold ? .bold : .regular).monospacedDigit()
                    .frame(width: 78, alignment: .trailing)
            }
        }
        .font(heading ? .callout : .body)
        .foregroundStyle(heading ? .secondary : .primary)
        .padding(.horizontal, 6)
        .padding(.vertical, 4)
    }

    private var footer: some View {
        let hasWaiting = model.snapshot?.sessions.contains { $0.attention } == true
        return HStack(spacing: 8) {
            Button(action: actions.toggleSilent) {
                Label("Silent mode", systemImage: soundEnabled ? "speaker.wave.2.fill" : "speaker.slash.fill")
            }
            .buttonStyle(FooterButtonStyle(checked: !soundEnabled))
            .accessibilityLabel("Silent mode")
            Button("Acknowledge agents", action: actions.acknowledge)
                .buttonStyle(FooterButtonStyle(checked: false))
                .disabled(!hasWaiting)
                .opacity(hasWaiting ? 1 : 0.5)
            Spacer()
            Button(action: actions.settings) { Image(systemName: "gearshape") }
                .buttonStyle(FooterButtonStyle(checked: false))
                .accessibilityLabel("Settings")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
    }
}

struct FooterButtonStyle: ButtonStyle {
    var checked: Bool

    func makeBody(configuration: Configuration) -> some View {
        HoverRow(tint: checked || configuration.isPressed ? Color.gray.opacity(0.16) : .clear) {
            configuration.label.padding(.horizontal, 8).padding(.vertical, 6).contentShape(Rectangle())
        }
    }
}

struct QuotaCard: View {
    var tool: String
    var limits: Limits?

    var body: some View {
        let now = Date().timeIntervalSince1970
        let windows = (limits?.windows ?? []).filter { $0.usedPercent?.isFinite == true }
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(TITLES[tool] ?? tool).font(.system(size: 14, weight: .bold))
                Spacer()
                if let plan = limits?.plan, !plan.isEmpty { Text(plan).font(.caption).foregroundStyle(.secondary) }
            }
            ForEach(Array(windows.enumerated()), id: \.offset) { _, window in
                let level = budget(Limits(windows: [window], limitReached: false, fetchedAt: limits?.fetchedAt,
                                          error: limits?.error), now: now)
                HStack {
                    Text(windowTitle(window.minutes)).font(.callout)
                    Spacer()
                    Text("\(Int(window.usedPercent!.rounded()))%").fontWeight(.bold).monospacedDigit()
                }
                .padding(.top, 4)
                Meter(fraction: window.usedPercent! / 100, color: level.color, stale: level.stale)
                HStack {
                    Spacer()
                    Text(reset(window.resetsAt, now: now)).font(.caption).foregroundStyle(.secondary)
                }
            }
            if windows.isEmpty { Text("Usage unavailable").font(.callout).foregroundStyle(.secondary) }
            if tool == "codex", let count = limits?.resetsAvailable, count >= 0, count == count.rounded() {
                Text("\(Int(count)) \(count == 1 ? "reset" : "resets") left").font(.callout)
                if count > 0, let expires = limits?.resetExpiresAt, expires > 0 {
                    Text(expires - now > 0 ? "Next expires in \(duration(expires - now))" : "Reset expiry awaiting refresh")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if let error = limits?.error, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } else if budget(limits, now: now).stale && !windows.isEmpty {
                Text("Last known usage").font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.gray.opacity(0.1)))
    }

    private func reset(_ resetsAt: Double?, now: Double) -> String {
        guard let resetsAt, resetsAt > 0 else { return "Reset time unavailable" }
        return resetsAt - now > 0 ? "Resets in \(duration(resetsAt - now))" : "Awaiting refresh"
    }
}

// MARK: - Settings

struct SettingsView: View {
    @AppStorage("show-percent") private var showPercent = true
    @AppStorage("pulse-enabled") private var pulse = true
    @AppStorage("show-token-counts") private var showTokenCounts = true
    @AppStorage("show-cache-info") private var cacheInfo = false
    @AppStorage("notify-ready") private var notifyReady = true
    @AppStorage("notifications-enabled") private var notifications = true
    @AppStorage("sound-enabled") private var soundEnabled = false
    @AppStorage("sound-repeat") private var soundRepeat = "once"
    @AppStorage("sound-effect") private var soundEffect = 1
    @AppStorage("sound-file") private var soundFile = ""
    @AppStorage("sound-volume") private var volume = 70
    var preview: () -> Void

    var body: some View {
        Form {
            Section {
                Toggle("Show allowance percentage", isOn: $showPercent)
                Toggle("Blink the tool background when attention is needed", isOn: $pulse)
            } header: { Text("Menu bar") } footer: {
                Text("Claude and Codex share one menu. Colored underlines show the most consumed allowance: green below 70%, orange near depletion, red when exhausted.")
                    .foregroundStyle(.secondary)
            }
            Section("Tokens") {
                Toggle("Show token counts", isOn: $showTokenCounts)
                Toggle(isOn: $cacheInfo) {
                    Text("Show cached token data")
                    Text("Show cached token counts and cache-hit percentages")
                }
                .disabled(!showTokenCounts)
            }
            Section {
                Toggle("Alert when an agent finishes its turn", isOn: $notifyReady)
                Toggle("Show desktop notifications", isOn: $notifications)
            } header: { Text("Attention") } footer: {
                Text("Approvals and questions need attention. Finished turns can alert too. Acknowledging silences a session until its next state change.")
                    .foregroundStyle(.secondary)
            }
            Section {
                Toggle("Attention sound", isOn: $soundEnabled)
                Picker("Repeat while waiting", selection: $soundRepeat) {
                    ForEach(SOUND_REPEATS, id: \.id) { Text($0.name).tag($0.id) }
                }
                Picker("Sound effect", selection: effect) {
                    ForEach(Array(SOUND_EFFECTS.enumerated()), id: \.offset) { index, sound in
                        Text(sound.name + (index == 0 ? " (default)" : "")).tag(index)
                    }
                    Text("Custom file").tag(SOUND_EFFECTS.count)
                }
                LabeledContent {
                    HStack {
                        Button("Choose…", action: choose)
                        Button("Use default sound") { soundFile = ""; soundEffect = 1 }
                    }
                } label: {
                    Text("Custom sound file")
                    Text(soundFile.isEmpty ? "Using a bundled effect" : soundFile)
                }
                LabeledContent("Sound volume") {
                    HStack {
                        Slider(value: Binding(get: { Double(volume) }, set: { volume = Int($0) }), in: 0...100, step: 5)
                            .frame(width: 180)
                        Text("\(volume)%").monospacedDigit().frame(width: 44, alignment: .trailing)
                    }
                }
                LabeledContent("Preview selected sound") { Button("Play", action: preview) }
            } header: { Text("Sound") } footer: {
                Text("Choose one of three bundled effects, or a custom file. Message is the default.").foregroundStyle(.secondary)
            }
            Section("Terminal sessions") {
                Text("Launch Claude/Codex normally after installing. Codex requires a one-time /hooks trust review. Optional names: ai-monitor run --name \"Fix checkout\" claude. Terminal and iTerm2 select the exact tab once you allow AI Monitor to control them. tmux selects its pane and attached client. Other terminals bring their app to the front. SSH sessions are not forwarded to this Mac.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Section {
                HStack {
                    Link("View repository", destination: REPOSITORY)
                    Spacer()
                    Button("Quit AI Monitor") { NSApp.terminate(nil) }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 560, height: 680)
    }

    private var effect: Binding<Int> {
        Binding(get: { soundFile.isEmpty ? min(max(soundEffect, 1), SOUND_EFFECTS.count) - 1 : SOUND_EFFECTS.count },
                set: { index in
                    if index < SOUND_EFFECTS.count {
                        soundFile = ""
                        soundEffect = index + 1
                    } else if soundFile.isEmpty {
                        choose()
                    }
                })
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.title = "Choose an attention sound"
        panel.allowedContentTypes = [.audio]
        panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url { soundFile = url.path }
    }
}

// MARK: - Application

@main
enum AIMonitorApp {
    static func main() {
        signal(SIGPIPE, SIG_IGN)  // A bridge that exits mid-write must not end the app.
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate, UNUserNotificationCenterDelegate {
    private let defaults = UserDefaults.standard
    private let model = Model()
    private let bridge = Bridge()
    private let tracker = AlertTracker()
    private let reminders = SoundReminders()
    private lazy var sound = SoundPlayer { [weak self] in self?.error($0) }
    private let popover = NSPopover()
    private var statusItem: NSStatusItem!
    private var settingsWindow: NSWindow?
    private var previousApp: NSRunningApplication?
    private var timer: Timer?
    private var pulseOn = true
    private var locked = false
    private var waitingTools: Set<String> = []
    private var budgets: [String: Budget] = [:]
    private var popoverClosedAt: TimeInterval = 0
    private var visibleTab: (bundle: String, tty: String)?
    private var permissions: [String: (allowed: Bool, at: TimeInterval)] = [:]

    private var notifyReady: Bool { defaults.bool(forKey: "notify-ready") }

    func applicationDidFinishLaunching(_: Notification) {
        defaults.register(defaults: DEFAULTS)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePopover)
        popover.behavior = .transient
        popover.delegate = self
        let dashboard = NSHostingController(rootView: DashboardView(model: model, actions: Actions(
            focus: { [weak self] id in
                self?.previousApp = nil
                self?.popover.performClose(nil)
                self?.bridge.send(["op": "focus", "id": id, "timestamp": 0])
            },
            acknowledge: { [weak self] in
                self?.popover.performClose(nil)
                self?.bridge.send(["op": "ack"])
                self?.sound.stop()
            },
            toggleSilent: { [weak self] in
                guard let self else { return }
                popover.performClose(nil)
                defaults.set(!defaults.bool(forKey: "sound-enabled"), forKey: "sound-enabled")
            },
            settings: { [weak self] in
                self?.previousApp = nil
                self?.popover.performClose(nil)
                self?.openSettings()
            }), maxHeight: { max(160, (NSScreen.main?.visibleFrame.height ?? 800) - 140) }))
        dashboard.sizingOptions = [.preferredContentSize]  // The popover follows the dashboard's height.
        popover.contentViewController = dashboard

        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert]) { _, _ in }

        NotificationCenter.default.addObserver(forName: UserDefaults.didChangeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.sound.stop()
            self?.render()
            self?.checkSound()
        }
        let distributed = DistributedNotificationCenter.default()
        distributed.addObserver(forName: .init("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            self?.locked = true
            self?.checkSound()
        }
        distributed.addObserver(forName: .init("com.apple.screenIsUnlocked"), object: nil, queue: .main) { [weak self] _ in
            self?.locked = false
            self?.checkSound()
        }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                                          object: nil, queue: .main) { [weak self] _ in
            self?.visibleTab = nil
            self?.sound.stop()
            self?.checkSound()
        }

        bridge.onLine = { [weak self] line in self?.receive(line) }
        bridge.onExit = { [weak self] in
            self?.model.snapshot = nil
            self?.render()
        }
        bridge.start()
        bridge.send(["op": "active", "value": !locked])
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
        render()
    }

    func applicationWillTerminate(_: Notification) {
        bridge.stop()
        sound.stop()
    }

    private func tick() {
        pulseOn.toggle()
        paint()
        checkSound()
        bridge.send(["op": "active", "value": !locked])
        if !bridge.running { bridge.start() }
    }

    // MARK: Bridge messages

    private func receive(_ line: Data) {
        guard let message = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              let type = message["type"] as? String else { return }
        switch type {
        case "snapshot":
            if let snapshot = try? JSONDecoder().decode(Snapshot.self, from: line) { update(snapshot) }
        case "focus":
            struct Focus: Decodable { var id: String; var target: FocusTarget }
            if let focus = try? JSONDecoder().decode(Focus.self, from: line) { self.focus(focus.id, focus.target) }
        case "error":
            error(message["error"] as? String ?? "The service could not complete the request")
        case "offline":
            model.snapshot = nil
            render()
        default:
            break
        }
    }

    private func update(_ snapshot: Snapshot) {
        model.snapshot = snapshot
        let fresh = tracker.update(snapshot.sessions, notifyReady: notifyReady, locked: locked)
        // Coalesce banners; sound resolves the visible terminal tab below.
        let unseen = fresh.filter { !isVisible($0) }
        if !unseen.isEmpty && defaults.bool(forKey: "notifications-enabled") {
            let first = unseen[0]
            notify(unseen.count > 1 ? "\(unseen.count) agents need attention" : "\(TITLES[first.tool] ?? first.tool) · \(first.title)",
                   unseen.count > 1 ? unseen.map { "\(TITLES[$0.tool] ?? $0.tool): \($0.title)" }.joined(separator: "\n")
                       : STATES[first.state] ?? "")
        }
        checkSound()
        render()
    }

    // MARK: Visibility and sound

    private func allowed(_ bundle: String) -> Bool {
        let now = ProcessInfo.processInfo.systemUptime
        if let cached = permissions[bundle], now - cached.at < 5 { return cached.allowed }
        let value = automationAllowed(bundle)
        permissions[bundle] = (value, now)
        return value
    }

    /// Whether a session's terminal is in front. Terminal and iTerm2 tabs are matched exactly
    /// once automation is allowed; other apps cannot tell their windows and tabs apart.
    private func isVisible(_ row: Session) -> Bool {
        guard let front = NSWorkspace.shared.frontmostApplication?.bundleIdentifier else { return false }
        return (row.views ?? []).contains { view in
            guard let terminal = view.macTerminal, terminal.bundleId == front else { return false }
            if EXACT_TABS[front] != nil && allowed(front) {
                return visibleTab?.bundle == front && validTty(terminal.tty) == visibleTab?.tty
            }
            return true
        }
    }

    private func checkSound() {
        let front = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""
        let rows = model.snapshot?.sessions ?? []
        let relevant = EXACT_TABS[front] != nil && rows.contains { row in
            attention(row, notifyReady: notifyReady) &&
                (row.views ?? []).contains { $0.macTerminal?.bundleId == front && validTty($0.macTerminal?.tty) != nil }
        } && allowed(front)
        guard relevant, let script = EXACT_TABS[front]?.visible else {
            visibleTab = nil
            applySound()
            return
        }
        // Only the front tab's tty is requested; terminal contents are never read.
        visibleTab = validTty(runScript(script)?.stringValue).map { (front, $0) }
        if visibleTab != nil { acknowledgeSeen() }
        applySound()
    }

    // A waiting session shown in the front tab counts as seen, like choosing it in the menu.
    private func acknowledgeSeen() {
        guard !locked, let tab = visibleTab, var snapshot = model.snapshot else { return }
        let seen = Set(snapshot.sessions.filter { row in
            attention(row, notifyReady: notifyReady) && (row.views ?? []).contains {
                $0.macTerminal?.bundleId == tab.bundle && validTty($0.macTerminal?.tty) == tab.tty }
        }.map(\.id))
        guard !seen.isEmpty else { return }
        for id in seen { bridge.send(["op": "ack", "id": id]) }
        // The bridge snapshots every two seconds; stop the highlight now.
        snapshot.sessions = snapshot.sessions.map { row in
            var row = row
            if seen.contains(row.id) { row.attention = false }
            return row
        }
        model.snapshot = snapshot
        render()
    }

    private func applySound() {
        let snapshot = model.snapshot
        let rows = snapshot?.sessions ?? []
        let now = Date().timeIntervalSince1970
        let available = snapshot != nil && (snapshot?.heartbeat.map { abs(now - $0) < 10 } ?? true)
        let enabled = defaults.bool(forKey: "sound-enabled") && defaults.integer(forKey: "sound-volume") > 0
        let suppressed = Set(rows.filter { isVisible($0) }.map(\.id))
        let audible = rows.contains { attention($0, notifyReady: notifyReady) && !suppressed.contains($0.id) }
        if !available || !enabled || locked || !audible { sound.stop() }
        let interval = SOUND_REPEATS.first { $0.id == defaults.string(forKey: "sound-repeat") }?.seconds ?? 0
        if reminders.update(rows, interval: interval, enabled: enabled, locked: locked, available: available,
                            notifyReady: notifyReady, suppressedIds: suppressed) {
            sound.play()
        }
    }

    // MARK: Menu bar

    private func render() {
        let snapshot = model.snapshot
        let sessions = snapshot?.sessions ?? []
        let available = TOOLS.filter { tool in
            snapshot?.tools?.contains(tool) == true || sessions.contains { $0.tool == tool } ||
                !((snapshot?.usage.limits?[tool] ?? nil)?.windows ?? []).isEmpty ||
                (snapshot?.usage.tokens?[tool] ?? [:]).values.contains { tokenTotal($0) > 0 }
        }
        let visible = TOOLS.filter { available.isEmpty ? $0 == "codex" : available.contains($0) }
        if model.visibleTools != visible { model.visibleTools = visible }
        var total = 0, waiting = 0
        waitingTools = []
        for tool in TOOLS {
            budgets[tool] = budget(snapshot?.usage.limits?[tool] ?? nil)
            let count = counts(sessions, tool: tool, notifyReady: notifyReady)
            if count.waiting > 0 { waitingTools.insert(tool) }
            total += count.total
            waiting += count.waiting
        }
        statusItem.button?.setAccessibilityLabel("ai-monitor: \(total) terminal sessions, \(waiting) needing attention")
        paint()
    }

    private func paint() {
        let showPercent = defaults.bool(forKey: "show-percent")
        let pulse = defaults.bool(forKey: "pulse-enabled")
        let tools = model.visibleTools.map { tool -> (String, Budget, Bool) in
            (tool, budgets[tool] ?? budget(nil), waitingTools.contains(tool) && (pulseOn || !pulse))
        }
        statusItem.button?.image = statusImage(tools, showPercent: showPercent)
    }

    private func statusImage(_ tools: [(String, Budget, Bool)], showPercent: Bool) -> NSImage {
        let nameFont = NSFont.systemFont(ofSize: 11, weight: .semibold)
        let percentFont = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        let height = max(18, NSStatusBar.system.thickness)
        let padding: CGFloat = 7, spacing: CGFloat = 4, meterWidth: CGFloat = 44
        let layouts = tools.map { tool, level, highlighted -> (String, String, Budget, Bool, CGFloat, CGFloat) in
            let name = TITLES[tool] ?? tool
            let percent = level.percent.map { "\(Int($0.rounded()))%\(level.stale ? "*" : "")" } ?? "—"
            let nameWidth = max(meterWidth, ceil((name as NSString).size(withAttributes: [.font: nameFont]).width))
            let percentWidth = showPercent ? ceil((percent as NSString).size(withAttributes: [.font: percentFont]).width) + 5 : 0
            return (name, percent, level, highlighted, nameWidth, percentWidth)
        }
        let width = layouts.reduce(0) { $0 + padding * 2 + $1.4 + $1.5 } + spacing * CGFloat(max(0, layouts.count - 1))
        let image = NSImage(size: NSSize(width: max(width, 1), height: height), flipped: true) { _ in
            var x: CGFloat = 0
            for (name, percent, level, highlighted, nameWidth, percentWidth) in layouts {
                let pill = NSRect(x: x, y: 2, width: padding * 2 + nameWidth + percentWidth, height: height - 4)
                if highlighted {
                    NSColor(srgbRed: 166 / 255, green: 104 / 255, blue: 225 / 255, alpha: 0.32).setFill()
                    NSBezierPath(roundedRect: pill, xRadius: pill.height / 2, yRadius: pill.height / 2).fill()
                }
                let top = (height - 17) / 2
                (name as NSString).draw(at: NSPoint(x: x + padding, y: top - 1),
                                        withAttributes: [.font: nameFont, .foregroundColor: NSColor.labelColor])
                let track = NSRect(x: x + padding, y: top + 13, width: meterWidth, height: 3)
                NSColor.gray.withAlphaComponent(0.35).setFill()
                NSBezierPath(roundedRect: track, xRadius: 1.5, yRadius: 1.5).fill()
                let fraction = max(0, min(1, (level.percent ?? 0) / 100))
                if fraction > 0 {
                    NSColor(hex: level.color).withAlphaComponent(level.stale ? 0.5 : 1).setFill()
                    let fill = NSRect(x: track.minX, y: track.minY, width: max(3, fraction * meterWidth), height: 3)
                    NSBezierPath(roundedRect: fill, xRadius: 1.5, yRadius: 1.5).fill()
                }
                if percentWidth > 0 {
                    let size = (percent as NSString).size(withAttributes: [.font: percentFont])
                    (percent as NSString).draw(at: NSPoint(x: x + padding + nameWidth + 5, y: (height - size.height) / 2),
                                               withAttributes: [.font: percentFont, .foregroundColor: NSColor.labelColor])
                }
                x += pill.width + spacing
            }
            return true
        }
        image.isTemplate = false
        return image
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        // The mouse-down on this button already closed a transient popover; do not reopen it.
        if ProcessInfo.processInfo.systemUptime - popoverClosedAt < 0.3 { return }
        let front = NSWorkspace.shared.frontmostApplication
        previousApp = front?.processIdentifier == ProcessInfo.processInfo.processIdentifier ? nil : front
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }

    func popoverDidClose(_: Notification) {
        popoverClosedAt = ProcessInfo.processInfo.systemUptime
        // Return focus to the app that was in front, unless a session or Settings was chosen.
        if settingsWindow?.isVisible != true { previousApp?.activate() }
        previousApp = nil
    }

    private func openSettings() {
        if settingsWindow == nil {
            let window = NSWindow(contentViewController: NSHostingController(rootView: SettingsView { [weak self] in
                self?.sound.play(preview: true)
            }))
            window.title = "AI Monitor Settings"
            window.styleMask = [.titled, .closable, .miniaturizable]
            window.isReleasedWhenClosed = false
            window.center()
            settingsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    // MARK: Focus

    private func focus(_ id: String, _ target: FocusTarget) {
        let bundle = target.macTerminal?.bundleId ?? ""
        if let tty = validTty(target.macTerminal?.tty), let select = EXACT_TABS[bundle]?.select(tty),
           !NSRunningApplication.runningApplications(withBundleIdentifier: bundle).isEmpty {
            let result = runScript(select)
            if let result, result.booleanValue {
                bridge.send(["op": "ack", "id": id])
            } else if result != nil {
                error("The original terminal tab has closed.")
            } else {
                // Automation was declined or failed: still bring the right app forward.
                activate(id, target)
            }
            return
        }
        activate(id, target)
    }

    private func activate(_ id: String, _ target: FocusTarget) {
        let bundle = target.macTerminal?.bundleId ?? ""
        let byBundle = bundle.isEmpty ? nil : NSRunningApplication.runningApplications(withBundleIdentifier: bundle).first
        let byProcess = (target.pids ?? []).lazy.compactMap { NSRunningApplication(processIdentifier: pid_t($0)) }
            .first { $0.activationPolicy == .regular }
        guard let app = byBundle ?? byProcess else {
            error(target.tmux == true ? "Pane selected. Focus the terminal attached to tmux to continue."
                : "Could not identify a desktop window for this terminal. Open it manually.")
            return
        }
        app.activate(options: [.activateAllWindows])
        bridge.send(["op": "ack", "id": id])
    }

    // MARK: Notifications

    private func notify(_ title: String, _ body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }

    private func error(_ message: String) { notify("ai-monitor", message) }

    func userNotificationCenter(_: UNUserNotificationCenter, willPresent _: UNNotification,
                                withCompletionHandler completion: @escaping (UNNotificationPresentationOptions) -> Void) {
        completion([.banner, .list])
    }
}

/// Runs one AppleScript on the main thread, as NSAppleScript requires; each script bounds its
/// own wait. Returns nil when the script could not run (for example, automation was declined).
func runScript(_ source: String) -> NSAppleEventDescriptor? {
    var error: NSDictionary?
    let result = NSAppleScript(source: source)?.executeAndReturnError(&error)
    return error == nil ? result : nil
}
