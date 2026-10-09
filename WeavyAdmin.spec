# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the macOS WeavyAdmin.app bundle.
# Built by shahin/build_dmg.sh — run from the repo root: `pyinstaller WeavyAdmin.spec`.

import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

sys.path.insert(0, SPECPATH)
from app.version import APP_VERSION  # noqa: E402

# Runtime images (logo, splash) — loaded relative to the package, so keep the path.
datas = [("res", "res")]

# Packages that read their own version via importlib.metadata at runtime.
# pyi_hooks/bundle_metadata.py makes these findable inside the .app bundle.
for dist in ("weaviate-client", "weaviate-agents", "grpcio", "httpx", "pydantic", "protobuf"):
    try:
        datas += copy_metadata(dist)
    except Exception:  # noqa: BLE001 — optional package not installed
        pass

# The client and agents import submodules dynamically. ``weaviate.agents`` is an
# alias weaviate-agents installs over itself — its modules are bundled under
# ``weaviate_agents.*``, so skip the alias names (PyInstaller can't resolve them).
hiddenimports = collect_submodules(
    "weaviate", filter=lambda name: not name.startswith("weaviate.agents")
) + collect_submodules("weaviate_agents")

a = Analysis(
    ["main.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=["pyi_hooks/bundle_metadata.py"],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WeavyAdmin",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="WeavyAdmin")

app = BUNDLE(
    coll,
    name="WeavyAdmin.app",
    icon="res/images/weaviate-logo.png",  # converted to .icns via Pillow
    bundle_identifier="io.weaviate.weavyadmin",
    version=APP_VERSION,
    info_plist={
        "CFBundleName": "WeavyAdmin",
        "CFBundleDisplayName": "WeavyAdmin",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
