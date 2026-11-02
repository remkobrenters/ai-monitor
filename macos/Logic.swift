// Pure presentation and notification decisions for the macOS menu bar app.
// Mirrors extension/logic.js; macos/tests/main.swift covers the same cases as tests/logic.test.mjs.
import Foundation

let TOOLS = ["claude", "codex"]
let TITLES = ["claude": "Claude", "codex": "Codex"]
let STATES = ["working": "Working", "approval": "Needs approval", "question": "Needs answer",
              "ready": "Turn finished", "idle": "Idle", "unknown": "Waiting for status hooks"]

enum Palette {
    static let normal = "#56c785"
    static let warning = "#efb85d"
    static let danger = "#f27d78"
    static let unknown = "#959ba8"
    static let attention = "#c5a0f5"
}

struct SoundEffect { let name: String; let file: String }
let SOUND_EFFECTS = [SoundEffect(name: "Message", file: "sound_effect_1"),
                     SoundEffect(name: "Level Up 07", file: "sound_effect_2"),
                     SoundEffect(name: "Notification Chime", file: "sound_effect_3")]
struct SoundRepeat { let id: String; let name: String; let seconds: Double }
let SOUND_REPEATS = [SoundRepeat(id: "once", name: "Once", seconds: 0),
                     SoundRepeat(id: "minute", name: "Every minute", seconds: 60),
                     SoundRepeat(id: "five-minutes", name: "Every five minutes", seconds: 300)]

// MARK: - Service snapshot (see Monitor.dispatch in ai_monitor/service.py)

struct TokenCounts: Decodable {
    var input: Double? = 0
    var cacheRead: Double? = 0
    var cacheWrite: Double? = 0
    var output: Double? = 0
}

struct LimitWindow: Decodable {
    var minutes: Double?
    var usedPercent: Double?
    var resetsAt: Double?
}

struct Limits: Decodable {
    var plan: String?
    var windows: [LimitWindow]?
    var limitReached: Bool?
    var resetsAvailable: Double?
    var resetExpiresAt: Double?
    var fetchedAt: Double?
    var error: String?
}

struct Usage: Decodable {
    var tokens: [String: [String: TokenCounts]]?
    var limits: [String: Limits?]?
    var errors: [String: String]?
}

struct MacTerminal: Decodable, Equatable {
    var bundleId: String?
    var tty: String?
}

struct TerminalView: Decodable {
    var macTerminal: MacTerminal?
}

struct Session: Decodable, Identifiable {
    var id: String
    var tool: String
    var title: String = ""
    var project: String? = nil
    var state: String
    var since: Double = 0
    var attention: Bool = false
    var revision: Int = 0
    var subagentCount: Int? = nil
    var waitingForSubagents: Bool? = nil
    var views: [TerminalView]? = nil
}

struct Snapshot: Decodable {
    var sessions: [Session]
    var usage: Usage
    var collectorError: String?
    var heartbeat: Double?
    var tools: [String]?
}

struct FocusTarget: Decodable {
    var pids: [Int]?
    var tmux: Bool?
    var macTerminal: MacTerminal?
}

// MARK: - Presentation

struct Budget: Equatable {
    var percent: Double?
    var color: String
    var stale: Bool
}

func budget(_ limits: Limits?, now: Double = Date().timeIntervalSince1970) -> Budget {
    let windows = (limits?.windows ?? []).filter { $0.usedPercent?.isFinite == true }
    guard let limits, !windows.isEmpty else { return Budget(percent: nil, color: Palette.unknown, stale: true) }
    let percent = limits.limitReached == true ? 100 : windows.map { $0.usedPercent! }.max()!
    let fetched = limits.fetchedAt ?? 0
    let stale = !(limits.error ?? "").isEmpty || fetched == 0 || now - fetched > 1200 ||
        windows.contains { ($0.resetsAt ?? 0) != 0 && $0.resetsAt! <= now }
    return Budget(percent: percent, color: stale ? Palette.unknown :
        percent >= 100 ? Palette.danger : percent >= 70 ? Palette.warning : Palette.normal, stale: stale)
}

func attention(_ row: Session, notifyReady: Bool = true) -> Bool {
    row.attention && (["approval", "question"] + (notifyReady ? ["ready"] : [])).contains(row.state)
}

struct Counts: Equatable { var working: Int; var waiting: Int; var total: Int }

func counts(_ rows: [Session], tool: String, notifyReady: Bool = true) -> Counts {
    let own = rows.filter { $0.tool == tool }
    return Counts(working: own.filter { $0.state == "working" }.count,
                  waiting: own.filter { attention($0, notifyReady: notifyReady) }.count, total: own.count)
}

func sessionStatus(_ row: Session) -> String {
    let count = max(0, row.subagentCount ?? 0)
    if row.state == "working" && count > 0 {
        let agents = "\(count) \(count == 1 ? "subagent" : "subagents")"
        return row.waitingForSubagents == true ? "Waiting for \(agents) to finish" : "Working · \(agents)"
    }
    return STATES[row.state] ?? "Status unavailable"
}

extension TokenCounts {
    var cached: Double { cacheRead ?? 0 }
    var allInput: Double { (input ?? 0) + cached + (cacheWrite ?? 0) }
}

func tokenTotal(_ value: TokenCounts?) -> Double {
    guard let value else { return 0 }
    return value.allInput + (value.output ?? 0)
}

func cacheShare(_ values: [TokenCounts?]) -> Double? {
    let input: Double = values.reduce(0) { $0 + ($1?.allInput ?? 0) }
    let cached: Double = values.reduce(0) { $0 + ($1?.cached ?? 0) }
    return input > 0 ? cached / input * 100 : nil
}

func formatTokens(_ value: Double) -> String {
    if value >= 1e9 { return String(format: "%.1fB", value / 1e9) }
    if value >= 1e6 { return String(format: "%.1fM", value / 1e6) }
    if value >= 1e3 { return String(format: "%.1fk", value / 1e3) }
    return String(Int(value.rounded()))
}

func duration(_ seconds: Double) -> String {
    let minutes = max(0, Int((seconds / 60).rounded(.up)))
    if minutes < 60 { return "\(minutes)m" }
    if minutes < 1440 { return "\(minutes / 60)h \(minutes % 60)m" }
    return "\(minutes / 1440)d \(minutes % 1440 / 60)h"
}

func windowTitle(_ minutes: Double?) -> String {
    let minutes = Int(minutes ?? 0)
    if minutes == 10080 { return "Weekly" }
    if minutes > 0 && minutes % 60 == 0 { return "\(minutes / 60)-hour" }
    return minutes > 0 ? "\(minutes)-minute" : "Current window"
}

// MARK: - Alerts

final class AlertTracker {
    private var seen: [String: Int] = [:]
    private var initialized = false

    func update(_ rows: [Session], notifyReady: Bool = true, locked: Bool = false) -> [Session] {
        let fresh = rows.filter { attention($0, notifyReady: notifyReady) && seen[$0.id] != $0.revision }
        let result = initialized && !locked ? fresh : []
        seen = Dictionary(rows.map { ($0.id, $0.revision) }, uniquingKeysWith: { _, last in last })
        initialized = true
        return result
    }
}

// One coalesced chime when any unacknowledged terminal becomes due. Deadlines
// use monotonic time; suppressed reminders are deferred, never caught up in a burst.
final class SoundReminders {
    private struct Entry { var revision: Int; var due: Double }
    private let clock: () -> Double
    private var pending: [String: Entry] = [:]
    private var initialized = false
    private var interval: Double = 0

    init(clock: @escaping () -> Double = { ProcessInfo.processInfo.systemUptime }) { self.clock = clock }

    func update(_ rows: [Session], interval requested: Double = 0, enabled: Bool = true, locked: Bool = false,
                available: Bool = true, notifyReady: Bool = true, suppressedIds: Set<String> = []) -> Bool {
        let now = clock()
        let seconds = [0, 60, 300].contains(requested) ? requested : 0
        let deadline = seconds > 0 ? now + seconds : Double.infinity
        if seconds != interval {
            for key in pending.keys { pending[key]!.due = deadline }
            interval = seconds
        }
        if !available {
            for key in pending.keys { pending[key]!.due = deadline }
            return false
        }
        let waiting = rows.filter { attention($0, notifyReady: notifyReady) }
        let active = Set(waiting.map(\.id))
        pending = pending.filter { active.contains($0.key) }
        var play = false
        for row in waiting {
            var entry = pending[row.id]
            let fresh = entry == nil || entry!.revision != row.revision
            if fresh { entry = Entry(revision: row.revision, due: deadline) }
            if !enabled || locked || suppressedIds.contains(row.id) {
                entry!.due = deadline
            } else if (fresh && initialized) || (seconds > 0 && now >= entry!.due) {
                play = true
                entry!.due = deadline
            }
            pending[row.id] = entry
        }
        if play { for key in pending.keys { pending[key]!.due = deadline } }
        initialized = true
        return play
    }
}
