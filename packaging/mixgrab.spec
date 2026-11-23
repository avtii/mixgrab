# PyInstaller spec for the mixgrab console app (one file). Build: pyinstaller packaging/mixgrab.spec
from PyInstaller.utils.hooks import collect_all

datas, binaries, hidden = [], [], []
for pkg in ("shazamio", "shazamio_core", "aioslsk", "yt_dlp", "yt_dlp_ejs", "certifi"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d; binaries += b; hidden += h

a = Analysis(["entry.py"], pathex=[".."], datas=datas, binaries=binaries, hiddenimports=hidden,
             excludes=["fastapi", "uvicorn", "starlette", "mixgrab.web", "tkinter", "matplotlib", "PIL.ImageQt"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="mixgrab", console=True, upx=False,
          target_arch=None, codesign_identity=None)
