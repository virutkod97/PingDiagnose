a = Analysis(
    ["service.py"],
    pathex=[],
    datas=[
        ("pingdiagnose/templates", "pingdiagnose/templates"),
        ("pingdiagnose/static", "pingdiagnose/static"),
    ],
    hiddenimports=["win32timezone", "cheroot.ssl.builtin"],
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PingDiagnose",
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PingDiagnose", upx=False)
