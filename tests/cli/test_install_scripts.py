from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_install_sh_reinstalls_tool_environment() -> None:
    script_path = _repo_root() / "scripts" / "install.sh"
    script_text = script_path.read_text(encoding="utf-8")

    install_command = (
        'uv tool install --directory "${REPO_ROOT}" '
        '--editable "${CLI_PACKAGE_DIR}" --force --reinstall'
    )
    assert install_command in script_text

    assert script_text.index("uv tool install") < script_text.index("if ! command -v jelica")


def test_install_ps1_reinstalls_tool_environment() -> None:
    script_path = _repo_root() / "scripts" / "install.ps1"
    script_text = script_path.read_text(encoding="utf-8")

    install_command = (
        'uv tool install --directory "$repoRoot" --editable "$cliPackageDir" --force --reinstall'
    )
    assert install_command in script_text

    assert script_text.index("uv tool install") < script_text.index(
        "if (-not (Get-Command jelica -ErrorAction SilentlyContinue))"
    )
