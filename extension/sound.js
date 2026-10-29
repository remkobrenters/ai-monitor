import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {soundFile} from './sound-options.js';

export class Sound {
    constructor(settings, error, extensionPath) {
        this.settings = settings;
        this.error = error;
        this.extensionPath = extensionPath ?? GLib.build_filenamev([
            GLib.get_user_data_dir(), 'gnome-shell', 'extensions', 'ai-monitor@local']);
        this.process = null;
        this.timer = null;
        this.last = -Infinity;
        this.changed = settings.connect('changed', () => this.stop());
    }

    play({preview = false} = {}) {
        if ((!preview && !this.settings.get_boolean('sound-enabled')) || !this.settings.get_uint('sound-volume')) return;
        const now = GLib.get_monotonic_time() / 1e6;
        if (!preview && now - this.last < 3) return;
        this.last = now;
        const path = soundFile(this.settings.get_uint('sound-effect'), this.settings.get_string('sound-file'), this.extensionPath);
        try {
            if (!GLib.path_is_absolute(path) || !GLib.file_test(path, GLib.FileTest.IS_REGULAR))
                throw new Error('Choose a readable local sound file in Settings.');
            const player = GLib.find_program_in_path('gst-play-1.0');
            if (!player) throw new Error('Attention sounds need gstreamer1.0-plugins-base-apps.');
            this.stop();
            const process = Gio.Subprocess.new([player, '--quiet', '--no-interactive', '--flags=audio+soft-volume',
                `--volume=${this.settings.get_uint('sound-volume') / 100}`, Gio.File.new_for_path(path).get_uri()],
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
            this.process = process;
            this.timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 15, () => {
                this.timer = null;
                this.stop();
                return GLib.SOURCE_REMOVE;
            });
            process.wait_check_async(null, (source, result) => {
                let failed = false;
                try { source.wait_check_finish(result); } catch { failed = true; }
                if (this.process !== source) return;
                this.process = null;
                if (this.timer) GLib.source_remove(this.timer);
                this.timer = null;
                if (failed) this.error('Could not play the selected sound. Check its format and output device.');
            });
        } catch (error) {
            this.error(error.message);
        }
    }

    stop() {
        if (this.timer) GLib.source_remove(this.timer);
        this.timer = null;
        this.process?.force_exit();
        this.process = null;
    }

    destroy() {
        this.stop();
        this.settings.disconnect(this.changed);
    }
}
