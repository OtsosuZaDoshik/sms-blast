# -*- mode: python ; coding: utf-8 -*-
"""Конфигурация PyInstaller.

На Windows собираем один файл smsblast.exe, на macOS — папку и .app-бандл
(внутрь бандла нельзя писать, поэтому база живёт в папке данных пользователя —
этим занимается launcher.py).
"""

import sys

SRC = "src"
ONEFILE = sys.platform.startswith("win")

# app.py импортирует пакет напрямую, но часть импортов отложена внутрь функций
# (openpyxl в importer), поэтому перечисляем явно.
HIDDEN = [
    "app",
    "smsblast",
    "smsblast.config",
    "smsblast.db",
    "smsblast.discovery",
    "smsblast.gateway",
    "smsblast.importer",
    "smsblast.phones",
    "smsblast.sender",
    "smsblast.templating",
    "smsblast.usb",
    "openpyxl",
    "dotenv",
    "desktop",
    "webview",
]

if sys.platform == "darwin":
    HIDDEN += ["webview.platforms.cocoa", "objc", "Foundation", "AppKit", "WebKit"]
elif sys.platform.startswith("win"):
    HIDDEN += ["webview.platforms.edgechromium", "clr", "System"]

analysis = Analysis(
    ["launcher.py"],
    pathex=[SRC],
    binaries=[],
    datas=[
        (SRC + "/templates", "templates"),
        (SRC + "/static", "static"),
    ],
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest", "IPython"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name="smsblast",
        debug=False,
        strip=False,
        upx=False,
        # Оконное приложение: чёрная консоль за окном не нужна. Вывод уходит
        # в журнал — им занимается desktop.ensure_output().
        console=False,
        disable_windowed_traceback=False,
    )
else:
    exe = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name="smsblast",
        debug=False,
        strip=False,
        upx=False,
        console=True,
    )
    collected = COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        name="smsblast",
    )
    bundle = BUNDLE(
        collected,
        name="SMS-рассылка.app",
        icon=None,
        bundle_identifier="local.smsblast.panel",
        info_plist={
            "CFBundleName": "SMS-рассылка",
            "CFBundleDisplayName": "SMS-рассылка",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
            # Приложение открывает браузер и не имеет своего окна.
            "LSBackgroundOnly": False,
        },
    )
