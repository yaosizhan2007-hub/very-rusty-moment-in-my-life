# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir，推荐先验证 onedir 稳定性）。

用法（在项目根目录执行）：
    .venv\\Scripts\\pyinstaller packaging\\rusty_moment.spec --noconfirm

产物：dist/RustyMoment/RustyMoment.exe（可直接双击，无需安装 Python）

onefile 单文件在音频/PortAudio 场景下启动更慢且更容易被杀软误报，
若确实需要，可用：
    pyinstaller packaging\\rusty_moment.spec --noconfirm --onefile
但请自行复测首次启动时间与麦克风/音频设备枚举。
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "config" / "default_config.json"), "config")],
    hiddenimports=[
        "win32crypt",  # DPAPI 凭据存储
        "websocket",
        "sounddevice",
        "soundfile",
        "_soundfile_data",
        "av",  # PyAV：AAC/M4A 等格式解码（FFmpeg）
        "av.audio",
        "av.container",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "pandas",
        "scipy",
        "PIL",
        "pytest",
        "pytestqt",
        # 未使用的 Qt 模块（保留 QtCore/QtGui/QtWidgets/Network/Svg 等基础依赖）
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQuickControls2",
        "PySide6.QtQml",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtUiTools",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtSpatialAudio",
        "PySide6.QtTextToSpeech",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtPositioning",
        "PySide6.QtLocation",
        "PySide6.QtRemoteObjects",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtWebSockets",
        "PySide6.QtWebChannel",
        "PySide6.QtScxml",
        "PySide6.QtStateMachine",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RustyMoment",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 程序，不弹控制台
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RustyMoment",
)
