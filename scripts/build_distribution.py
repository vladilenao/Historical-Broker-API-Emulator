"""Сборка автономного пользовательского дистрибутива.

Результат — ``dist/historical-broker-api-emulator-v<version>/``:

* PyInstaller-бандл one-dir (исполняемый файл
  ``historical-broker-api-emulator`` или ``.exe`` на Windows);
* ``VERSION.txt``, ``README.txt``, ``API_CONNECTION.md``, ``COMPATIBILITY.md``;
* ``.env``;
* пустые каталоги-заглушки ``config/ data/ logs/``;
* правила сессий ``config/schedule_rules/*.toml`` для ``reconstruct-schedules``.

Версия берётся из единого источника ``src.__version__`` (SemVer
``MAJOR.MINOR.PATCH``); ручной правки версии в нескольких местах нет.

Использование:

    python scripts/build_distribution.py [--keep-build]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import __version__  # noqa: E402

BINARY_NAME = "historical-broker-api-emulator"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
VERSION = __version__
DIST_NAME = f"{BINARY_NAME}-v{VERSION}"

_COPY_FILES = [
    ".env",
    "API_CONNECTION.md",
    "COMPATIBILITY.md",
]
_PLACEHOLDER_DIRS = ["config", "data", "logs"]
_TEMPLATE_DIR = ROOT / "scripts" / "dist"


def _clean() -> None:
    for directory in (DIST_DIR, BUILD_DIR):
        shutil.rmtree(directory, ignore_errors=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)


def _build_bundle() -> Path:
    spec = ROOT / "historical_broker_api_emulator.spec"
    if not spec.is_file():
        sys.exit(f"spec не найден: {spec}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            str(spec),
        ],
        cwd=ROOT,
        check=True,
    )
    bundle = DIST_DIR / BINARY_NAME
    if not bundle.is_dir():
        sys.exit(f"PyInstaller не создал бандл в {bundle}")
    return bundle


def _assemble(bundle: Path, keep_build: bool) -> Path:
    final = DIST_DIR / DIST_NAME
    if final.exists():
        shutil.rmtree(final)
    bundle.rename(final)

    (final / "VERSION.txt").write_text(f"{VERSION}\n", encoding="utf-8")

    with (final / "README.txt").open("w", encoding="utf-8") as handle:
        handle.write(
            (_TEMPLATE_DIR / "README.txt").read_text(encoding="utf-8").replace(
                "___VERSION___", VERSION
            )
        )

    for name in _COPY_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, final / name)

    for directory in _PLACEHOLDER_DIRS:
        target = final / directory
        target.mkdir(exist_ok=True)
        placeholder = _TEMPLATE_DIR / directory / "README.txt"
        if placeholder.is_file():
            shutil.copy2(placeholder, target / "README.txt")

    shutil.copytree(
        ROOT / "config" / "schedule_rules", final / "config" / "schedule_rules", dirs_exist_ok=True
    )

    if not keep_build:
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Сборка автономного дистрибутива.")
    parser.add_argument(
        "--keep-build",
        action="store_true",
        help="сохранять промежуточный каталог сборки PyInstaller build/",
    )
    args = parser.parse_args()

    _clean()
    bundle = _build_bundle()
    final = _assemble(bundle, keep_build=args.keep_build)
    print(f"Собран дистрибутив: {final}")
    print(f"Версия: {VERSION}")


if __name__ == "__main__":
    main()
