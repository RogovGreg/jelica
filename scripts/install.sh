#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLI_PACKAGE_DIR="${REPO_ROOT}/apps/cli"

print_uv_available_message() {
  local uv_version
  uv_version="$(uv --version 2>/dev/null | awk '{print $2}')"
  if [[ "${uv_version}" == "" ]]; then
    uv_version="unknown"
  fi
  echo "uv ${uv_version} is available."
}

add_standard_uv_dirs_to_path() {
  local candidate
  for candidate in "${HOME}/.local/bin" "${HOME}/.cargo/bin"; do
    if [[ -x "${candidate}/uv" ]] && [[ ":${PATH}:" != *":${candidate}:"* ]]; then
      PATH="${candidate}:${PATH}"
    fi
  done
}

ensure_uv_available() {
  if command -v uv >/dev/null 2>&1; then
    print_uv_available_message
    return
  fi

  echo "uv was not found. Installing uv using the official Astral standalone installer..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "Error: uv is not installed and neither curl nor wget is available." >&2
    echo "Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and rerun this script." >&2
    exit 1
  fi

  add_standard_uv_dirs_to_path

  if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv was installed but is still not available in PATH." >&2
    echo "Open a new terminal (or restart your login shell) and rerun this script." >&2
    exit 1
  fi

  print_uv_available_message
}

ensure_uv_available

if [[ ! -f "${CLI_PACKAGE_DIR}/pyproject.toml" ]]; then
  echo "Error: CLI package was not found at '${CLI_PACKAGE_DIR}'." >&2
  exit 1
fi

echo "Installing jelica-cli as a global uv tool..."
uv tool install --directory "${REPO_ROOT}" --editable "${CLI_PACKAGE_DIR}" --force --reinstall

UV_TOOL_BIN_DIR="$(uv tool dir --bin)"
if [[ "${UV_TOOL_BIN_DIR}" == "" ]]; then
  echo "Error: uv returned an empty tools bin directory path." >&2
  exit 1
fi

SHELL_PATH_UPDATED=0
SHELL_PATH_UPDATE_FAILED=0
if [[ ":${PATH}:" != *":${UV_TOOL_BIN_DIR}:"* ]]; then
  echo "uv tool bin directory is not in current PATH. Updating shell configuration..."
  if uv tool update-shell; then
    SHELL_PATH_UPDATED=1
  else
    SHELL_PATH_UPDATE_FAILED=1
  fi
fi

if [[ ":${PATH}:" != *":${UV_TOOL_BIN_DIR}:"* ]]; then
  export PATH="${UV_TOOL_BIN_DIR}:${PATH}"
  echo "Temporarily added '${UV_TOOL_BIN_DIR}' to PATH for this install.sh run."
fi

if ! command -v jelica >/dev/null 2>&1; then
  echo "Error: 'jelica' command is still not available after installation." >&2
  exit 1
fi

CONFIG_PATH="$(jelica config path)"
echo "System config path: ${CONFIG_PATH}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "System config is missing. Initializing with defaults..."
  jelica config init --non-interactive
else
  echo "System config already exists. Skipping initialization."
fi

echo "Verifying CLI version..."
jelica --version

if [[ "${SHELL_PATH_UPDATED}" -eq 1 ]]; then
  echo "Shell configuration was updated successfully."
  echo "Open a new terminal or restart your login shell to apply the persistent PATH change."
fi

if [[ "${SHELL_PATH_UPDATE_FAILED}" -eq 1 ]]; then
  echo "Warning: failed to update shell configuration automatically via 'uv tool update-shell'."
  echo "JELICA is installed and works in this run because PATH was updated temporarily."
  echo "Run this command manually to persist PATH:"
  echo "  uv tool update-shell"
fi

echo "Installation completed successfully."
