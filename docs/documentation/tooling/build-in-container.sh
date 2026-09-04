#!/usr/bin/env bash
set -euo pipefail

target="${1:-all}"
locale="${2:-en}"
profile="${3:-screen}"
text_size="${4:-standard}"

case "$target" in all|pdf|html|release) ;; *) echo "Invalid build target: ${target}" >&2; exit 2 ;; esac
case "$locale" in en|ru|sr-Latn|sr-Cyrl) ;; *) echo "Unsupported locale: ${locale}" >&2; exit 2 ;; esac
case "$profile" in screen|print) ;; *) echo "Invalid profile: ${profile}" >&2; exit 2 ;; esac
case "$text_size" in small|standard|large) ;; *) echo "Invalid text size: ${text_size}" >&2; exit 2 ;; esac

docs_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_root="${docs_root}/source/${locale}"
template_root="${docs_root}/template/latex"
artifact_root="${docs_root}/build/${locale}/${profile}-${text_size}"
work_root="${artifact_root}/work"
wrapper_name="jelica-documentation-driver"
wrapper="${work_root}/${wrapper_name}.tex"
pdf_name="jelica-documentation-${locale}-${profile}-${text_size}.pdf"

[[ -f "${source_root}/main.tex" ]] || {
  echo "Documentation source for locale is unavailable: ${locale}" >&2
  exit 1
}

if [[ "$target" == "all" || "$target" == "release" ]]; then
  rm -rf "$artifact_root"
elif [[ "$target" == "pdf" ]]; then
  rm -rf "${artifact_root}/pdf" "${work_root}/pdf"
else
  rm -rf "${artifact_root}/pdf" "${work_root}/pdf"
  rm -rf "${artifact_root}/html" "${work_root}/html"
  rm -f \
    "${artifact_root}/documentation-manifest.json" \
    "${artifact_root}/search-index.json" \
    "${artifact_root}/version.json"
fi

mkdir -p "$work_root"
printf '%s\n' \
  "\\def\\JelicaBuildLocale{${locale}}" \
  "\\def\\JelicaProfile{${profile}}" \
  "\\def\\JelicaTextSize{${text_size}}" \
  "\\input{main.tex}" > "$wrapper"

export TEXINPUTS="${template_root}//:"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1767225600}"
export FORCE_SOURCE_DATE=1
export TZ=UTC
export LC_ALL=C.UTF-8

cd "$source_root"

if [[ "$target" == "all" || "$target" == "pdf" || "$target" == "release" ]]; then
  pdf_work="${work_root}/pdf"
  pdf_output="${artifact_root}/pdf"
  mkdir -p "$pdf_work" "$pdf_output"
  latexmk -lualatex \
    -interaction=nonstopmode \
    -halt-on-error \
    -file-line-error \
    -outdir="$pdf_work" \
    "$wrapper"
  cp "${pdf_work}/${wrapper_name}.pdf" "${pdf_output}/${pdf_name}"
fi

if [[ "$target" == "all" || "$target" == "html" || "$target" == "release" ]]; then
  html_work="${work_root}/html"
  html_output="${artifact_root}/html"
  mkdir -p "$html_work" "$html_output"
  make4ht -l -u \
    -f html5+copy_images \
    -c "${template_root}/jelica-html.cfg" \
    -B "$html_work" \
    -d "$html_output" \
    "$wrapper"
  shopt -s nullglob
  generated_html=("${html_work}"/*.html)
  generated_css=("${html_work}"/*.css)
  [[ ${#generated_html[@]} -gt 0 ]] || {
    echo "make4ht completed without producing HTML pages." >&2
    exit 1
  }
  cp "${generated_html[@]}" "$html_output/"
  if [[ ${#generated_css[@]} -gt 0 ]]; then
    cp "${generated_css[@]}" "$html_output/"
  fi
  shopt -u nullglob
  python3 "${docs_root}/tooling/generate-artifacts.py" \
    --docs-root "$docs_root" \
    --locale "$locale" \
    --profile "$profile" \
    --size "$text_size" \
    --artifact-root "$artifact_root" \
    --job-name "$wrapper_name" \
    --pdf-name "$pdf_name"
  python3 "${docs_root}/tooling/validate-artifacts.py" \
    --docs-root "$docs_root" \
    --artifact-root "$artifact_root"
  if [[ "$target" == "release" ]]; then
    python3 "${docs_root}/tooling/package-release.py" \
      --docs-root "$docs_root" \
      --artifact-root "$artifact_root" \
      --locale "$locale" \
      --profile "$profile" \
      --size "$text_size"
  fi
fi
