"""Build the small application-owned macOS notification helper bundle."""

import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "packages" / "core" / "native" / "macos" / "JelicaNotificationHelper.swift"
APP = (
    ROOT
    / "packages"
    / "core"
    / "src"
    / "jelica_core"
    / "resources"
    / "macos"
    / "JELICA Notification Helper.app"
)
EXECUTABLE = APP / "Contents" / "MacOS" / "JELICA Notification Helper"

swiftc = Path("/usr/bin/swiftc")
codesign = Path("/usr/bin/codesign")
if not SOURCE.is_file():
    raise SystemExit(f"missing helper source: {SOURCE}")
if not swiftc.is_file():
    raise SystemExit("macOS helper build requires /usr/bin/swiftc")
if not codesign.is_file():
    raise SystemExit("macOS helper build requires /usr/bin/codesign")

with tempfile.TemporaryDirectory(prefix="jelica-notification-helper-") as temporary_root:
    temporary_app = Path(temporary_root) / APP.name
    temporary_executable = temporary_app / "Contents" / "MacOS" / APP.stem
    temporary_info = temporary_app / "Contents" / "Info.plist"
    temporary_executable.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(swiftc),
            "-O",
            "-framework",
            "UserNotifications",
            str(SOURCE),
            "-o",
            str(temporary_executable),
        ],
        check=True,
    )
    temporary_info.write_bytes(
        plistlib.dumps(
            {
                "CFBundleDisplayName": "JELICA",
                "CFBundleExecutable": APP.stem,
                "CFBundleIdentifier": "org.jelica.notification-helper",
                "CFBundleName": "JELICA",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.1.0",
                "CFBundleVersion": "0.1.0",
                "LSUIElement": True,
            }
        )
    )
    subprocess.run(
        [
            str(codesign),
            "--force",
            "--deep",
            "--sign",
            "-",
            "--timestamp=none",
            str(temporary_app),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(codesign),
            "--verify",
            "--deep",
            "--strict",
            "--verbose=4",
            str(temporary_app),
        ],
        check=True,
    )
    APP.parent.mkdir(parents=True, exist_ok=True)
    if APP.exists():
        shutil.rmtree(APP)
    shutil.copytree(temporary_app, APP)
print(EXECUTABLE)
