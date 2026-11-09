"""Public build deliberately excludes the third-party call SDK."""
from maxclient.ui.call_window import calls_assets_available
assert not calls_assets_available()
print("PUBLIC_CALLS_DISABLED: no SDK/server/media requested")
