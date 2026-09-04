from __future__ import annotations

import tomllib
from pathlib import Path

import jelica_core
from jelica_core import get_core_info


def test_jelica_core_importable() -> None:
    assert hasattr(jelica_core, "__version__")


def test_core_info_contains_expected_metadata() -> None:
    info = get_core_info()

    assert info["package"] == "jelica-core"
    assert info["import_name"] == "jelica_core"
    assert info["version"] == jelica_core.__version__
    assert info["version"]


def test_core_package_declares_biopython_runtime_dependency() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pyproject_path = repo_root / "packages" / "core" / "pyproject.toml"
    pyproject_document = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    runtime_dependencies = pyproject_document["project"]["dependencies"]

    assert any(dependency.lower().startswith("biopython") for dependency in runtime_dependencies)
