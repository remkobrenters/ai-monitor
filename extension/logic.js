// Pure presentation and notification decisions; shared by GNOME and Node tests.
export const TOOLS = ['claude', 'codex'];
export const TITLES = {claude: 'Claude', codex: 'Codex'};
export const STATES = {working: 'Working', approval: 'Needs approval', question: 'Needs answer',
    ready: 'Turn finished', idle: 'Idle', unknown: 'Waiting for status hooks'};
export const COLORS = {normal: '#56c785', warning: '#efb85d', danger: '#f27d78',
    unknown: '#959ba8', attention: '#c5a0f5'};

export function budget(limits, now = Date.now() / 1000) {
    const windows = (limits?.windows ?? []).filter(w => Number.isFinite(w.usedPercent));
    if (!windows.length) return {percent: null, color: COLORS.unknown, stale: true};
    const percent = limits.limitReached ? 100 : Math.max(...windows.map(w => w.usedPercent));
    const stale = Boolean(limits.error) || !limits.fetchedAt || now - limits.fetchedAt > 1200 ||
        windows.some(w => w.resetsAt && w.resetsAt <= now);
    return {percent, stale, color: stale ? COLORS.unknown :
        percent >= 100 ? COLORS.danger : percent >= 70 ? COLORS.warning : COLORS.normal};
}

export function attention(row, notifyReady = true) {
    return row.attention && ['approval', 'question', ...(notifyReady ? ['ready'] : [])].includes(row.state);
}

export function counts(rows, tool, notifyReady = true) {
    const own = rows.filter(row => row.tool === tool);
    return {working: own.filter(row => row.state === 'working').length,
        waiting: own.filter(row => attention(row, notifyReady)).length, total: own.length};
}

export function sessionStatus(row) {
    const count = Number.isInteger(row.subagentCount) && row.subagentCount > 0 ? row.subagentCount : 0;
    if (row.state === 'working' && count) {
        const agents = `${count} ${count === 1 ? 'subagent' : 'subagents'}`;
        return row.waitingForSubagents ? `Waiting for ${agents} to finish` : `Working · ${agents}`;
    }
    return STATES[row.state] ?? 'Status unavailable';
}

export function tokenTotal(value) {
    return ['input', 'cacheRead', 'cacheWrite', 'output'].reduce((n, k) => n + (value?.[k] ?? 0), 0);
}

export function cacheShare(values) {
    const input = values.reduce((sum, v) => sum + (v?.input ?? 0) + (v?.cacheRead ?? 0) + (v?.cacheWrite ?? 0), 0);
    return input ? values.reduce((sum, v) => sum + (v?.cacheRead ?? 0), 0) / input * 100 : null;
}

export function tokens(value) {
    if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
    if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
    if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
    return `${Math.round(value)}`;
}

export function duration(seconds) {
    const minutes = Math.max(0, Math.ceil(seconds / 60));
    if (minutes < 60) return `${minutes}m`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
    return `${Math.floor(minutes / 1440)}d ${Math.floor(minutes % 1440 / 60)}h`;
}

export function windowTitle(minutes) {
    if (minutes === 10080) return 'Weekly';
    if (minutes % 60 === 0 && minutes > 0) return `${minutes / 60}-hour`;
    return minutes ? `${minutes}-minute` : 'Current window';
}

export class AlertTracker {
    constructor() {
        this.seen = new Map();
        this.initialized = false;
    }

    update(rows, {notifyReady = true, locked = false} = {}) {
        const fresh = rows.filter(row => attention(row, notifyReady) &&
            this.seen.get(row.id) !== row.revision);
        const result = this.initialized && !locked ? fresh : [];
        this.seen = new Map(rows.map(row => [row.id, row.revision]));
        this.initialized = true;
        return result;
    }
}

// One coalesced chime when any unacknowledged terminal becomes due. Deadlines
// use monotonic time; suppressed reminders are deferred, never caught up in a burst.
export class SoundReminders {
    constructor(clock = () => Date.now() / 1000) {
        this.clock = clock;
        this.pending = new Map();
        this.initialized = false;
        this.interval = 0;
    }

    update(rows, {interval = 0, enabled = true, locked = false,
        available = true, notifyReady = true, suppressedIds = []} = {}) {
        const now = this.clock();
        const seconds = [0, 60, 300].includes(interval) ? interval : 0;
        const deadline = () => seconds ? now + seconds : Infinity;
        if (seconds !== this.interval) {
            for (const entry of this.pending.values()) entry.due = deadline();
            this.interval = seconds;
        }
        if (!available) {
            for (const entry of this.pending.values()) entry.due = deadline();
            return false;
        }
        const suppressed = new Set(suppressedIds);
        const waiting = rows.filter(row => attention(row, notifyReady));
        const active = new Set(waiting.map(row => row.id));
        for (const id of this.pending.keys()) if (!active.has(id)) this.pending.delete(id);
        let play = false;
        for (const row of waiting) {
            let entry = this.pending.get(row.id);
            const fresh = !entry || entry.revision !== row.revision;
            if (fresh) {
                entry = {revision: row.revision, due: deadline()};
                this.pending.set(row.id, entry);
            }
            if (!enabled || locked || suppressed.has(row.id)) {
                entry.due = deadline();
                continue;
            }
            if ((fresh && this.initialized) || (seconds && now >= entry.due)) {
                play = true;
                entry.due = deadline();
            }
        }
        if (play) for (const entry of this.pending.values()) entry.due = deadline();
        this.initialized = true;
        return play;
    }
}
