import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class SchemaTests(unittest.TestCase):
    def test_real_gsettings_schema_defaults_and_ranges_without_desktop(self):
        source = Path(__file__).resolve().parents[1] / "extension/schemas/org.gnome.shell.extensions.ai-monitor.gschema.xml"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / source.name).write_bytes(source.read_bytes())
            subprocess.run(["glib-compile-schemas", "--strict", temporary], check=True)
            script = directory / "settings.js"
            script.write_text('''import Gio from 'gi://Gio';
const settings = new Gio.Settings({schema_id: 'org.gnome.shell.extensions.ai-monitor'});
if (settings.get_boolean('sound-enabled')) throw new Error('Sound must be opt-in');
if (settings.get_boolean('show-cache-info')) throw new Error('Cached data must default off');
if (!settings.set_boolean('show-cache-info', true) || !settings.get_boolean('show-cache-info'))
    throw new Error('Cannot enable cached data');
if (settings.get_uint('sound-effect') !== 1) throw new Error('Effect 1 must be the default');
if (!settings.set_uint('sound-effect', 3)) throw new Error('Cannot select effect 3');
if (settings.get_string('sound-repeat') !== 'once') throw new Error('Sound must default to Once');
for (const value of ['minute', 'five-minutes', 'once']) {
    if (!settings.set_string('sound-repeat', value) || settings.get_string('sound-repeat') !== value)
        throw new Error('Cannot configure sound repeat');
}
if (!settings.get_boolean('notify-ready')) throw new Error('Ready alerts default on');
if (settings.get_uint('sound-volume') !== 70) throw new Error('Unexpected volume');
if (!settings.set_uint('sound-volume', 25)) throw new Error('Cannot set volume');
if (settings.get_uint('sound-volume') !== 25) throw new Error('Volume was not retained');
''')
            result = subprocess.run(["gjs", "-m", str(script)], capture_output=True, text=True,
                                    env={**os.environ, "GSETTINGS_SCHEMA_DIR": temporary, "GSETTINGS_BACKEND": "memory"})
            self.assertEqual(result.returncode, 0, result.stderr)
