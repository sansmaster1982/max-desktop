"""Pinned onedir packaging. No account data is included; build log/manifest retained."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest(source):
    return {p.relative_to(source).as_posix(): sha(p) for p in source.rglob('*')
        if p.is_file() and not any(part in {'.git', '.venv', '__pycache__', '.test-tmp', 'artifacts'}
                                  for part in p.relative_to(source).parts)
        and p.suffix != '.pyc'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    source, out = args.source.resolve(), args.out.resolve()
    tools = Path(__file__).resolve().parent
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 11):
        raise SystemExit('Build requires Windows CPython 3.11')
    versions = {}
    for line in (tools / 'requirements-build.lock').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        name, expected = line.split('==')
        actual = metadata.version(name)
        if actual != expected:
            raise SystemExit(f'Pinned dependency mismatch: {name}')
        versions[name] = actual
    if (source / 'maxclient/ui/calls_web').exists():
        raise SystemExit('Third-party call SDK must be excluded')
    allowed_assets = {'message.wav', *(f'pattern_{n}.svg' for n in
        ('space', 'feb23', 'mar8', 'nature', 'neon', 'moscow', 'lebedev'))}
    if {p.name for p in (source / 'maxclient/assets').iterdir()} != allowed_assets:
        raise SystemExit('Unexpected public asset')
    out.mkdir(parents=True, exist_ok=False)
    source_hashes = source_manifest(source)
    env = os.environ.copy()
    env['MAXIM_PUBLIC_SOURCE'] = str(source)
    env['PYINSTALLER_CONFIG_DIR'] = str(out / 'pyinstaller-cache')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Do not resolve Qt's Windows ICU dependency against unrelated native tools
    # on the developer PATH (e.g. a Poppler ICU with version-suffixed exports).
    windows = Path(env.get('SystemRoot', 'C:/Windows'))
    env['PATH'] = os.pathsep.join(map(str, (Path(sys.base_prefix),
        Path(sys.base_prefix) / 'Scripts', windows / 'System32', windows)))
    with (out / 'build.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
            '--distpath', str(out / 'dist'), '--workpath', str(out / 'work'), str(tools / 'MAXIM.spec')],
            cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
    if result.returncode:
        raise SystemExit(f'Build failed; see {out / "build.log"}')
    bundle = out / 'dist/MAXIM'
    forbidden = [p.relative_to(bundle).as_posix() for p in bundle.rglob('*')
                 if p.is_file() and any(token in p.name.lower()
                    for token in ('webengine', 'webchannel', 'chromium', 'chrome_'))]
    if forbidden:
        raise SystemExit('Excluded browser components collected: ' + ', '.join(forbidden))
    if (source / 'third_party_licenses').is_dir():
        shutil.copytree(source / 'third_party_licenses', bundle / 'third_party_licenses')
    for name in ('README.md', 'ROADMAP.md', 'THIRD_PARTY_NOTICES.md', 'Qt_NOTICES.md',
                 'BUILDING.md', 'PUBLIC_RELEASE.md', 'RELEASE_NOTES.md', 'LICENSE'):
        if (source / name).is_file():
            shutil.copyfile(source / name, bundle / name)
    smoke = out / 'smoke'
    smoke.mkdir()
    for key in ('APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'MAX_DESKTOP_DATA_DIR'):
        path = smoke / key.lower()
        path.mkdir()
        env[key] = str(path)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['MAXDESKTOP_SELFTEST_LOG'] = str(smoke / 'selftest.log')
    with (smoke / 'process.log').open('w', encoding='utf-8') as log:
        run = subprocess.run([str(bundle / 'MAXIM.exe'), '--selftest'], cwd=smoke,
            env=env, stdout=log, stderr=subprocess.STDOUT, timeout=60)
    report = (smoke / 'selftest.log').read_text(encoding='utf-8') if (smoke / 'selftest.log').exists() else ''
    if run.returncode != 0 or 'SELFTEST OK' not in report:
        raise SystemExit('Frozen offline smoke failed; evidence retained')
    after = source_manifest(source)
    if source_hashes != after:
        raise SystemExit('Source changed during build')
    archive = Path(shutil.make_archive(str(out / 'MAXIM-1.1.0-alpha.3-windows-x64'), 'zip', bundle.parent, bundle.name))
    receipt = {'version': '1.1.0-alpha.3', 'python': sys.version, 'dependencies': versions,
        'sourceManifest': source_hashes, 'sourceUnchanged': True, 'selftestPassed': True,
        'selftestScope': 'isolated empty data; login UI+QtMultimedia construction only',
        'realLoginTested': False, 'networkGuard': 'Python audit hook; no native-network verification claim',
        'binarySigned': False, 'licenseReviewRequired': True, 'byteReproducibilityClaimed': False,
        'webEngineBundled': False, 'miniappsAvailableInExecutable': False,
        'optionalCryptoExcluded': ['cryptography', 'OpenSSL', 'cffi', '_cffi_backend'],
        'optionalDotNetExcluded': ['clr', 'pythonnet', 'clr_loader'],
        'exeSha256': sha(bundle / 'MAXIM.exe'), 'zipSha256': sha(archive), 'zipBytes': archive.stat().st_size,
        'buildLogSha256': sha(out / 'build.log'), 'selftestLogSha256': sha(smoke / 'selftest.log')}
    (out / 'build_receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps({'archive': str(archive), 'zipSha256': receipt['zipSha256'], 'selftestPassed': True}))


if __name__ == '__main__':
    main()
