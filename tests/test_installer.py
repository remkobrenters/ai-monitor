import contextlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("manage", Path(__file__).resolve().parents[1] / "scripts/manage.py")
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-monitor install 'space ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.installation = manage.Installation(self.root)
        self.claude = self.root / "home/.claude/settings.json"
        self.codex = self.root / "home/.codex/hooks.json"
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_reinstall_and_uninstall_preserve_original_settings_and_history(self):
        original = '{\n  "theme": "dark",\n  "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-own-hook"}]}]}\n}\n'
        self.claude.parent.mkdir(parents=True)
        self.claude.write_text(original)
        self.installation.install()
        first = self.claude.read_text()
        self.installation.install()
        self.assertEqual(json.loads(first), json.loads(self.claude.read_text()))
        self.assertEqual(len(json.loads(first)["hooks"]["Stop"]), 2)
        for path in (self.claude, self.codex):
            hooks = json.loads(path.read_text())['hooks']
            self.assertEqual(len(hooks['SubagentStart']), 1)
            self.assertEqual(len(hooks['SubagentStop']), 1)
        self.assertTrue(self.installation.unit.exists())
        for number in (1, 2, 3):
            name = f'sound_effect_{number}.mp3'
            self.assertEqual((self.installation.sounds / name).read_bytes(),
                             (manage.ROOT / 'extension/sounds' / name).read_bytes())
        result = subprocess.run([str(self.installation.launcher), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ai-monitor", result.stdout)
        history = self.installation.state / "ai-days/2026-01-01.json"
        history.parent.mkdir()
        history.write_text('{"tokens": 10}')
        self.installation.uninstall()
        self.assertEqual(json.loads(self.claude.read_text()), json.loads(original))
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.installation.app.exists())
        self.assertFalse(self.installation.extension.exists())
        self.assertFalse(self.installation.unit.exists())
        self.assertFalse(self.installation.launcher.exists())
        self.assertTrue(history.exists())
        self.installation.uninstall(purge=True)
        self.assertFalse(self.installation.state.exists())

    def test_uninstall_preserves_settings_and_hooks_added_after_install(self):
        self.installation.install()
        changed = json.loads(self.claude.read_text())
        changed["theme"] = "light"
        changed["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "later-hook"})
        self.claude.write_text(json.dumps(changed))
        self.installation.uninstall()
        self.assertEqual(json.loads(self.claude.read_text()), {"theme": "light", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "later-hook"}]}]}})

    def test_invalid_settings_fail_before_installing_or_disabling_anything(self):
        self.claude.parent.mkdir(parents=True)
        self.claude.write_text("{invalid")
        with self.assertRaises(ValueError):
            self.installation.install()
        self.assertFalse(self.installation.app.exists())
        self.assertEqual(self.claude.read_text(), "{invalid")

    def test_no_hooks_and_dry_run_do_not_modify_cli_settings(self):
        self.installation.install(dry_run=True)
        self.assertFalse(self.installation.app.exists())
        self.assertFalse(self.claude.exists())
        self.installation.install(hooks=False)
        self.assertFalse(self.claude.exists())
        self.installation.uninstall()

    def test_settings_changed_to_invalid_json_block_uninstall_before_app_removal(self):
        self.installation.install()
        self.claude.write_text("not-json")
        with self.assertRaises(ValueError):
            self.installation.uninstall()
        self.assertTrue(self.installation.app.exists())

    def test_symlink_targets_are_not_replaced(self):
        self.installation.app.parent.mkdir(parents=True)
        target = self.root / "keep"
        target.mkdir()
        self.installation.app.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.installation.install()
        self.assertTrue(target.exists())

    def test_original_empty_hooks_object_is_restored(self):
        self.claude.parent.mkdir(parents=True)
        original = '{"hooks": {}}\n'
        self.claude.write_text(original)
        self.installation.install()
        self.installation.uninstall()
        self.assertEqual(json.loads(self.claude.read_text()), json.loads(original))

    def test_installer_records_do_not_copy_secrets_and_upgrades_remove_obsolete_code(self):
        self.claude.parent.mkdir(parents=True)
        self.claude.write_text(json.dumps({'env': {'API_KEY': 'fixture-secret'}}))
        self.installation.install()
        self.assertNotIn('fixture-secret', self.installation.manifest.read_text())
        stale = self.installation.sounds.parent / 'obsolete.js'
        stale.write_text('old module')
        self.installation.install()
        self.assertFalse(stale.exists())
        self.installation.uninstall()
        self.assertEqual(json.loads(self.claude.read_text()), {'env': {'API_KEY': 'fixture-secret'}})

    def test_legacy_manifest_is_migrated_without_retaining_the_original_settings(self):
        self.installation.install()
        manifest = json.loads(self.installation.manifest.read_text())
        manifest['hooks'][0] = {**manifest['hooks'][0], 'original': '{"secret":"fixture-secret"}'}
        self.installation.manifest.write_text(json.dumps(manifest))
        self.installation.install()
        self.assertNotIn('fixture-secret', self.installation.manifest.read_text())

    def test_control_characters_cannot_inject_service_directives(self):
        installation = manage.Installation(self.root / 'bad\nExecStart=unexpected')
        with self.assertRaisesRegex(ValueError, 'control characters'):
            installation.install()
        self.assertFalse(installation.app.exists())

    def test_installed_uninstaller_does_not_need_source_checkout(self):
        self.installation.install()
        manager = self.installation.app / "scripts/manage.py"
        result = subprocess.run([sys.executable, str(manager), "uninstall", "--destdir", str(self.root), "--purge"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.installation.app.exists())
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.installation.state.exists())

    @unittest.skipUnless(manage.MACOS, "macOS layout")
    def test_macos_install_registers_launch_agents_and_menu_bar_app(self):
        self.installation.install()
        service = plistlib.loads(self.installation.unit.read_bytes())
        entry = str(self.installation.app / "entry.py")
        self.assertEqual(service["ProgramArguments"], [self.installation.python, entry, "serve"])
        self.assertEqual(service["EnvironmentVariables"]["XDG_STATE_HOME"], str(self.installation.state.parent))
        self.assertIn("PATH", service["EnvironmentVariables"])
        menu = plistlib.loads(self.installation.menu_unit.read_bytes())
        self.assertEqual(menu["ProgramArguments"],
                         [str(self.installation.extension / "Contents/MacOS" / manage.MENU_EXECUTABLE)])
        info = plistlib.loads((self.installation.extension / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], manage.MENU_LABEL)
        self.assertTrue(info["LSUIElement"])
        self.assertEqual((info["AIMonitorPython"], info["AIMonitorEntry"]), (self.installation.python, entry))
        hooks = json.loads(self.claude.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(hooks, f"{shlex.quote(self.installation.python)} {shlex.quote(entry)} hook claude")
        self.installation.uninstall(purge=True)
        for path in (self.installation.unit, self.installation.menu_unit, self.installation.extension):
            self.assertFalse(path.exists())
