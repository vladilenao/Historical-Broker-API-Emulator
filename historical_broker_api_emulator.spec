# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация сборки для Historical Broker API Emulator.

Автономный (one-dir) дистрибутив. Сборка воспроизводимо запускается через
``scripts/build_distribution.py``:

    python scripts/build_distribution.py

Итоговый бандл находится в
``dist/historical-broker-api-emulator-v<version>/`` с исполняемым файлом
``historical-broker-api-emulator(.exe)``, документацией рантайма, файлом
``.env.example`` и пустыми каталогами-заглушками ``config/ data/ logs/``.

Примечания:

* Точка входа — модуль CLI ``src.cli`` (он же запускает HTTP API через ``serve``).
* Tinkoff SDK упаковывает TLS-корневой сертификат как data-resource
  (``t_tech.invest.certs``); его нужно собрать в бандл, иначе создание gRPC-
  канала упадёт в рантайме.
* Сгенерированные protobuf-модули внутри ``t_tech.invest.grpc`` добавлены как
  скрытые импорты для надёжности.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("t_tech.invest.certs", includes=["*.pem"])
hiddenimports = collect_submodules("t_tech.invest.grpc")
hiddenimports += collect_submodules("t_tech.invest.market_data_stream")

a = Analysis(
    ["src/cli.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PIL",
        "IPython",
        "scipy",
        "numpy",
        "pandas",
        "pytest",
        "ruff",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="historical-broker-api-emulator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="historical-broker-api-emulator",
)