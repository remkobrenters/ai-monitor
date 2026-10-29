import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

// Exercise the real extension against desktop boundaries without changing Shell.
async function fixture(orientation = false) {
    let nextId = 1;
    const timers = new Map();
    const notifications = [];
    const activations = [];
    const processes = [];
    const windows = [];
    const statusAreas = [];
    const dbusCalls = [];
    const screens = [];
    const desktop = {activeTab: 0, defer: false, pending: [], fail: false};
    const display = {focus_window: null, connect() { return 1; }, disconnect() {}};
    const sent = [];
    const geometry = {border: 6, radius: 36, scale: 2, width: 2160, height: 2160};
    const clock = {seconds: Date.now() / 1000};
    const preferences = {opened: 0};
    class Actor {
        constructor(props = {}) { this._init(props); }
        _init(props = {}) { Object.assign(this, props); this.children ??= []; this.signals ??= new Map(); this.clutter_text ??= {}; }
        connect(name, callback) { const id = nextId++; this.signals.set(id, [name, callback]); return id; }
        disconnect(id) { this.signals.delete(id); }
        emit(name, ...args) { for (const [signal, callback] of [...this.signals.values()]) if (signal === name) callback(this, ...args); }
        add_child(child) { this.children.push(child); }
        set_child(child) { this.children = [child]; }
        add_actor(child) { this.add_child(child); }
        destroy() { this.destroyed = true; }
        add_style_class_name(name) { this.style_class = [...new Set(`${this.style_class ?? ''} ${name}`.trim().split(/\s+/))].join(' '); }
        remove_style_class_name(name) { this.style_class = (this.style_class ?? '').split(/\s+/).filter(value => value !== name).join(' '); }
        set_style(style) { this.style = style; }
        get_theme_node() { return {get_border_width: () => geometry.border, get_border_radius: () => geometry.radius}; }
        queue_repaint() {}
    }
    class Menu extends Actor {
        constructor() { super(); this.box = this; }
        addMenuItem(item) { this.add_child(item); }
        removeAll() { this.children = []; }
        open() { if (!this.children.length || this.isOpen) return; this.isOpen = true; this.emit('open-state-changed', true); }
        close() { this.isOpen = false; this.emit('open-state-changed', false); }
    }
    class Button extends Actor {
        constructor() { super(); this.menu = new Menu(); }
    }
    class Item extends Actor {
        constructor(value = '', props = {}) {
            super(props);
            if (props.reactive === false || value?.reactive === false) this.add_style_class_name('popup-inactive-menu-item');
            this.label = new Actor({text: value}); this.add_child(this.label);
        }
    }
    class Section extends Menu {
        constructor() { super(); this.actor = new Actor(); this.actor.section = this; }
    }
    class Submenu extends Item {
        constructor(...args) { super(...args); this.menu = new Menu(); }
    }
    const settings = new Actor();
    settings.values = {'show-percent': true, 'show-cache-info': false, 'pulse-enabled': true, 'notify-ready': true,
        'notifications-enabled': true, 'sound-enabled': false, 'sound-file': '', 'sound-effect': 1,
        'sound-volume': 70, 'sound-repeat': 'once'};
    for (const type of ['boolean', 'double', 'string', 'uint']) {
        settings[`get_${type}`] = key => settings.values[key];
        settings[`set_${type}`] = (key, value) => { settings.values[key] = value; settings.emit('changed', key); };
    }
    class Extension {
        constructor() { this.dir = {get_child: value => value}; }
        getSettings() { return settings; }
        openPreferences() { preferences.opened++; }
    }
    const subprocess = {new(args) {
        const process = {args, wait_check_async() {}, get_stdin_pipe: () => ({write_all_async(bytes, priority, cancel, callback) {
            sent.push(JSON.parse(new TextDecoder().decode(bytes)));
            callback({write_all_finish() {}}, {});
        }}), get_stdout_pipe: () => ({}), wait_async() {}, force_exit() { this.killed = true; }};
        processes.push(process);
        return process;
    }};
    const main = {panel: {addToStatusArea(name) { statusAreas.push(name); }}, sessionMode: {isLocked: false},
        layoutManager: {primaryIndex: 0, getWorkAreaForMonitor: () => ({height: geometry.height, width: geometry.width})},
        notify: (...args) => notifications.push(args), activateWindow: window => activations.push(window)};
    const sources = {
        cairo: {default: {LineCap: {ROUND: 1}}},
        'gi://Clutter': {default: {ActorAlign: {START: 0, CENTER: 1, END: 2}, Orientation: {VERTICAL: 1}, BinLayout: Actor,
            GridLayout: class {constructor(props) {Object.assign(this, props); this.cells = [];} attach(...cell) {this.cells.push(cell);}}}},
        'gi://Gio': {default: {Subprocess: subprocess, FileIcon: {new: value => value},
            File: {new_for_path: path => ({get_uri: () => `file://${path}`})},
            SubprocessFlags: {STDIN_PIPE: 1, STDOUT_PIPE: 2, STDERR_SILENCE: 4, STDOUT_SILENCE: 8},
            Cancellable: class {cancel() { this.cancelled = true; } is_cancelled() { return Boolean(this.cancelled); }},
            DataInputStream: class {read_line_async() {}}, DBusCallFlags: {NONE: 0}, DBusSignalFlags: {NONE: 0},
            DBus: {session: {call(bus, objectPath, iface, method, params, type, flags, timeout, cancel, callback) {
                dbusCalls.push({bus, objectPath, iface, method, params});
                const respond = () => callback({call_finish: () => {
                    if (desktop.fail) throw new Error('unavailable');
                    return {deep_unpack: () => method === 'Describe' ?
                        [[true, 'i', [{deep_unpack: () => desktop.activeTab}]]] : [screens]};
                }}, {});
                if (desktop.defer) desktop.pending.push(respond);
                else respond();
            }, signal_subscribe() { return 1; }, signal_unsubscribe() {}}}}},
        'gi://GLib': {default: {get_user_data_dir: () => '/tmp/test', build_filenamev: p => p.join('/'),
            PRIORITY_DEFAULT: 0, SOURCE_CONTINUE: true, SOURCE_REMOVE: false,
            timeout_add_seconds: (_priority, _seconds, callback) => { const id = nextId++; timers.set(id, callback); return id; },
            source_remove: id => timers.delete(id), get_monotonic_time: () => clock.seconds * 1e6,
            path_is_absolute: path => path.startsWith('/'), file_test: () => true,
            FileTest: {IS_REGULAR: 1}, find_program_in_path: () => '/fake/gst-play-1.0',
            Variant: class {constructor(type, value) {this.type = type; this.value = value;}}}},
        'gi://GObject': {default: {registerClass: value => value}},
        'gi://Pango': {default: {WrapMode: {WORD_CHAR: 2}, EllipsizeMode: {NONE: 0}}},
        'gi://St': {default: {BoxLayout: class extends Actor {static list_properties() { return orientation ? [{name: 'orientation'}] : []; }},
            DrawingArea: Actor, Widget: Actor, Icon: Actor, Label: Actor, Bin: Actor, Button: Actor, ScrollView: Actor, PolicyType: {NEVER: 0},
            ThemeContext: {get_for_stage: () => ({scale_factor: geometry.scale})},
            Corner: {TOPLEFT: 0}, Side: {LEFT: 0, RIGHT: 1, TOP: 2, BOTTOM: 3}}},
        'resource:///org/gnome/shell/ui/main.js': main,
        'resource:///org/gnome/shell/ui/panelMenu.js': {Button},
        'resource:///org/gnome/shell/ui/popupMenu.js': {PopupMenuItem: Item, PopupBaseMenuItem: Item,
            PopupMenuSection: Section, PopupSeparatorMenuItem: Item, PopupSubMenuMenuItem: Submenu, PopupSwitchMenuItem: Item},
        'resource:///org/gnome/shell/extensions/extension.js': {Extension},
    };
    const context = vm.createContext({console, TextEncoder, TextDecoder, Date,
        global: {display, get_current_time: () => 12345,
            get_window_actors: () => windows.map(window => ({meta_window: window}))}});
    const modules = new Map();
    async function load(name) {
        if (modules.has(name)) return modules.get(name);
        let module;
        if (sources[name]) {
            const value = sources[name];
            module = new vm.SyntheticModule(Object.keys(value), function () {
                for (const [key, entry] of Object.entries(value)) this.setExport(key, entry);
            }, {context, identifier: name});
        } else {
            module = new vm.SourceTextModule(await fs.readFile(name, 'utf8'), {context, identifier: name});
        }
        modules.set(name, module);
        await module.link((specifier, parent) => load(specifier.startsWith('.') ? path.resolve(path.dirname(parent.identifier), specifier) : specifier));
        return module;
    }
    const module = await load(path.resolve('extension/extension.js'));
    await module.evaluate();
    const extension = new module.namespace.default();
    extension.enable();
    const snapshot = sessions => ({sessions, usage: {tokens: {}, limits: {}, errors: {}}, collectorError: ''});
    return {extension, settings, timers, notifications, activations, processes, windows, snapshot, main, statusAreas,
        dbusCalls, screens, sent, geometry, clock, preferences, desktop, display};
}

for (const orientation of [false, true]) {
    test(`panel renders, alerts, focuses and cleans up (${orientation ? 'new' : 'old'} orientation API)`, async () => {
        const f = await fixture(orientation);
        const e = f.extension;
        assert.equal(f.statusAreas.length, 1);
        assert.equal(f.statusAreas[0], 'ai-monitor');
        // Match GNOME's refusal to open empty menus, including the offline first click.
        e._button.menu.open();
        assert.equal(e._button.menu.isOpen, true);
        assert.ok(e._dashboard.actor.children.some(item => item.text?.includes('Service unavailable')));
        e._button.menu.close();
        const row = {id: 'a', tool: 'claude', title: 'shop', state: 'working', attention: false, revision: 1, since: Date.now() / 1000};
        e._update(f.snapshot([row]));
        assert.equal(e._buttons.claude._working, undefined);
        assert.equal(e._buttons.claude._waiting, undefined);
        assert.equal(e._buttons.claude.needsAttention, false);
        e._update(f.snapshot([{...row, state: 'approval', attention: true, revision: 2}]));
        assert.equal(e._buttons.claude.needsAttention, true);
        assert.equal(f.notifications.length, 1);
        e._update(f.snapshot([{...row, state: 'approval', attention: true, revision: 2}]));
        assert.equal(f.notifications.length, 1);
        e._button.menu.open();
        assert.ok(e._button.menu.children.includes(e._footer));
        assert.equal(e._button.menu.children[0], e._dashboardScroll);
        assert.equal(e._dashboardScroll.children[0], e._dashboard.actor);
        f.settings.set_boolean('show-percent', false);
        assert.equal(e._buttons.claude._percent.visible, false);
        const window = {get_pid: () => 123, get_description: () => '0x123 terminal', get_title: () => 'Terminal'};
        f.windows.push(window);
        e._focus({id: 'a', target: {pids: [123], windowId: '', tmux: false}});
        assert.equal(f.activations[0], window);
        e.disable();
        assert.equal(f.timers.size, 0);
        assert.ok(f.processes.every(process => process.killed));
    });
}

test('ambiguous terminals offer a window chooser', async () => {
    const f = await fixture();
    const e = f.extension;
    f.windows.push(...['One', 'Two'].map(title => ({get_pid: () => 123, get_title: () => title})));
    e._focus({id: 'a', target: {pids: [123], windowId: '', tmux: false}});
    assert.equal(f.activations.length, 0);
    assert.equal(e._button.menu.children[0].label.text, 'Choose the terminal window');
    e._button.menu.children[1].emit('activate');
    assert.equal(f.activations[0].get_title(), 'One');
    e.disable();
});

test('attention highlight follows the panel theme border and rounding at the current scale', async () => {
    const f = await fixture();
    const e = f.extension;
    assert.equal(e._buttons.codex.frame.style, 'padding: 3px 0 3px 0;');
    assert.equal(e._buttons.codex.frame.children[0], e._buttons.codex.actor);
    assert.equal(e._buttons.codex.frame.clip_to_allocation, true);
    assert.equal(e._buttons.codex.actor.style, 'border-radius: 18px;');
    f.geometry.radius = 50;
    f.geometry.border = 4;
    e._button.emit('style-changed');
    assert.equal(e._buttons.codex.actor.style, 'border-radius: 25px;');
    assert.equal(e._buttons.claude.frame.style, 'padding: 2px 0 2px 0;');
    e.disable();
});

test('sessions expand naturally above token totals without an inner scroll view', async () => {
    const f = await fixture();
    const rows = ['claude', 'codex'].flatMap(tool => [1, 2, 3].map(i => ({
        id: `${tool}-${i}`, tool, title: `Session ${i}`, state: 'working', since: 100,
    })));
    f.extension._update(f.snapshot(rows));
    f.extension._button.menu.open();
    const dashboard = f.extension._dashboard;
    const sessionsIndex = dashboard.actor.children.findIndex(actor => actor.children?.[0]?.text === 'Sessions');
    const tokensIndex = dashboard.actor.children.findIndex(actor => actor.children?.[0]?.text === 'Tokens');
    assert.ok(sessionsIndex >= 0 && sessionsIndex < tokensIndex);
    const list = dashboard.actor.children[sessionsIndex].children[1];
    assert.equal(list.style_class, 'am-session-list');
    assert.equal(dashboard.sessionButtons.size, 6);
    assert.equal(f.extension._dashboardScroll.style, 'max-height: 860px;');
    f.extension.disable();
});

test('a narrow scaled monitor keeps usage, sessions and footer actions in a compact menu', async () => {
    const f = await fixture();
    f.geometry.width = 640;
    f.geometry.height = 960;
    const data = f.snapshot([{id: 'a', tool: 'codex', title: 'Example', project: '/projects/example',
        state: 'working', since: Date.now() / 1000}]);
    data.tools = ['claude', 'codex'];
    f.extension._update(data);
    f.extension._button.menu.open();
    const dashboard = f.extension._dashboard;
    assert.equal(dashboard.actor.style, 'width: 288px;');
    assert.ok(dashboard.actor.style_class.includes('am-dashboard-compact'));
    assert.equal(dashboard.actor.children[1].vertical, true);
    assert.equal(dashboard.sessionButtons.size, 1);
    assert.ok(dashboard.tokenCells.total);
    assert.equal(f.extension._footer.children.length, 3);
    assert.equal(f.extension._footer.children[1].child.text, 'Acknowledge');
    assert.equal(f.extension._footer.children[1].accessible_name, 'Acknowledge agents');
    f.extension.disable();
});

test('one shared menu exposes usage and sessions for detected tools', async () => {
    const f = await fixture();
    const e = f.extension;
    const now = Date.now() / 1000;
    const data = f.snapshot([{id: 'a', tool: 'codex', title: 'worker', project: '/projects/example/worker', state: 'idle', since: now}]);
    data.tools = ['codex'];
    data.usage.tokens.codex = {today: {input: 100, cacheRead: 800, output: 100},
        week: {input: 500, cacheRead: 9000, output: 500}};
    data.usage.limits.codex = {fetchedAt: now, resetsAvailable: 2, resetExpiresAt: now + 3600,
        windows: [{minutes: 300, usedPercent: 100}, {minutes: 10080, usedPercent: 12}]};
    e._update(data);
    assert.equal(e._buttons.claude.actor.visible, false);
    assert.equal(e._buttons.codex.actor.visible, true);
    assert.equal(e._buttons.codex._working, undefined);
    e._button.menu.open();
    assert.equal(e._button.menu.isOpen, true);
    assert.ok(e._dashboard.tokenCells.total);
    assert.equal(e._dashboard.tokenCells.total.length, 4);
    assert.equal(e._dashboard.tokenCells.total[0].text, '1.0k');
    assert.equal(e._dashboard.tokenCells.total[1].text, '10.0k');
    assert.equal(e._dashboard.tokenCells.cached, undefined);
    assert.equal(e._dashboard.tokenCells.hit, undefined);
    f.settings.set_boolean('show-cache-info', true);
    assert.equal(e._dashboard.tokenCells.cached[0].text, '800');
    assert.equal(e._dashboard.tokenCells.hit[0].text, '88.9%');
    f.settings.set_boolean('show-cache-info', false);
    assert.equal(e._dashboard.tokenCells.hit, undefined);
    assert.equal(e._dashboard.tokenCells.cached, undefined);
    assert.equal(e._dashboard.tokenCells.total[0].text, '1.0k');
    assert.equal(e._dashboard.cards.codex.meters.length, 2);
    assert.ok(e._dashboard.cards.codex.actor.children.some(child => child.text === '2 resets left'));
    assert.ok(e._dashboard.cards.codex.actor.children.some(child => child.text === 'Next expires in 1h 0m'));
    assert.equal(e._buttons.codex._name.text, 'Codex');
    const session = e._dashboard.sessionButtons.get('a');
    assert.ok(session);
    const projectPath = session.children[0].children[0].children[1];
    assert.equal(projectPath.text, '/projects/example/worker');
    assert.equal(projectPath.clutter_text.line_wrap, true);
    session.emit('clicked');
    assert.equal(e._button.menu.isOpen, false);
    e.disable();
});

test('only the tool needing attention blinks purple, and acknowledgement clears it', async () => {
    const f = await fixture();
    const e = f.extension;
    const row = {id: 'a', tool: 'claude', title: 'Review', state: 'approval', attention: true,
        revision: 1, since: Date.now() / 1000};
    e._update(f.snapshot([row]));
    assert.ok(e._buttons.claude.actor.style_class.includes('am-panel-waiting'));
    assert.ok(!e._buttons.codex.actor.style_class.includes('am-panel-waiting'));
    const tick = [...f.timers.values()][0];
    tick();
    assert.ok(!e._buttons.claude.actor.style_class.includes('am-panel-waiting'));
    tick();
    assert.ok(e._buttons.claude.actor.style_class.includes('am-panel-waiting'));
    f.settings.set_boolean('pulse-enabled', false);
    tick();
    assert.ok(e._buttons.claude.actor.style_class.includes('am-panel-waiting'));
    e._update(f.snapshot([{...row, attention: false}]));
    assert.ok(!e._buttons.claude.actor.style_class.includes('am-panel-waiting'));
    e.disable();
});

test('bundled effects default to 1, can be previewed while muted, and stop on changes', async () => {
    const f = await fixture();
    const sound = f.extension._sound;
    sound.play();
    assert.equal(f.processes.length, 1); // Only the bridge: automatic audio is still opt-in.
    sound.play({preview: true});
    assert.ok(f.processes.at(-1).args.at(-1).endsWith('/sounds/sound_effect_1.mp3'));
    const first = f.processes.at(-1);
    f.settings.set_uint('sound-effect', 2);
    assert.equal(first.killed, true);
    sound.play({preview: true});
    assert.ok(f.processes.at(-1).args.at(-1).endsWith('/sounds/sound_effect_2.mp3'));
    f.settings.set_uint('sound-effect', 3);
    sound.play({preview: true});
    assert.ok(f.processes.at(-1).args.at(-1).endsWith('/sounds/sound_effect_3.mp3'));
    assert.ok(f.processes.at(-1).args.includes('--volume=0.7'));
    f.extension.disable();
    assert.equal(f.timers.size, 0);
    assert.ok(f.processes.every(process => process.killed));
});

test('repeated working events preserve the open dashboard and keyboard targets', async () => {
    const f = await fixture();
    const row = {id: 'a', tool: 'codex', title: 'Worker', state: 'working', since: 100, updated: 100,
        subagentCount: 3, waitingForSubagents: true};
    f.extension._update(f.snapshot([row]));
    f.extension._button.menu.open();
    const dashboard = f.extension._dashboard;
    assert.equal(dashboard.sessionButtons.size, 1);
    assert.ok(dashboard.sessionButtons.get('a').accessible_name.includes('Waiting for 3 subagents to finish'));
    f.extension._update(f.snapshot([{...row, updated: 102}]));
    assert.equal(f.extension._dashboard, dashboard);
    f.extension.disable();
});

test('Ubuntu Terminal uses screen identity to select the exact tab despite a shared PID', async () => {
    const f = await fixture();
    const screenId = '12345678-abcd-abcd-abcd-123456789abc';
    f.screens.push(screenId);
    f.windows.push(...['Wrong window', 'Other wrong window'].map(title => ({get_pid: () => 123, get_title: () => title})));
    f.extension._focus({id: 'right-session', target: {pids: [123], windowId: '', tmux: false,
        gnomeTerminal: {busName: ':1.77', screenId}}});
    assert.equal(f.activations.length, 0);
    assert.equal(f.dbusCalls.length, 2);
    assert.equal(f.dbusCalls[0].method, 'GetInitialResultSet');
    assert.equal(f.dbusCalls[1].method, 'ActivateResult');
    assert.equal(f.dbusCalls[1].params.type, '(sasu)');
    assert.equal(f.dbusCalls[1].params.value[0], screenId);
    assert.equal(f.dbusCalls[1].bus, ':1.77');
    assert.ok(f.sent.some(message => message.op === 'ack' && message.id === 'right-session'));
    f.extension.disable();
});

test('a closed Ubuntu Terminal screen never activates another matching window', async () => {
    const f = await fixture();
    f.windows.push({get_pid: () => 123});
    f.extension._focus({id: 'closed', target: {pids: [123], gnomeTerminal: {busName: ':1.77', screenId: 'closed'}}});
    assert.equal(f.dbusCalls.length, 1);
    assert.equal(f.activations.length, 0);
    assert.ok(!f.sent.some(message => message.op === 'ack'));
    f.extension.disable();
});

test('focused Ubuntu tab suppresses initial and repeat sounds while another tab still alerts', async () => {
    const f = await fixture();
    const e = f.extension;
    f.settings.set_boolean('sound-enabled', true);
    f.settings.set_string('sound-repeat', 'minute');
    e._update(f.snapshot([]));
    f.display.focus_window = {
        get_gtk_unique_bus_name: () => ':1.77', get_gtk_application_id: () => 'org.gnome.Terminal',
        get_gtk_window_object_path: () => '/org/gnome/Terminal/window/1', get_description: () => '0x123',
    };
    // Reverse notebook order, with the other window's tabs before this window.
    f.screens.push('other-window', 'second-tab', 'first-tab');
    const row = {id: 'a', tool: 'codex', title: 'Worker', state: 'question', attention: true,
        revision: 1, views: [{windowId: '0x123', gnomeTerminal: {busName: ':1.77', screenId: 'first-tab'}}]};
    e._update(f.snapshot([row]));
    assert.equal(f.processes.length, 1);
    f.clock.seconds += 61;
    f.timers.get(e._timer)();
    assert.equal(f.processes.length, 1);
    const other = {...row, id: 'b', views: [{windowId: '0x123',
        gnomeTerminal: {busName: ':1.77', screenId: 'second-tab'}}]};
    e._update(f.snapshot([row, other]));
    assert.equal(f.processes.length, 2, 'another tab can alert despite the shared window ID');
    f.desktop.activeTab = 1;
    e._update(f.snapshot([other]));
    assert.equal(f.processes[1].killed, true, 'entering the waiting tab stops playback');
    f.clock.seconds += 120;
    f.timers.get(e._timer)();
    assert.equal(f.processes.length, 2);
    assert.ok(f.dbusCalls.every(call => ['Describe', 'GetInitialResultSet'].includes(call.method)));
    assert.equal(f.activations.length, 0);
    e.disable();
});

test('an asynchronous foreground check cannot suppress or chime using an old focused window', async () => {
    const f = await fixture();
    const e = f.extension;
    f.settings.set_boolean('sound-enabled', true);
    e._update(f.snapshot([]));
    f.display.focus_window = {get_gtk_unique_bus_name: () => ':1.77',
        get_gtk_window_object_path: () => '/org/gnome/Terminal/window/1'};
    f.desktop.defer = true;
    const row = {id: 'a', tool: 'claude', title: 'Worker', state: 'question', attention: true,
        revision: 1, views: [{gnomeTerminal: {busName: ':1.77', screenId: 'first-tab'}}]};
    e._update(f.snapshot([row]));
    assert.equal(f.processes.length, 1, 'no sound before the tab check finishes');
    f.display.focus_window = null;
    f.desktop.pending.shift()();
    assert.equal(f.processes.length, 1, 'the stale callback is ignored');
    f.timers.get(e._timer)();
    assert.equal(f.processes.length, 2, 'the current foreground is used on the next check');
    e.disable();
});

test('tmux pane visibility and ordinary X11 windows suppress only the focused terminal', async () => {
    const f = await fixture();
    const e = f.extension;
    f.settings.set_boolean('sound-enabled', true);
    e._update(f.snapshot([]));
    f.display.focus_window = {get_description: () => '0x7b terminal'};
    const row = {id: 'a', tool: 'codex', title: 'Worker', state: 'approval', attention: true, revision: 1,
        windowId: '999', views: [{windowId: '123'}]};
    e._update(f.snapshot([row]));
    assert.equal(f.processes.length, 1);
    e._update(f.snapshot([{...row, id: 'b', views: []}]));
    assert.equal(f.processes.length, 2, 'an inactive tmux pane is not suppressed by the server window');
    e.disable();
});

test('the panel timer repeats sound while waiting, without repeating desktop banners', async () => {
    const f = await fixture();
    const e = f.extension;
    f.settings.set_boolean('sound-enabled', true);
    f.settings.set_string('sound-repeat', 'minute');
    e._update(f.snapshot([]));
    const row = {id: 'a', tool: 'codex', title: 'Worker', state: 'approval', attention: true,
        revision: 1, since: Date.now() / 1000};
    e._update(f.snapshot([row]));
    assert.equal(f.processes.length, 2); // Bridge and first sound.
    const tick = f.timers.get(e._timer);
    f.clock.seconds += 59;
    tick();
    assert.equal(f.processes.length, 2);
    f.clock.seconds += 1;
    tick();
    assert.equal(f.processes.length, 3);
    assert.equal(f.notifications.length, 1);
    e._update(f.snapshot([{...row, attention: false}]));
    f.clock.seconds += 300;
    tick();
    assert.equal(f.processes.length, 3);
    e.disable();
    assert.equal(f.timers.size, 0);
});

test('one footer row toggles silent mode, acknowledges agents, and opens settings', async () => {
    const f = await fixture();
    const e = f.extension;
    e._button.menu.open();
    assert.equal(e._button.menu.children.some(item => item.label?.text === 'Attention sound'), false);
    assert.equal(e._footer.children.length, 3);
    assert.equal(e._footer.children[0].accessible_name, 'Silent mode');
    assert.equal(e._footer.children[0].checked, true);
    assert.equal(e._footer.children[1].child.text, 'Acknowledge agents');
    assert.equal(e._footer.children[1].reactive, false);
    assert.equal(e._footer.children[2], e._settingsButton);
    e._footer.children[0].emit('clicked');
    assert.equal(e._button.menu.isOpen, false);
    assert.equal(f.settings.get_boolean('sound-enabled'), true);
    e._button.menu.open();
    assert.equal(e._footer.children[0].checked, false);
    e._footer.children[0].emit('clicked');
    assert.equal(f.settings.get_boolean('sound-enabled'), false);
    e._update(f.snapshot([{id: 'a', tool: 'codex', title: 'Worker', state: 'question', attention: true,
        revision: 1, since: Date.now() / 1000}]));
    e._button.menu.open();
    assert.equal(e._footer.children[1].reactive, true);
    e._footer.children[1].emit('clicked');
    assert.equal(e._button.menu.isOpen, false);
    assert.ok(f.sent.some(message => message.op === 'ack' && !message.id));
    e._button.menu.open();
    assert.equal(e._settingsButton.accessible_name, 'Settings');
    assert.equal(e._settingsButton.child.icon_name, 'emblem-system-symbolic');
    e._settingsButton.emit('clicked');
    assert.equal(e._button.menu.isOpen, false);
    assert.equal(f.preferences.opened, 1);
    e.disable();
});
