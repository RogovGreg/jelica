"""Stage the tracked notification WAV into the installable Core package."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "assets" / "notifications" / "notification.wav"
DESTINATION = (
    ROOT
    / "packages"
    / "core"
    / "src"
    / "jelica_core"
    / "resources"
    / "notifications"
    / "notification.wav"
)

if not SOURCE.is_file():
    raise SystemExit(f"canonical notification sound is missing: {SOURCE}")
DESTINATION.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(SOURCE, DESTINATION)
if SOURCE.read_bytes() != DESTINATION.read_bytes():
    raise SystemExit("staged notification sound differs from canonical source")
print(DESTINATION)
