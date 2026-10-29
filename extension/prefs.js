import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';
import {SOUND_EFFECTS, SOUND_REPEATS} from './sound-options.js';
import {Sound} from './sound.js';

export default class AiMonitorPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage({title: 'ai-monitor', icon_name: 'utilities-terminal-symbolic'});
        window.add(page);
        const panel = new Adw.PreferencesGroup({title: 'Panel',
            description: 'Claude and Codex share one menu. Colored underlines show the most consumed allowance: green below 70%, orange near depletion, red when exhausted.'});
        page.add(panel);
        for (const [key, title] of [['show-percent', 'Show allowance percentage'], ['pulse-enabled', 'Blink the tool background when attention is needed']]) {
            const row = new Adw.SwitchRow({title});
            settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT);
            panel.add(row);
        }
        const tokens = new Adw.PreferencesGroup({title: 'Tokens'});
        const cacheInfo = new Adw.SwitchRow({title: 'Show cached token data',
            subtitle: 'Show cached token counts and cache-hit percentages'});
        settings.bind('show-cache-info', cacheInfo, 'active', Gio.SettingsBindFlags.DEFAULT);
        tokens.add(cacheInfo);
        page.add(tokens);
        const alerts = new Adw.PreferencesGroup({title: 'Attention', description:
            'Approvals and questions need attention. Finished turns can alert too. Acknowledging silences a session until its next state change.'});
        page.add(alerts);
        for (const [key, title] of [['notify-ready', 'Alert when an agent finishes its turn'],
            ['notifications-enabled', 'Show desktop notifications']]) {
            const row = new Adw.SwitchRow({title});
            settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT);
            alerts.add(row);
        }
        const audio = new Adw.PreferencesGroup({title: 'Sound', description:
            'Choose one of three bundled effects, or a custom file. Message is the default.'});
        page.add(audio);
        const enabled = new Adw.SwitchRow({title: 'Attention sound'});
        settings.bind('sound-enabled', enabled, 'active', Gio.SettingsBindFlags.DEFAULT);
        audio.add(enabled);
        const repeat = new Adw.ComboRow({title: 'Repeat while waiting',
            model: Gtk.StringList.new(SOUND_REPEATS.map(option => option.name)),
            selected: Math.max(0, SOUND_REPEATS.findIndex(option => option.id === settings.get_string('sound-repeat')))});
        repeat.connect('notify::selected', () => settings.set_string('sound-repeat', SOUND_REPEATS[repeat.selected].id));
        audio.add(repeat);
        const effect = new Adw.ComboRow({title: 'Sound effect',
            model: Gtk.StringList.new([...SOUND_EFFECTS.map((sound, index) => `${sound.name}${index === 0 ? ' (default)' : ''}`), 'Custom file']),
            selected: settings.get_string('sound-file') ? 3 : settings.get_uint('sound-effect') - 1});
        audio.add(effect);
        let selecting = false;
        const select = index => { selecting = true; effect.selected = index; selecting = false; };
        const file = new Adw.ActionRow({title: 'Custom sound file',
            subtitle: settings.get_string('sound-file') || 'Using a bundled effect'});
        const choose = new Gtk.Button({label: 'Choose…', valign: Gtk.Align.CENTER});
        choose.connect('clicked', () => {
            const dialog = new Gtk.FileDialog({title: 'Choose an attention sound'});
            dialog.open(window, null, (source, result) => {
                try {
                    const selected = source.open_finish(result);
                    const path = selected.get_path();
                    if (path) {
                        settings.set_string('sound-file', path);
                        file.subtitle = path;
                        select(3);
                    }
                } catch { /* File selection was cancelled. */ }
                if (!settings.get_string('sound-file')) select(settings.get_uint('sound-effect') - 1);
            });
        });
        file.add_suffix(choose);
        const reset = new Gtk.Button({label: 'Use default sound', valign: Gtk.Align.CENTER});
        reset.connect('clicked', () => {
            settings.set_string('sound-file', '');
            settings.set_uint('sound-effect', 1);
            file.subtitle = 'Using a bundled effect';
            select(0);
        });
        file.add_suffix(reset);
        audio.add(file);
        effect.connect('notify::selected', () => {
            if (selecting) return;
            if (effect.selected < SOUND_EFFECTS.length) {
                settings.set_string('sound-file', '');
                settings.set_uint('sound-effect', effect.selected + 1);
                file.subtitle = 'Using a bundled effect';
            } else if (!settings.get_string('sound-file')) {
                choose.emit('clicked');
            }
        });
        const volume = new Adw.SpinRow({title: 'Sound volume (%)',
            adjustment: new Gtk.Adjustment({lower: 0, upper: 100, step_increment: 5,
                value: settings.get_uint('sound-volume')})});
        volume.connect('notify::value', () => settings.set_uint('sound-volume', Math.round(volume.value)));
        audio.add(volume);
        const player = new Sound(settings, message => window.add_toast(new Adw.Toast({title: message})));
        const previewRow = new Adw.ActionRow({title: 'Preview selected sound'});
        const preview = new Gtk.Button({label: 'Play', valign: Gtk.Align.CENTER});
        preview.connect('clicked', () => player.play({preview: true}));
        previewRow.add_suffix(preview);
        audio.add(previewRow);
        window.connect('close-request', () => { player.destroy(); return false; });
        const support = new Adw.PreferencesGroup({title: 'Terminal sessions', description:
            'Launch Claude/Codex normally after installing. Codex requires a one-time /hooks trust review. ' +
            'Optional names: ai-monitor run --name "Fix checkout" claude. ' +
            'Ubuntu Terminal selects the exact tab. tmux selects its pane and attached client. Other terminals focus their window. ' +
            'SSH sessions are not forwarded to this desktop.'});
        page.add(support);
        const footer = new Adw.PreferencesGroup();
        footer.add(new Gtk.LinkButton({label: 'View repository',
            uri: 'https://github.com/remkobrenters/ai-monitor', halign: Gtk.Align.CENTER}));
        page.add(footer);
    }
}
