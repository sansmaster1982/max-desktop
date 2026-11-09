"""Run the curated suite with inherited offline/account guards and a retained log."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


GUARD = '''import os, sys
def audit(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind', 'socket.sendto'}:
        raise RuntimeError('Public verification network access denied')
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        name = os.fsdecode(args[0]).replace('\\\\','/').lower()
        if '/.local/' in name:
            raise RuntimeError('Private account files denied')
sys.addaudithook(audit)
os.environ['MAXIM_PUBLIC_OFFLINE_GUARD'] = 'active'
'''


def manifest(source):
    return {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in ('maxclient', 'tests') for p in (source / directory).rglob('*')
            if p.is_file() and p.suffix in {'.py', '.svg', '.wav', '.ico', '.png'} and '__pycache__' not in p.parts}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--deps', type=Path)
    args = parser.parse_args()
    source, out = args.source.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    guard = out / 'guard'
    guard.mkdir()
    (guard / 'sitecustomize.py').write_text(GUARD, encoding='utf-8')
    env = os.environ.copy()
    deps = args.deps.resolve() if args.deps else None
    env['PYTHONPATH'] = os.pathsep.join(str(p) for p in (guard, source, source / 'tests', deps) if p)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    for key in ('APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'MAX_DESKTOP_DATA_DIR'):
        path = out / key.lower()
        path.mkdir()
        env[key] = str(path)
    before = manifest(source)
    with (out / 'full_offline.log').open('w', encoding='utf-8') as log:
        entry = "import os,runpy; assert os.environ.get('MAXIM_PUBLIC_OFFLINE_GUARD') == 'active'; runpy.run_path('tests/run_offline_tests.py', run_name='__main__')"
        run = subprocess.run([sys.executable, '-c', entry],
            cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=600)
    after = manifest(source)
    receipt = {'returnCode': run.returncode, 'sourceUnchanged': before == after,
               'sourceManifest': before, 'networkGuardInherited': True,
               'accountFilesRead': False, 'scope': 'curated public variant; not private release',
               'logSha256': hashlib.sha256((out / 'full_offline.log').read_bytes()).hexdigest()}
    (out / 'test_receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps({'returnCode': run.returncode, 'sourceUnchanged': before == after, 'log': str(out / 'full_offline.log')}))
    return 0 if run.returncode == 0 and before == after else 1


if __name__ == '__main__':
    raise SystemExit(main())
