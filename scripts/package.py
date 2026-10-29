"""Create an allowlisted source archive with no personal settings or runtime data."""

import hashlib
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_monitor import __version__

REQUIRED = (
    '.gitignore', 'README.md', 'LICENSE', 'PRIVACY.md', 'SECURITY.md', 'CONTRIBUTING.md',
    'THIRD_PARTY_NOTICES.md', 'Makefile', 'install.sh', 'uninstall.sh', 'status.sh',
    'extension/package.json', 'extension/metadata.json', 'extension/stylesheet.css',
    'extension/schemas/org.gnome.shell.extensions.ai-monitor.gschema.xml',
    'extension/sounds/sound_effect_1.mp3', 'extension/sounds/sound_effect_2.mp3',
    'extension/sounds/sound_effect_3.mp3', 'docs/desktop-check.md', 'docs/repository.md',
    'docs/audio-provenance.json', 'docs/screenshots/dashboard-dark.png',
    'docs/screenshots/dashboard-light.png', 'docs/screenshots/panel-normal.png',
    'docs/screenshots/panel-attention.png', '.github/workflows/checks.yml',
)
PATTERNS = ('ai_monitor/*.py', 'extension/*.js', 'scripts/*.py', 'tests/test_*.py',
            'tests/*.test.mjs', 'docs/releases/v*.md')


def source_files(root):
    files = {root / name for name in REQUIRED}
    for pattern in PATTERNS:
        files.update(root.glob(pattern))
    for path in files:
        for part in (path, *path.parents):
            if part == root:
                break
            if part.is_symlink():
                raise ValueError(f'Refusing to package a symlink: {path.relative_to(root)}')
        if not path.is_file():
            raise ValueError(f'Missing release file: {path.relative_to(root)}')
    return sorted(files)


def build_archive(root, destination):
    files = source_files(root)
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(str(Path('ai-monitor') / path.relative_to(root)))
            info.create_system = 3
            info.external_attr = (0o100755 if path.suffix == '.sh' else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    checksum = destination.with_suffix(destination.suffix + '.sha256')
    checksum.write_text(f'{hashlib.sha256(destination.read_bytes()).hexdigest()}  {destination.name}\n')
    return checksum


if __name__ == '__main__':
    destination = ROOT / f'dist/ai-monitor-{__version__}-source.zip'
    checksum = build_archive(ROOT, destination)
    print(destination)
    print(checksum)
