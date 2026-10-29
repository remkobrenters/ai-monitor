import hashlib
import importlib.util
from pathlib import Path
import shutil
import secrets
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('package', Path(__file__).resolve().parents[1] / 'scripts/package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def test_release_excludes_runtime_files_and_keeps_notices_permissions_and_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'
            for original in package.source_files(package.ROOT):
                destination = root / original.relative_to(package.ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
            secret = 'fixture-' + secrets.token_hex(16)
            for name in ('.env', 'ai_monitor/credentials.json', 'extension/.credentials.json',
                         'docs/screenshots/private.png', 'tests/private.jsonl'):
                (root / name).write_text(secret)
            output = Path(temporary) / 'release.zip'
            checksum = package.build_archive(root, output)
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertIn('ai-monitor/THIRD_PARTY_NOTICES.md', names)
                self.assertIn('ai-monitor/docs/screenshots/dashboard-dark.png', names)
                self.assertEqual(archive.getinfo('ai-monitor/install.sh').external_attr >> 16, 0o100755)
                self.assertTrue(all(secret.encode() not in archive.read(name) for name in names))
            self.assertEqual(checksum.read_text().split()[0], hashlib.sha256(output.read_bytes()).hexdigest())
            target = root / 'extension/sounds/sound_effect_1.mp3'
            target.unlink()
            target.symlink_to(root / '.env')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                package.build_archive(root, output)
