"""Only --selftest is offline and must use a new, explicitly isolated directory."""
import os
from pathlib import Path
import sys

if '--selftest' in sys.argv:
    directory = os.environ.get('MAX_DESKTOP_DATA_DIR')
    if not directory:
        raise RuntimeError('Selftest requires an explicit isolated data directory')
    root = Path(directory).resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError('Selftest requires an empty data directory')
    if '.local' in (part.lower() for part in root.parts):
        raise RuntimeError('Account directory forbidden in selftest')
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'

    def deny_network(event, args):
        if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                     'socket.bind', 'socket.sendto'}:
            raise RuntimeError('Selftest network access blocked')
        if event == 'open' and args and isinstance(args[0], (str, bytes)):
            name = os.fsdecode(args[0]).replace('\\', '/').lower()
            if '/.local/' in name:
                raise RuntimeError('Private data access forbidden')
    sys.addaudithook(deny_network)
