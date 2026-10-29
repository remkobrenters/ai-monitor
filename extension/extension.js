import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {AlertTracker, SoundReminders, TOOLS, TITLES, STATES, attention, budget, counts, tokenTotal} from './logic.js';
import {Dashboard, Meter, label, vertical} from './dashboard.js';
import {Sound} from './sound.js';
import {SOUND_REPEATS} from './sound-options.js';

export default class AiMonitor extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._tracker = new AlertTracker();
        this._soundReminders = new SoundReminders(() => GLib.get_monotonic_time() / 1e6);
        this._snapshot = null;
        this._buttons = {};
        this._closed = false;
        this._choosingWindow = false;
        this._panelGeometryStyle = null;
        this._sound = new Sound(this._settings, message => this._error(message));
        this._writes = [];
        this._writing = false;
        this._lastFingerprint = '';
        this._button = new PanelMenu.Button(0.5, 'ai-monitor');
        const panel = new St.BoxLayout({style_class: 'am-panel-tools', y_expand: true});
        this._button.add_child(panel);
        for (const tool of TOOLS) {
            const button = {};
            button.frame = new St.Bin({y_expand: true, clip_to_allocation: true});
            const box = new St.BoxLayout({style_class: 'am-panel', y_expand: true});
            button.actor = box;
            button.frame.set_child(box);
            const identity = new St.BoxLayout({...vertical, style_class: 'am-panel-identity',
                y_align: Clutter.ActorAlign.CENTER});
            button._name = label(TITLES[tool], 'am-panel-name');
            button._meter = new Meter({width: 44, height: 3, style_class: 'am-panel-meter', x_expand: false});
            identity.add_child(button._name);
            identity.add_child(button._meter);
            box.add_child(identity);
            button._percent = label('—', 'am-percent');
            box.add_child(button._percent);
            panel.add_child(button.frame);
            this._buttons[tool] = button;
        }
        this._button.connect('style-changed', () => this._syncPanelGeometry());
        this._button.menu.connect('open-state-changed', (_menu, open) => {
            if (open && !this._choosingWindow) this._menu();
            if (!open) this._choosingWindow = false;
        });
        Main.panel.addToStatusArea('ai-monitor', this._button, 1);
        this._syncPanelGeometry();
        this._settingsChanged = this._settings.connect('changed', () => {
            this._render();
            this._checkSound();
            if (this._button.menu.isOpen && !this._choosingWindow) this._menu();
        });
        this._pulseOn = true;
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
            this._pulseOn = !this._pulseOn;
            this._paintAttention();
            this._checkSound();
            this._send({op: 'active', value: !Main.sessionMode.isLocked});
            if (!this._process) this._start();
            return GLib.SOURCE_CONTINUE;
        });
        this._start();
        const changed = () => {
            this._visibilityRequest = null;
            this._sound.stop();
            this._checkSound();
        };
        this._focusChanged = global.display.connect('notify::focus-window', changed);
        this._tabChanged = Gio.DBus.session.signal_subscribe(null, 'org.gtk.Actions', 'Changed',
            null, null, Gio.DBusSignalFlags.NONE, (_connection, sender, path) => {
                const window = global.display.focus_window;
                if (sender === window?.get_gtk_unique_bus_name?.() &&
                    path === window?.get_gtk_window_object_path?.()) changed();
            });
        this._render();
        // PopupMenu.open() refuses empty menus; populate before the first click.
        this._menu();
    }

    _start() {
        const app = GLib.build_filenamev([GLib.get_user_data_dir(), 'ai-monitor', 'entry.py']);
        try {
            const process = Gio.Subprocess.new(['/usr/bin/python3', app, 'watch'],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
            this._process = process;
            this._cancel = new Gio.Cancellable();
            this._input = process.get_stdin_pipe();
            this._writes = [];
            this._writing = false;
            const stream = new Gio.DataInputStream({base_stream: process.get_stdout_pipe()});
            this._read(process, stream, this._cancel);
            this._send({op: 'active', value: !Main.sessionMode.isLocked});
            process.wait_async(null, (source, result) => {
                try { source.wait_finish(result); } catch { /* Shell may be shutting down. */ }
                if (this._process !== process) return;
                this._cancel?.cancel();
                this._process = null;
                this._snapshot = null;
                this._render();
            });
        } catch {
            this._process = null;
        }
    }

    _read(process, stream, cancel) {
        stream.read_line_async(GLib.PRIORITY_DEFAULT, cancel, (source, result) => {
            if (this._closed || this._process !== process) return;
            try {
                const [line] = source.read_line_finish_utf8(result);
                if (line === null) return;
                const message = JSON.parse(line);
                if (message.type === 'snapshot') this._update(message);
                else if (message.type === 'focus') this._focus(message);
                else if (message.type === 'error') this._error(message.error);
                else if (message.type === 'offline') {
                    this._snapshot = null;
                    this._render();
                }
            } catch (error) {
                if (!cancel.is_cancelled()) console.error(`ai-monitor: ${error.message}`);
            }
            if (!cancel.is_cancelled()) this._read(process, stream, cancel);
        });
    }

    _send(message) {
        if (!this._process || this._closed || this._writes.length > 20) return;
        this._writes.push(new TextEncoder().encode(`${JSON.stringify(message)}\n`));
        this._flush();
    }

    _flush() {
        if (this._writing || !this._writes.length || !this._process) return;
        const process = this._process;
        this._writing = true;
        const bytes = this._writes.shift();
        this._input.write_all_async(bytes, GLib.PRIORITY_DEFAULT, this._cancel, (source, result) => {
            try { source.write_all_finish(result); } catch { /* Reconnect on the next timer. */ }
            if (this._process !== process) return;
            this._writing = false;
            this._flush();
        });
    }

    _update(snapshot) {
        this._snapshot = snapshot;
        const fresh = this._tracker.update(snapshot.sessions, {notifyReady: this._settings.get_boolean('notify-ready'),
            locked: Main.sessionMode.isLocked});
        // Coalesce desktop banners; sound resolves the active terminal tab below.
        const visible = fresh.filter(row => !this._isVisible(row));
        if (visible.length) {
            if (this._settings.get_boolean('notifications-enabled')) {
                const first = visible[0];
                Main.notify(visible.length > 1 ? `${visible.length} agents need attention` :
                    `${TITLES[first.tool]} · ${first.title}`, visible.length > 1 ?
                    visible.map(row => `${TITLES[row.tool]}: ${row.title}`).join('\n') : STATES[first.state]);
            }
        }
        this._checkSound();
        this._render();
        const fingerprint = JSON.stringify([snapshot.sessions.map(({updated, ...row}) => row), snapshot.usage.tokens, snapshot.usage.limits, snapshot.collectorError]);
        if (fingerprint !== this._lastFingerprint) {
            if (this._button.menu.isOpen && !this._choosingWindow) this._menu();
            this._lastFingerprint = fingerprint;
        }
    }

    _isVisible(row, screen = null) {
        const window = global.display.focus_window;
        if (!window) return false;
        return (row.views ?? [{windowId: row.windowId}]).some(view => {
            // A window ID alone cannot distinguish GNOME Terminal's tabs.
            if (view.gnomeTerminal)
                return screen?.window === window && screen.id === view.gnomeTerminal.screenId &&
                    (screen.bus === view.gnomeTerminal.busName || screen.app === view.gnomeTerminal.busName);
            if (!view.windowId) return false;
            const description = window.get_description?.().split(' ')[0];
            return description === view.windowId || description === `0x${Number(view.windowId).toString(16)}`;
        });
    }

    _checkSound() {
        if (this._closed) return;
        const window = global.display.focus_window;
        const bus = window?.get_gtk_unique_bus_name?.();
        const app = window?.get_gtk_application_id?.();
        const path = window?.get_gtk_window_object_path?.();
        const relevant = this._snapshot?.sessions.some(row => attention(row, this._settings.get_boolean('notify-ready')) &&
            row.views?.some(view => view.gnomeTerminal &&
                [bus, app].includes(view.gnomeTerminal.busName)));
        if (!bus || !path || !relevant || !this._settings.get_boolean('sound-enabled')) {
            this._visibilityRequest = null;
            this._applySound();
            return;
        }
        if (this._visibilityRequest?.window === window) return;
        const request = {window};
        this._visibilityRequest = request;
        const current = () => !this._closed && this._visibilityRequest === request &&
            global.display.focus_window === window;
        const finish = screen => {
            if (!current()) return;
            this._visibilityRequest = null;
            this._applySound(screen);
        };
        const call = (objectPath, iface, method, params, callback) => {
            Gio.DBus.session.call(bus, objectPath, iface, method, params, null,
                Gio.DBusCallFlags.NONE, 1000, this._cancel, (source, result) => {
                    if (!current()) return;
                    try { callback(source.call_finish(result).deep_unpack()); }
                    catch { finish(null); }
                });
        };
        const describe = callback => call(path, 'org.gtk.Actions', 'Describe',
            new GLib.Variant('(s)', ['active-tab']), result => {
                const state = result[0]?.[2]?.[0];
                const index = state?.deep_unpack ? state.deep_unpack() : state;
                if (!Number.isInteger(index) || index < 0) finish(null);
                else callback(index);
            });
        // GNOME Terminal prepends screens while walking GTK's most-recently
        // focused windows and their notebook tabs, hence this reverse index.
        // Only IDs are read; never request result metadata/terminal contents.
        describe(index => call('/org/gnome/Terminal/SearchProvider', 'org.gnome.Shell.SearchProvider2',
            'GetInitialResultSet', new GLib.Variant('(as)', [[]]), ([screens]) => {
                const id = screens[screens.length - 1 - index];
                describe(confirmed => {
                    if (confirmed !== index) {
                        this._visibilityRequest = null; // Recheck on the next tick; do not chime during a tab switch.
                        return;
                    }
                    finish({window, bus, app, id});
                });
            }));
    }

    _applySound(screen = null) {
        const snapshot = this._snapshot;
        const rows = snapshot?.sessions ?? [];
        const notifyReady = this._settings.get_boolean('notify-ready');
        const available = Boolean(snapshot && (!Number.isFinite(snapshot.heartbeat) ||
            Math.abs(Date.now() / 1000 - snapshot.heartbeat) < 10));
        const enabled = this._settings.get_boolean('sound-enabled') && this._settings.get_uint('sound-volume') > 0;
        const locked = Main.sessionMode.isLocked;
        const suppressedIds = rows.filter(row => this._isVisible(row, screen)).map(row => row.id);
        const audible = rows.some(row => attention(row, notifyReady) && !suppressedIds.includes(row.id));
        if (!available || !enabled || locked || !audible) this._sound.stop();
        const interval = SOUND_REPEATS.find(option => option.id === this._settings.get_string('sound-repeat'))?.seconds ?? 0;
        if (this._soundReminders.update(rows, {interval, available, enabled, locked, notifyReady, suppressedIds}))
            this._sound.play();
    }

    _render() {
        if (this._closed) return;
        const available = TOOLS.filter(tool => this._snapshot?.tools?.includes(tool) ||
            this._snapshot?.sessions?.some(row => row.tool === tool) ||
            this._snapshot?.usage?.limits?.[tool]?.windows?.length ||
            Object.values(this._snapshot?.usage?.tokens?.[tool] ?? {}).some(value => tokenTotal(value) > 0));
        let total = 0;
        let waiting = 0;
        for (const tool of TOOLS) {
            const button = this._buttons[tool];
            const level = budget(this._snapshot?.usage?.limits?.[tool]);
            const count = counts(this._snapshot?.sessions ?? [], tool, this._settings.get_boolean('notify-ready'));
            button._meter.setLevel({fraction: (level.percent ?? 0) / 100, color: level.color, stale: level.stale});
            button._percent.text = level.percent === null ? '—' : `${Math.round(level.percent)}%${level.stale ? '*' : ''}`;
            button._percent.visible = this._settings.get_boolean('show-percent');
            button.needsAttention = count.waiting > 0;
            button.actor.visible = !available.length ? tool === 'codex' : available.includes(tool);
            button.frame.visible = button.actor.visible;
            total += count.total;
            waiting += count.waiting;
        }
        this._button.accessible_name = `ai-monitor: ${total} terminal sessions, ${waiting} needing attention`;
        this._paintAttention();
    }

    _paintAttention() {
        for (const button of Object.values(this._buttons)) {
            const highlighted = button.needsAttention &&
                (this._pulseOn || !this._settings.get_boolean('pulse-enabled'));
            if (highlighted) button.actor.add_style_class_name('am-panel-waiting');
            else button.actor.remove_style_class_name('am-panel-waiting');
        }
    }

    _syncPanelGeometry() {
        const node = this._button.get_theme_node();
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const radius = node.get_border_radius(St.Corner.TOPLEFT) / scale;
        const padding = Math.max(node.get_border_width(St.Side.LEFT), node.get_border_width(St.Side.RIGHT)) / scale;
        const style = `-natural-hpadding: ${padding}px; -minimum-hpadding: ${padding}px;`;
        if (this._panelGeometryStyle !== style) {
            this._panelGeometryStyle = style;
            this._button.set_style(style);
        }
        for (const button of Object.values(this._buttons)) {
            // St.Bin allocates its child inside this padding. Keep the colored
            // surface inside the panel border regardless of BoxLayout margins.
            const top = node.get_border_width(St.Side.TOP) / scale;
            const bottom = node.get_border_width(St.Side.BOTTOM) / scale;
            button.frame.set_style(`padding: ${top}px 0 ${bottom}px 0;`);
            button.actor.set_style(`border-radius: ${radius}px;`);
        }
    }

    _item(menu, value, style = '') {
        const item = new PopupMenu.PopupMenuItem(value, {reactive: false, can_focus: false});
        if (style) item.label.add_style_class_name(style);
        menu.addMenuItem(item);
        return item;
    }

    _clearMenu() {
        // This plain scroll view deliberately isn't a disabled PopupBaseMenuItem.
        // PopupMenu.removeAll() only destroys menu items, so remove our view explicitly.
        this._dashboardScroll?.destroy();
        this._dashboardScroll = null;
        this._dashboard = null;
        this._footer?.destroy();
        this._footer = null;
        this._settingsButton = null;
        this._button.menu.removeAll();
    }

    _menu() {
        const menu = this._button.menu;
        this._clearMenu();
        const visibleTools = TOOLS.filter(tool => this._buttons[tool].actor.visible);
        const monitor = Main.layoutManager.findMonitorForActor?.(this._button);
        const workArea = Main.layoutManager.getWorkAreaForMonitor(monitor?.index ?? Main.layoutManager.primaryIndex);
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const width = Math.max(280, Math.min(560, Math.floor((workArea.width ?? 1184) / scale) - 32));
        this._dashboard = new Dashboard(this._snapshot, visibleTools, this._settings, id => {
            menu.close();
            this._send({op: 'focus', id});
        }, width < 480);
        this._dashboard.actor.set_style(`width: ${width}px;`);
        const height = Math.max(120, Math.floor(workArea.height / scale) - 220);
        this._dashboardScroll = new St.ScrollView({style_class: 'am-dashboard-scroll',
            overlay_scrollbars: true, hscrollbar_policy: St.PolicyType.NEVER});
        this._dashboardScroll.set_style(`max-height: ${height}px;`);
        if (this._dashboardScroll.set_child) this._dashboardScroll.set_child(this._dashboard.actor);
        else this._dashboardScroll.add_actor(this._dashboard.actor);
        menu.box.add_child(this._dashboardScroll);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._footer = new St.BoxLayout({style_class: 'am-footer', x_expand: true});
        const silent = !this._settings.get_boolean('sound-enabled');
        const silentContent = new St.BoxLayout({style_class: 'am-footer-button-content'});
        silentContent.add_child(new St.Icon({icon_name: silent ? 'audio-volume-muted-symbolic' :
            'audio-volume-high-symbolic', icon_size: 16}));
        silentContent.add_child(label(width < 480 ? 'Silent' : 'Silent mode'));
        const silentButton = new St.Button({style_class: 'button flat am-footer-button',
            accessible_name: 'Silent mode', can_focus: true, toggle_mode: true, checked: silent,
            child: silentContent});
        silentButton.connect('clicked', () => {
            menu.close();
            this._settings.set_boolean('sound-enabled', silent);
        });
        this._footer.add_child(silentButton);
        const hasWaiting = Boolean(this._snapshot?.sessions.some(row => row.attention));
        const acknowledge = new St.Button({style_class: 'button flat am-footer-button',
            accessible_name: 'Acknowledge agents', can_focus: hasWaiting,
            reactive: hasWaiting, opacity: hasWaiting ? 255 : 128,
            child: label(width < 480 ? 'Acknowledge' : 'Acknowledge agents')});
        acknowledge.connect('clicked', () => {
            menu.close();
            this._send({op: 'ack'});
            this._sound.stop();
        });
        this._footer.add_child(acknowledge);
        this._settingsButton = new St.Button({style_class: 'icon-button flat am-settings-button',
            accessible_name: 'Settings', can_focus: true, x_expand: true, x_align: Clutter.ActorAlign.END,
            child: new St.Icon({icon_name: 'emblem-system-symbolic', icon_size: 16})});
        this._settingsButton.connect('clicked', () => { menu.close(); this.openPreferences(); });
        this._footer.add_child(this._settingsButton);
        menu.box.add_child(this._footer);
    }

    _focus(message) {
        const target = message.target;
        if (target.gnomeTerminal) {
            this._focusGnome(message);
            return;
        }
        this._focusWindow(message);
    }

    _focusGnome(message) {
        const {busName, screenId} = message.target.gnomeTerminal;
        const path = '/org/gnome/Terminal/SearchProvider';
        const iface = 'org.gnome.Shell.SearchProvider2';
        // Validate the exact screen before activation: all Ubuntu terminal windows share a PID.
        Gio.DBus.session.call(busName, path, iface, 'GetInitialResultSet',
            new GLib.Variant('(as)', [[]]), null, Gio.DBusCallFlags.NONE, 2000, this._cancel, (source, result) => {
                if (this._closed) return;
                try {
                    const [screens] = source.call_finish(result).deep_unpack();
                    if (!screens.includes(screenId)) {
                        this._error('The original terminal tab has closed.');
                        return;
                    }
                    Gio.DBus.session.call(busName, path, iface, 'ActivateResult',
                        new GLib.Variant('(sasu)', [screenId, [], global.get_current_time()]),
                        null, Gio.DBusCallFlags.NONE, 2000, this._cancel, (connection, activation) => {
                            if (this._closed) return;
                            try {
                                connection.call_finish(activation);
                                this._send({op: 'ack', id: message.id});
                            } catch {
                                this._error('Could not activate this Ubuntu Terminal tab.');
                            }
                        });
                } catch {
                    this._focusWindow(message);
                }
            });
    }

    _focusWindow(message) {
        const target = message.target;
        const windows = global.get_window_actors().map(actor => actor.meta_window);
        let matches = [];
        if (target.windowId) matches = windows.filter(window => {
            const description = window.get_description().split(' ')[0];
            return description === target.windowId || description === `0x${Number(target.windowId).toString(16)}`;
        });
        if (!matches.length) matches = windows.filter(window => target.pids.includes(window.get_pid()));
        if (matches.length === 1) {
            Main.activateWindow(matches[0]);
            this._send({op: 'ack', id: message.id});
        } else if (matches.length > 1) {
            const menu = this._button.menu;
            this._choosingWindow = true;
            this._clearMenu();
            this._item(menu, 'Choose the terminal window', 'am-heading');
            for (const window of matches) {
                const item = new PopupMenu.PopupMenuItem(window.get_title() || 'Terminal');
                item.connect('activate', () => {
                    Main.activateWindow(window);
                    this._send({op: 'ack', id: message.id});
                });
                menu.addMenuItem(item);
            }
            menu.open();
        } else {
            this._error(target.tmux ? 'Pane selected. Focus the terminal attached to tmux to continue.' :
                'Could not identify a desktop window for this terminal. Open it manually.');
        }
    }

    _error(message) {
        if (!this._closed) Main.notify('ai-monitor', message);
    }

    disable() {
        this._closed = true;
        this._visibilityRequest = null;
        if (this._focusChanged) global.display.disconnect(this._focusChanged);
        if (this._tabChanged) Gio.DBus.session.signal_unsubscribe(this._tabChanged);
        if (this._timer) GLib.source_remove(this._timer);
        this._cancel?.cancel();
        this._process?.force_exit();
        this._process = null;
        this._sound?.destroy();
        if (this._settingsChanged) this._settings.disconnect(this._settingsChanged);
        this._clearMenu();
        this._button.destroy();
        this._buttons = {};
        this._snapshot = null;
    }
}
