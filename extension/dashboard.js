import Clutter from 'gi://Clutter';
import GObject from 'gi://GObject';
import St from 'gi://St';
import Pango from 'gi://Pango';
import {TOOLS, TITLES, COLORS, attention, budget, tokens, tokenTotal,
    cacheShare, duration, windowTitle, sessionStatus} from './logic.js';

export const vertical = St.BoxLayout.list_properties().some(p => p.name === 'orientation')
    ? {orientation: Clutter.Orientation.VERTICAL} : {vertical: true};
export const label = (value = '', style = '') => {
    const actor = new St.Label({text: value, style_class: style, y_align: Clutter.ActorAlign.CENTER});
    if (/\bam-(muted|plan|reset|table-heading|session-project|provider-error)\b/.test(style)) actor.opacity = 200;
    return actor;
};
const rgb = hex => [1, 3, 5].map(start => parseInt(hex.slice(start, start + 2), 16) / 255);

export const Meter = GObject.registerClass(class AiMonitorMeter extends St.DrawingArea {
    _init(props = {}) {
        super._init({style_class: 'am-meter', x_expand: true, ...props});
        this.level = {fraction: 0, color: COLORS.unknown, stale: false};
        this.connect('repaint', () => {
            const cr = this.get_context();
            const [width, height] = this.get_surface_size();
            const radius = height / 2;
            const capsule = (size) => {
                cr.newSubPath();
                cr.arc(Math.max(radius, size - radius), radius, radius, -Math.PI / 2, Math.PI / 2);
                cr.arc(radius, radius, radius, Math.PI / 2, Math.PI * 1.5);
                cr.closePath();
            };
            capsule(width);
            cr.setSourceRGBA(0.5, 0.5, 0.5, 0.2);
            cr.fill();
            const fill = Math.max(0, Math.min(1, this.level.fraction)) * width;
            if (fill > 0) {
                capsule(Math.max(height, fill));
                cr.setSourceRGBA(...rgb(this.level.color), this.level.stale ? 0.5 : 1);
                cr.fill();
            }
            cr.$dispose();
        });
    }

    setLevel(level) { this.level = {...this.level, ...level}; this.queue_repaint(); }
});

function section(parent, title) {
    const actor = new St.BoxLayout({...vertical, style_class: 'am-section', x_expand: true});
    if (title) actor.add_child(label(title, 'am-section-heading'));
    parent.add_child(actor);
    return actor;
}

function rule(parent) { parent.add_child(new St.Widget({style_class: 'am-rule', x_expand: true})); }

function end(value, style = '') {
    const actor = label(value, style);
    actor.x_expand = true;
    actor.x_align = Clutter.ActorAlign.END;
    return actor;
}

const PERIODS = [['today', 'Today'], ['week', 'Week'], ['month', 'Month'], ['ytd', 'YTD']];

export class Dashboard {
    constructor(snapshot, visibleTools, settings, activate, compact = false) {
        this.actor = new St.BoxLayout({...vertical,
            style_class: `am-dashboard${compact ? ' am-dashboard-compact' : ''}`, x_expand: true});
        const head = new St.BoxLayout({x_expand: true});
        head.add_child(label('AI monitor', 'am-heading'));
        const sessions = snapshot?.sessions ?? [];
        const working = sessions.filter(row => row.state === 'working').length;
        const waiting = sessions.filter(row => attention(row, settings.get_boolean('notify-ready'))).length;
        head.add_child(end(`${working} working${waiting ? ` · ${waiting} waiting` : ''}`, waiting ? 'am-attention' : 'am-muted'));
        this.actor.add_child(head);
        if (!snapshot) {
            this.actor.add_child(label('Service unavailable. Run ./status.sh to diagnose.', 'am-muted'));
            return;
        }
        const cards = new St.BoxLayout({...(compact ? vertical : {}), style_class: 'am-quota-cards', x_expand: true});
        this.actor.add_child(cards);
        this.cards = {};
        for (const tool of visibleTools) this.cards[tool] = this.quota(cards, tool, snapshot.usage.limits?.[tool]);
        rule(this.actor);
        const sessionSection = section(this.actor, 'Sessions');
        const list = new St.BoxLayout({...vertical, style_class: 'am-session-list', x_expand: true});
        sessionSection.add_child(list);
        this.sessionButtons = new Map();
        for (const [index, tool] of visibleTools.entries()) {
            const own = sessions.filter(row => row.tool === tool);
            const heading = new St.BoxLayout({x_expand: true,
                style_class: `am-session-group${index > 0 ? ' am-session-group-spaced' : ''}`});
            heading.add_child(label(TITLES[tool], 'am-tool-heading'));
            list.add_child(heading);
            if (!own.length) list.add_child(label('No active sessions', 'am-muted'));
            for (const row of own) {
                const waiting = attention(row, settings.get_boolean('notify-ready'));
                const status = sessionStatus(row);
                const button = new St.Button({style_class: `am-session-button${waiting ? ' am-session-needs-input' : ''}`,
                    can_focus: true, x_expand: true, accessible_name: `${TITLES[tool]}: ${row.title}, ${status}`});
                const content = new St.BoxLayout({style_class: 'am-session-content', x_expand: true});
                const identity = new St.BoxLayout({...vertical, x_expand: true});
                identity.add_child(label(row.title, 'am-session-title'));
                if (row.project) {
                    const path = label(row.project, 'am-session-project');
                    path.clutter_text.line_wrap = true;
                    path.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
                    path.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
                    identity.add_child(path);
                }
                content.add_child(identity);
                const state = new St.BoxLayout({...vertical, x_align: Clutter.ActorAlign.END});
                const stateLabel = end(status, `${waiting ? 'am-attention' : 'am-session-state'} am-session-status`);
                stateLabel.clutter_text.line_wrap = true;
                stateLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
                stateLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
                state.add_child(stateLabel);
                state.add_child(end(duration(Date.now() / 1000 - row.since), 'am-session-project'));
                content.add_child(state);
                button.set_child(content);
                button.connect('clicked', () => activate(row.id));
                list.add_child(button);
                this.sessionButtons.set(row.id, button);
            }
        }
        rule(this.actor);
        this.tokenCells = {};
        this.tokenTable(section(this.actor, 'Tokens'), snapshot.usage, visibleTools,
            settings.get_boolean('show-cache-info'), compact);
        if (snapshot.collectorError) this.actor.add_child(label(snapshot.collectorError, 'am-muted'));
    }

    quota(parent, tool, limits) {
        const card = section(parent);
        card.add_style_class_name('am-quota-card');
        const heading = new St.BoxLayout({x_expand: true});
        heading.add_child(label(TITLES[tool], 'am-card-title'));
        if (limits?.plan) heading.add_child(end(limits.plan, 'am-plan'));
        card.add_child(heading);
        const meters = [];
        for (const window of limits?.windows ?? []) {
            const level = budget({...limits, limitReached: false, windows: [window]});
            const row = new St.BoxLayout({x_expand: true});
            row.add_child(label(windowTitle(window.minutes), 'am-window-name'));
            const value = end(`${Math.round(window.usedPercent)}%`, 'am-window-percent');
            row.add_child(value);
            card.add_child(row);
            const meter = new Meter();
            meter.setLevel({fraction: window.usedPercent / 100, color: level.color, stale: level.stale});
            card.add_child(meter);
            meters.push(meter);
            const seconds = window.resetsAt - Date.now() / 1000;
            card.add_child(end(window.resetsAt ? (seconds > 0 ? `Resets in ${duration(seconds)}` : 'Awaiting refresh') : 'Reset time unavailable', 'am-reset'));
        }
        if (!meters.length) card.add_child(label('Usage unavailable', 'am-muted'));
        if (tool === 'codex' && Number.isInteger(limits?.resetsAvailable) && limits.resetsAvailable >= 0) {
            const count = limits.resetsAvailable;
            card.add_child(label(`${count} ${count === 1 ? 'reset' : 'resets'} left`, 'am-resets'));
            if (count > 0 && limits.resetExpiresAt) {
                const seconds = limits.resetExpiresAt - Date.now() / 1000;
                card.add_child(label(seconds > 0 ? `Next expires in ${duration(seconds)}` :
                    'Reset expiry awaiting refresh', 'am-reset'));
            }
        }
        if (limits?.error) card.add_child(label(limits.error, 'am-provider-error'));
        else if (budget(limits).stale && meters.length) card.add_child(label('Last known usage', 'am-reset'));
        return {actor: card, meters};
    }

    tokenTable(parent, usage, visibleTools, showCacheInfo = false, compact = false) {
        const layout = new Clutter.GridLayout({column_spacing: compact ? 8 : 18, row_spacing: 8});
        parent.add_child(new St.Widget({layout_manager: layout, x_expand: true}));
        PERIODS.forEach(([, title], column) => layout.attach(end(title, 'am-table-heading'), column + 1, 0, 1, 1));
        let rowIndex = 1;
        const row = (key, title, values, strong = false) => {
            // Keep a single highlight behind the entire row without changing
            // the shared column widths. Labels are not reactive, so the surface
            // also receives hover events beneath the numbers.
            const background = new St.Widget({style_class: 'am-token-row',
                reactive: true, track_hover: true, x_expand: true, y_expand: true});
            layout.attach(background, 0, rowIndex, PERIODS.length + 1, 1);
            layout.attach(label(title, strong ? 'am-table-total' : 'am-table-label'), 0, rowIndex, 1, 1);
            this.tokenCells[key] = values.map((value, column) => {
                const cell = end(value, strong ? 'am-table-number am-table-total' : 'am-table-number');
                layout.attach(cell, column + 1, rowIndex, 1, 1);
                return cell;
            });
            rowIndex++;
        };
        const hasUsage = Boolean(Object.keys(usage.tokens ?? {}).length);
        const values = period => TOOLS.map(tool => usage.tokens?.[tool]?.[period] ?? {});
        for (const tool of visibleTools)
            row(tool, TITLES[tool], PERIODS.map(([period]) => hasUsage ? tokens(tokenTotal(usage.tokens?.[tool]?.[period])) : '—'));
        row('total', 'Total', PERIODS.map(([period]) => hasUsage ? tokens(values(period).reduce((sum, v) => sum + tokenTotal(v), 0)) : '—'), true);
        if (showCacheInfo) {
            row('cached', 'Cached', PERIODS.map(([period]) => hasUsage ? tokens(values(period).reduce((sum, v) => sum + (v.cacheRead ?? 0), 0)) : '—'));
            row('hit', 'Cache hit', PERIODS.map(([period]) => {
                const share = cacheShare(values(period));
                return share === null ? '—' : `${share.toFixed(1)}%`;
            }));
        }
        for (const tool of visibleTools) if (usage.errors?.[tool]) parent.add_child(label(usage.errors[tool], 'am-provider-error'));
    }
}
