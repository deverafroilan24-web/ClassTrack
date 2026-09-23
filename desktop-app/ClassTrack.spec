# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

desktop_dir = Path(os.path.abspath('desktop-app'))

# Collect ultralytics and torch data files
ultralytics_datas = collect_data_files('ultralytics')
torch_datas = collect_data_files('torch')

datas = [
    (str(desktop_dir / 'models'), 'models'),
    (str(desktop_dir / 'assets'), 'assets'),
] + ultralytics_datas + torch_datas

hiddenimports = [
    'unittest',
    'unittest.mock',
    'ultralytics',
    'ultralytics.nn',
    'ultralytics.models',
    'ultralytics.models.yolo',
    'ultralytics.models.yolo.pose',
    'torch',
    'torchvision',
    'cv2',
    'websocket',
    'requests',
    'dotenv',
    'numpy',
    'PIL',
    'PIL.Image',
    'config',
    'api_client',
    'vision_worker',
    'vision_worker.worker',
    'vision_worker.pose_engine',
    'vision_worker.kinematics',
    'vision_worker.camera_source',
    'vision_worker.podium_queue',
    'vision_worker.state_machine',
    'vision_worker.face_engine',
] + collect_submodules('ultralytics') + collect_submodules('vision_worker')

a = Analysis(
    [str(desktop_dir / 'launch_desktop.py')],
    pathex=[str(desktop_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'IPython', 'pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ClassTrack',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(desktop_dir / 'assets' / 'app_icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ClassTrack',
)
