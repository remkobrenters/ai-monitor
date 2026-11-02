// Behavior tests for macos/Logic.swift, matching tests/logic.test.mjs. Built by `make test`:
//   swiftc macos/Logic.swift macos/tests/main.swift -o <binary> && <binary>
import Foundation

var failures = 0
var current = ""

func test(_ name: String, _ body: () -> Void) {
    current = name
    body()
    print("ok - \(name)")
}

func expect<T: Equatable>(_ actual: T, _ expected: T, line: Int = #line) {
    if actual != expected {
        failures += 1
        print("FAIL - \(current) (line \(line)): \(actual) != \(expected)")
    }
}

func row(_ id: String = "a", tool: String = "claude", state: String = "approval", attention: Bool = true,
         revision: Int = 1, subagents: Int? = nil, waitingForSubagents: Bool? = nil) -> Session {
    Session(id: id, tool: tool, state: state, attention: attention, revision: revision,
            subagentCount: subagents, waitingForSubagents: waitingForSubagents)
}

test("subagent activity is work, while approvals retain their input-needed state") {
    let working = row(tool: "codex", state: "working", attention: false, subagents: 3)
    expect(sessionStatus(working), "Working · 3 subagents")
    var waiting = working
    waiting.waitingForSubagents = true
    expect(sessionStatus(waiting), "Waiting for 3 subagents to finish")
    waiting.subagentCount = 1
    expect(sessionStatus(waiting), "Waiting for 1 subagent to finish")
    waiting.state = "approval"
    expect(sessionStatus(waiting), "Needs approval")
    expect(counts([working], tool: "codex").waiting, 0)
    expect(counts([working], tool: "codex").working, 1)
}

test("quota uses the most consumed window and marks stale data") {
    let limits = Limits(windows: [LimitWindow(usedPercent: 20), LimitWindow(usedPercent: 91)], fetchedAt: 1000)
    expect(budget(limits, now: 1100).percent, 91)
    expect(budget(limits, now: 1100).color, Palette.warning)
    expect(budget(limits, now: 1100).stale, false)
    expect(budget(limits, now: 2500).stale, true)
    var failed = limits
    failed.error = "offline"
    expect(budget(failed, now: 1100).stale, true)
    expect(budget(Limits(windows: [LimitWindow(usedPercent: .nan)])).percent, nil)
    expect(budget(nil).percent, nil)
    let expired = Limits(windows: [LimitWindow(usedPercent: 20, resetsAt: 1050)], fetchedAt: 1000)
    expect(budget(expired, now: 1100).stale, true)
}

test("circle color follows whichever allowance window prevents working") {
    func limits(_ values: [Double], reached: Bool = false) -> Limits {
        Limits(windows: values.map { LimitWindow(usedPercent: $0) }, limitReached: reached, fetchedAt: 1000)
    }
    expect(budget(limits([20, 65]), now: 1100).color, Palette.normal)
    expect(budget(limits([98, 20]), now: 1100).color, Palette.warning)
    expect(budget(limits([100, 20]), now: 1100).color, Palette.danger)
    expect(budget(limits([20, 100]), now: 1100).color, Palette.danger)
    expect(budget(limits([99], reached: true), now: 1100).color, Palette.danger)
}

test("cached input is counted once and cache share is weighted by input") {
    let values = [TokenCounts(input: 10, cacheRead: 90, cacheWrite: 0, output: 5),
                  TokenCounts(input: 900, cacheRead: 0, cacheWrite: 0, output: 10)]
    expect(tokenTotal(values[0]), 105)
    expect(cacheShare(values), 9)
    expect(cacheShare([]), nil)
}

test("token, duration and window labels match the GNOME panel") {
    expect(formatTokens(999), "999")
    expect(formatTokens(1500), "1.5k")
    expect(formatTokens(2_340_000), "2.3M")
    expect(formatTokens(4.2e9), "4.2B")
    expect(duration(59), "1m")
    expect(duration(3600 + 120), "1h 2m")
    expect(duration(2 * 86400 + 3 * 3600), "2d 3h")
    expect(windowTitle(10080), "Weekly")
    expect(windowTitle(300), "5-hour")
    expect(windowTitle(nil), "Current window")
}

test("alerts fire once per transition and never replay after lock") {
    let tracker = AlertTracker()
    var item = row(state: "working", attention: false, revision: 1)
    expect(tracker.update([item]).count, 0)
    item.state = "approval"; item.attention = true; item.revision = 2
    expect(tracker.update([item]).count, 1)
    expect(tracker.update([item]).count, 0)
    item.state = "ready"; item.revision = 3
    expect(tracker.update([item], locked: true).count, 0)
    expect(tracker.update([item]).count, 0)
}

test("working count excludes waiting and acknowledged idle sessions") {
    let rows = [row(state: "working", attention: false), row(state: "ready"),
                row(state: "approval", attention: false), row(tool: "codex", state: "working", attention: false)]
    expect(counts(rows, tool: "claude"), Counts(working: 1, waiting: 1, total: 3))
    expect(counts(rows, tool: "claude", notifyReady: false).waiting, 0)
    expect(attention(rows[1], notifyReady: false), false)
}

final class Clock { var seconds: Double = 0 }

func reminders() -> (Clock, SoundReminders, Session) {
    let time = Clock()
    let sound = SoundReminders(clock: { time.seconds })
    _ = sound.update([])
    return (time, sound, row())
}

test("Once plays on a new wait, without periodic reminders or startup replay") {
    let (time, sound, item) = reminders()
    expect(sound.update([item]), true)
    time.seconds = 10000
    expect(sound.update([item]), false)
    expect(SoundReminders().update([item]), false)
}

for interval in [60.0, 300.0] {
    test("waiting sound repeats every \(Int(interval)) seconds and stops after acknowledgement, work or exit") {
        let (time, sound, item) = reminders()
        expect(sound.update([item], interval: interval), true)
        time.seconds = interval - 1
        expect(sound.update([item], interval: interval), false)
        time.seconds = interval
        expect(sound.update([item], interval: interval), true)
        expect(sound.update([item], interval: interval), false)
        var acknowledged = item
        acknowledged.attention = false
        _ = sound.update([acknowledged], interval: interval)
        time.seconds += interval
        expect(sound.update([acknowledged], interval: interval), false)
        var changed = item
        changed.revision = 2
        expect(sound.update([changed], interval: interval), true)
        var working = item
        working.state = "working"; working.attention = false
        _ = sound.update([working], interval: interval)
        time.seconds += interval
        expect(sound.update([], interval: interval), false)
    }
}

let blocks: [(String, (SoundReminders, Session) -> Bool)] = [
    ("locked", { $0.update([$1], interval: 60, locked: true) }),
    ("disabled", { $0.update([$1], interval: 60, enabled: false) }),
    ("unavailable", { $0.update([$1], interval: 60, available: false) }),
    ("suppressed", { $0.update([$1], interval: 60, suppressedIds: ["a"]) }),
]
for (name, blocked) in blocks {
    test("suppression \(name) defers reminders without a catch-up chime") {
        let (time, sound, item) = reminders()
        _ = sound.update([item], interval: 60)
        time.seconds = 600
        expect(blocked(sound, item), false)
        expect(sound.update([item], interval: 60), false)
        time.seconds = 659
        expect(sound.update([item], interval: 60), false)
        time.seconds = 660
        expect(sound.update([item], interval: 60), true)
    }
}

test("cadence changes take effect immediately and Once cancels pending repeats") {
    let (time, sound, item) = reminders()
    _ = sound.update([item], interval: 60)
    time.seconds = 30
    expect(sound.update([item], interval: 300), false)
    time.seconds = 329
    expect(sound.update([item], interval: 300), false)
    time.seconds = 330
    expect(sound.update([item], interval: 300), true)
    _ = sound.update([item], interval: 0)
    time.seconds = 10000
    expect(sound.update([item], interval: 0), false)
}

test("multiple waiting terminals share a reminder cadence") {
    let (time, sound, item) = reminders()
    expect(sound.update([item], interval: 60), true)
    time.seconds = 30
    let other = row("b")
    expect(sound.update([item, other], interval: 60), true)
    time.seconds = 60
    expect(sound.update([item, other], interval: 60), false)
    time.seconds = 90
    expect(sound.update([item, other], interval: 60), true)
    expect(sound.update([item, other], interval: 60), false)
}

test("service snapshots decode, including missing limits and unknown keys") {
    let json = """
    {"type": "snapshot", "sessions": [{"id": "x", "tool": "claude", "title": "Fix", "project": "/p",
      "state": "ready", "since": 1.5, "attention": true, "revision": 4, "subagentCount": 0,
      "views": [{"windowId": "", "gnomeTerminal": null, "macTerminal": {"bundleId": "com.apple.Terminal", "tty": "/dev/ttys001"}}]}],
     "usage": {"tokens": {"claude": {"today": {"input": 1, "cacheRead": 2, "cacheWrite": 3, "output": 4}}},
               "limits": {"claude": null, "codex": {"windows": [{"minutes": 300, "usedPercent": 12.5, "resetsAt": null}],
               "resetsAvailable": 2, "fetchedAt": 10}}, "errors": {"claude": ""}},
     "collectorError": "", "heartbeat": 12.0, "tools": ["claude"], "health": {}}
    """
    let snapshot = try! JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    expect(snapshot.sessions[0].views?[0].macTerminal?.tty, "/dev/ttys001")
    expect(tokenTotal(snapshot.usage.tokens?["claude"]?["today"]), 10)
    expect(snapshot.usage.limits?["claude"]! == nil, true)
    expect(snapshot.usage.limits?["codex"]??.windows?[0].usedPercent, 12.5)
}

if failures > 0 {
    print("\(failures) failure(s)")
    exit(1)
}
