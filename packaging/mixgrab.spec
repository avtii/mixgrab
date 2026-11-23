# PyInstaller spec for the mixgrab console app (one file). Build: pyinstaller packaging/mixgrab.spec
from PyInstaller.utils.hooks import collect_all

import importlib.util

REQUIRED = ("shazamio", "shazamio_core", "aioslsk", "yt_dlp")
OPTIONAL = ("yt_dlp_ejs", "certifi")   # yt-dlp[default] extras: YouTube JS solver, CA bundle

datas, binaries, hidden = [], [], []
for pkg in REQUIRED + OPTIONAL:
    if importlib.util.find_spec(pkg) is None:
        if pkg in REQUIRED:
            raise SystemExit(f"{pkg} isn't installed: the binary would be broken")
        print(f"note: optional {pkg} isn't installed, building without it")
        continue
    d, b, h = collect_all(pkg)          # a failure here stops the build instead of shipping a broken binary
    datas += d; binaries += b; hidden += h

a = Analysis(["entry.py"], pathex=[".."], datas=datas, binaries=binaries, hiddenimports=hidden,
             excludes=["fastapi", "uvicorn", "starlette", "mixgrab.web", "tkinter", "matplotlib", "PIL.ImageQt"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="mixgrab", console=True, upx=False,
          target_arch=None, codesign_identity=None)
