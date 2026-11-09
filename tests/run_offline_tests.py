"""Run synthetic unittest suites and the legacy packet checks in isolated data."""
import os
import faulthandler
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qt_test_lifecycle import QtLifecycleTextResult


def main():
    faulthandler.enable()
    faulthandler.dump_traceback_later(90, repeat=True)
    tests = Path(__file__).resolve().parent
    project = tests.parent
    sys.path.insert(0, str(project))
    sys.path.insert(0, str(tests))
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='max-offline-') as temporary:
        os.environ['MAX_DESKTOP_DATA_DIR'] = str(Path(temporary) / 'client')
        os.environ['APPDATA'] = str(Path(temporary) / 'appdata')
        suite = unittest.TestSuite()
        for path in sorted(tests.glob('test_*.py')):
            if path.name != 'test_protocol.py':
                suite.addTests(unittest.defaultTestLoader.loadTestsFromName(path.stem))
        result = unittest.TextTestRunner(verbosity=2, resultclass=QtLifecycleTextResult).run(suite)
        faulthandler.cancel_dump_traceback_later()
        if not result.wasSuccessful():
            return 1
        return subprocess.run([sys.executable, str(tests / 'test_protocol.py')], cwd=project, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
