# Run via build_public.py; never collect_data_files(maxclient) or entire workspace.
import os
from pathlib import Path

root = Path(os.environ['MAXIM_PUBLIC_SOURCE']).resolve()
tools = Path(SPECPATH)
data = [(str(root / 'maxclient/resources/maxim.ico'), 'maxclient/resources'),
        (str(root / 'maxclient/resources/maxim_icon.png'), 'maxclient/resources')]
data += [(str(path), 'maxclient/assets') for path in sorted((root / 'maxclient/assets').glob('*.svg'))]
data += [(str(root / 'maxclient/assets/message.wav'), 'maxclient/assets')]
data += [(str(root / 'LICENSE'), '.')]
a = Analysis([str(root / 'run.py')], pathex=[str(root)], binaries=[], datas=data,
             hiddenimports=['qrcode.image.pil'], hookspath=[], hooksconfig={},
             runtime_hooks=[str(tools / 'runtime_selftest_guard.py')],
             excludes=['tkinter', 'pytest', 'IPython', 'matplotlib', 'numpy', 'scipy',
                       'PyQt5', 'PyQt6', 'PySide2',
                       'cryptography', 'OpenSSL', 'cffi', '_cffi_backend',
                       'clr', 'pythonnet', 'clr_loader',
                       'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
                       'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel',
                       'maxclient.ui.miniapp_window', 'maxclient.ui.call_window'],
             noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MAXIM', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'maxclient/resources/maxim.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MAXIM')
