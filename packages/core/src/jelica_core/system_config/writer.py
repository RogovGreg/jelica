from __future__ import annotations

import os
import tempfile
from pathlib import Path

import tomli_w

from .errors import CoreConfigWriteError
from .models import CoreConfigInput, to_toml_document


class CoreConfigWriter:
    """Write system config TOML atomically."""

    def write(self, *, config_path: Path, config_input: CoreConfigInput) -> None:
        temp_file_path: Path | None = None
        try:
            payload = self.serialize(config_input=config_input)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
                dir=config_path.parent,
                prefix="config.",
                suffix=".tmp",
            ) as temp_file:
                temp_file.write(payload)
                temp_file_path = Path(temp_file.name)

            os.replace(temp_file_path, config_path)
        except OSError as error:
            if temp_file_path is not None:
                temp_file_path.unlink(missing_ok=True)
            raise CoreConfigWriteError(path=config_path, detail=str(error)) from error

    def serialize(self, *, config_input: CoreConfigInput) -> str:
        document = to_toml_document(config_input)
        serialized = tomli_w.dumps(document)
        if not serialized.endswith("\n"):
            return f"{serialized}\n"
        return serialized
