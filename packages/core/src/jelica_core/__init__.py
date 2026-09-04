from __future__ import annotations

from importlib.metadata import version
from typing import Final, TypedDict

_IMPORT_NAME: Final = "jelica_core"
_PACKAGE_NAME: Final = "jelica-core"


class CoreInfo(TypedDict):
    package: str
    import_name: str
    version: str


__version__ = version(_PACKAGE_NAME)


def get_core_info() -> CoreInfo:
    """Return basic metadata about the installed JELICA Core package."""
    return {
        "package": _PACKAGE_NAME,
        "import_name": _IMPORT_NAME,
        "version": __version__,
    }


__all__ = ["CoreInfo", "__version__", "get_core_info"]
