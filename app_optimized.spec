# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — self-contained exe with Playwright bundled.
Requirements: playwright browsers (playwright install chromium)
"""

import os
PROJECT_DIR = r'D:\dy-scraper'
UPX_DIR = r'D:\upx\upx-4.2.4-win64'

# ── Exclude massive stdlib modules we never use ──────────────────────
EXCLUDES = [
    # GUI
    'tkinter', 'turtle', 'turtledemo',
    # Testing / Debug
    'unittest', 'test', 'doctest', 'pdb', 'pydoc', 'idlelib',
    # Installers / Packaging
    'ensurepip', 'venv', 'distutils', 'setuptools', 'pip',
    # Network servers/protocols we don't use
    'xmlrpc', 'netrc', 'ftplib', 'telnetlib', 'smtplib', 'poplib',
    'imaplib', 'nntplib', 'smtpd',
    # Numeric/Scientific
    'numpy', 'pandas', 'matplotlib', 'scipy', 'PIL',
    # Email (Flask/Werkzeug/http.server need email)
    'mailbox',
    # Multimedia
    'wave', 'aifc', 'sunau', 'sndhdr', 'colorsys', 'imghdr',
    # Web servers (Flask/Werkzeug needs http.server, socketserver)
    'wsgiref',
    # Misc heavy modules
    'lib2to3',
    'ctypes.test', 'distutils.tests',
]

HIDDEN_IMPORTS = [
    'playwright',
    'playwright._impl',
    'playwright.sync_api',
    'playwright.async_api',
    'flask',
    'flask_cors',
    'xlsxwriter',
    'apscheduler',
    'httpx',
    'execjs',
    'jinja2',
    'sqlite3',
    'json',
    'pathlib',
    'urllib.parse',
]

# ── Data files that must be bundled ───────────────────────────────────
DATAS = [
    (os.path.join(PROJECT_DIR, 'templates'), 'templates'),
    (os.path.join(PROJECT_DIR, 'img'), 'img'),
    (os.path.join(PROJECT_DIR, 'libs', 'douyin.js'), 'libs'),
    (os.path.join(PROJECT_DIR, 'keywords.txt'), '.'),
]

a = Analysis(
    [os.path.join(PROJECT_DIR, 'app.py')],
    pathex=[],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(PROJECT_DIR, 'runtime_hook.py')],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='dy-scraper2.2',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_dir=UPX_DIR,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
