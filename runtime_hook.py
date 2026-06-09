"""
PyInstaller runtime hook: prevent Playwright from using the "0" sentinel
that forces browser lookup relative to the temp-extracted package.
Instead, use the system Playwright browser cache.
"""
import os
import sys
import importlib
from importlib.abc import Loader, MetaPathFinder


def _find_browsers_path():
    candidates = []
    localappdata = os.environ.get('LOCALAPPDATA', '')
    if localappdata:
        candidates.append(os.path.join(localappdata, 'ms-playwright'))
    home = os.environ.get('HOME', '') or os.environ.get('USERPROFILE', '')
    if home:
        candidates.append(os.path.join(home, 'Library', 'Caches', 'ms-playwright'))
        candidates.append(os.path.join(home, '.cache', 'ms-playwright'))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


BROWSERS_PATH = _find_browsers_path()

if BROWSERS_PATH:
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = BROWSERS_PATH
    print(f"[hook] PLAYWRIGHT_BROWSERS_PATH -> {BROWSERS_PATH}")
else:
    print("[hook] Playwright browsers not found. Run: playwright install chromium")


# ── Patch playwright._impl._driver.get_driver_env ───────────────────
# When sys.frozen is True, it sets PLAYWRIGHT_BROWSERS_PATH="0" which
# makes the JS driver look for browsers relative to the _MEI* temp dir.
# We intercept via sys.meta_path to patch when the module is first loaded.

_TARGET = 'playwright._impl._driver'

class _PatcherFinder(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == _TARGET:
            # Get the real spec from default finders
            for finder in sys.meta_path:
                if finder is self:
                    continue
                spec = None
                try:
                    spec = finder.find_spec(fullname, path, target)
                except (AttributeError, ImportError):
                    continue
                if spec is not None:
                    # Wrap the loader to apply our patch after load
                    spec.loader = _PatcherLoader(spec.loader)
                    return spec
        return None


class _PatcherLoader(Loader):
    def __init__(self, original_loader):
        self._original = original_loader

    def create_module(self, spec):
        if hasattr(self._original, 'create_module'):
            return self._original.create_module(spec)
        return None

    def exec_module(self, module):
        # Load the real module first
        if hasattr(self._original, 'exec_module'):
            self._original.exec_module(module)
        # Then patch
        _orig = module.get_driver_env

        def _patched():
            env = _orig()
            if BROWSERS_PATH and env.get('PLAYWRIGHT_BROWSERS_PATH') == '0':
                env['PLAYWRIGHT_BROWSERS_PATH'] = BROWSERS_PATH
            return env

        module.get_driver_env = _patched
        print("[hook] Patched playwright._impl._driver.get_driver_env")


sys.meta_path.insert(0, _PatcherFinder())
