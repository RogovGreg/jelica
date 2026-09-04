#!/usr/bin/env bash
set -euo pipefail

docs_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${docs_root}/../.." && pwd)"
toolchain_image="${JELICA_DOCS_TEX_IMAGE:-texlive/texlive@sha256:7ff9aba34e665d3899215008a65e72eb2a8faaf569e846c34303827f805e81a5}"
use_local_toolchain="${JELICA_DOCS_USE_LOCAL_TOOLCHAIN:-0}"

target="all"
locale="en"
profile="screen"
text_size="standard"

usage() {
  echo "Usage: $0 [all|pdf|html|validate|release] [--locale en|ru|sr-Latn|sr-Cyrl] [--profile screen|print] [--size small|standard|large]"
}

if [[ $# -gt 0 && "${1}" != --* ]]; then
  target="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --locale)
      [[ $# -ge 2 ]] || { echo "Missing value for --locale." >&2; exit 2; }
      locale="$2"
      shift 2
      ;;
    --profile)
      [[ $# -ge 2 ]] || { echo "Missing value for --profile." >&2; exit 2; }
      profile="$2"
      shift 2
      ;;
    --size)
      [[ $# -ge 2 ]] || { echo "Missing value for --size." >&2; exit 2; }
      text_size="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$target" in
  all|pdf|html|validate|release) ;;
  *) echo "Unknown build target: ${target}. Choose all, pdf, html, validate, or release." >&2; exit 2 ;;
esac
case "$locale" in
  en|ru|sr-Latn|sr-Cyrl) ;;
  *) echo "Unsupported documentation locale '${locale}'. Choose en, ru, sr-Latn, or sr-Cyrl." >&2; exit 2 ;;
esac
case "$profile" in
  screen|print) ;;
  *) echo "Unknown profile '${profile}'. Choose screen or print." >&2; exit 2 ;;
esac
case "$text_size" in
  small|standard|large) ;;
  *) echo "Unknown text size '${text_size}'. Choose small, standard, or large." >&2; exit 2 ;;
esac

if [[ "$target" != "validate" && ! -f "${docs_root}/source/${locale}/main.tex" ]]; then
  echo "Documentation source for locale is unavailable: ${locale}" >&2
  exit 1
fi

if [[ "$target" == "validate" ]]; then
  command -v python3 >/dev/null 2>&1 || {
    echo "Documentation artifact validation requires Python 3." >&2
    exit 1
  }
  python3 "${docs_root}/tooling/validate-artifacts.py" \
    --docs-root "$docs_root" \
    --artifact-root "${docs_root}/build/${locale}/${profile}-${text_size}"
  echo "Documentation artifacts validated: docs/documentation/build/${locale}/${profile}-${text_size}"
  exit 0
fi

export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1767225600}"

case "$use_local_toolchain" in
  0|1) ;;
  *) echo "JELICA_DOCS_USE_LOCAL_TOOLCHAIN must be 0 or 1." >&2; exit 2 ;;
esac

if [[ "$use_local_toolchain" == "1" ]]; then
  command -v lualatex >/dev/null 2>&1 \
    && command -v latexmk >/dev/null 2>&1 \
    && command -v make4ht >/dev/null 2>&1 \
    && command -v python3 >/dev/null 2>&1 || {
      echo "Local builds require LuaLaTeX, latexmk, make4ht, and Python 3." >&2
      exit 1
    }
  "${docs_root}/tooling/build-in-container.sh" "$target" "$locale" "$profile" "$text_size"
else
  command -v docker >/dev/null 2>&1 || {
    echo "The reproducible documentation build requires Docker." >&2
    echo "Set JELICA_DOCS_USE_LOCAL_TOOLCHAIN=1 to opt into an installed local toolchain." >&2
    exit 1
  }
  docker info >/dev/null 2>&1 || {
    echo "Docker is installed but unavailable. Start Docker and rerun the command." >&2
    exit 1
  }
  docker run --rm \
    -e SOURCE_DATE_EPOCH \
    -e TZ=UTC \
    -v "${repo_root}:/workspace" \
    -w /workspace/docs/documentation \
    "$toolchain_image" \
    ./tooling/build-in-container.sh "$target" "$locale" "$profile" "$text_size"
fi

if [[ "$target" == "release" ]]; then
  echo "Documentation release completed for ${locale}/${profile}-${text_size}."
else
  echo "Documentation ${target} build completed: docs/documentation/build/${locale}/${profile}-${text_size}"
fi
