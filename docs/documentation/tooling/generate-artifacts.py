#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Heading:
    heading_id: str
    title: str


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str
    keywords: tuple[str, ...]
    headings: tuple[Heading, ...]
    content: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate JELICA documentation client artifacts.")
    parser.add_argument("--docs-root", required=True, type=Path)
    parser.add_argument("--locale", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--size", required=True)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--pdf-name", required=True)
    return parser.parse_args()


def command_value(source: str, command: str) -> str:
    match = re.search(rf"\\{re.escape(command)}\{{([^{{}}]*)\}}", source)
    if match is None:
        raise ValueError(f"Required LaTeX metadata command is missing: \\{command}")
    return match.group(1).strip()


def artifact_format_version(docs_root: Path) -> int:
    version_path = docs_root / "version.json"
    version_data = json.loads(version_path.read_text(encoding="utf-8"))
    value = version_data.get("artifactFormatVersion")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(
            f"artifactFormatVersion must be a positive integer in {version_path}."
        )
    return value


def parse_sections(source_root: Path, main_source: str) -> list[Section]:
    chapter_paths = re.findall(r"\\input\{chapters/([^{}]+)\}", main_source)
    if not chapter_paths:
        raise ValueError("No chapter inputs found in the LaTeX source.")

    sections: list[Section] = []
    for chapter_path in chapter_paths:
        path = source_root / "chapters" / f"{chapter_path}.tex"
        source = path.read_text(encoding="utf-8")
        chapter_match = re.search(
            r"\\JelicaChapter\{([^{}]+)\}\{([^{}]+)\}\{([^{}]*)\}", source
        )
        if chapter_match is None:
            raise ValueError(f"Chapter metadata is missing in {path}.")
        heading_matches = re.findall(r"\\JelicaSection\{([^{}]+)\}\{([^{}]+)\}", source)
        keywords = tuple(
            item.strip() for item in chapter_match.group(3).split(",") if item.strip()
        )
        sections.append(
            Section(
                section_id=chapter_match.group(1),
                title=chapter_match.group(2),
                keywords=keywords,
                headings=tuple(Heading(item[0], item[1]) for item in heading_matches),
                content=latex_plain_text(source),
            )
        )
    return sections


def latex_plain_text(source: str) -> str:
    text = re.sub(r"(?m)(?<!\\)%.*$", "", source)
    text = re.sub(r"\\JelicaChapter\{[^{}]+\}\{[^{}]+\}\{[^{}]*\}", "", text)
    text = re.sub(r"\\JelicaSection\{[^{}]+\}\{([^{}]+)\}", r"\1. ", text)
    text = re.sub(r"\\(?:textbf|emph|textit|texttt)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:begin|end)\{[^{}]+\}", " ", text)
    text = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?", " ", text)
    text = text.replace(r"\&", "&").replace("~", " ")
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def normalize_html(
    html_dir: Path,
    job_name: str,
    css_source: Path,
    document_title: str,
    text_size: str,
) -> list[Path]:
    primary = html_dir / f"{job_name}.html"
    index = html_dir / "index.html"
    if primary.exists():
        primary.rename(index)

    css_target = html_dir / "jelica-doc.css"
    shutil.copyfile(css_source, css_target)
    root_sizes = {"small": "15px", "standard": "16px", "large": "18px"}
    if text_size not in root_sizes:
        raise ValueError(f"Unsupported HTML text size: {text_size}")
    with css_target.open("a", encoding="utf-8") as stylesheet:
        stylesheet.write(
            f'\n:root {{ font-size: {root_sizes[text_size]}; }}\n'
        )

    html_files = sorted(html_dir.glob("*.html"))
    if not html_files:
        raise ValueError(f"make4ht did not produce HTML files in {html_dir}.")

    for path in html_files:
        document = path.read_text(encoding="utf-8")
        document = re.sub(
            r"<html(?=\s|>)",
            f'<html data-documentation-text-size="{html.escape(text_size)}"',
            document,
            count=1,
            flags=re.IGNORECASE,
        )
        document = document.replace(f"{job_name}.html", "index.html")
        document = re.sub(
            r"<title>\s*</title>",
            f"<title>{html.escape(document_title)}</title>",
            document,
            count=1,
            flags=re.IGNORECASE,
        )
        document = document.replace("alt='PIC'", "alt='JELICA'")
        document = re.sub(
            r"(<img\s+alt='JELICA')\s+height='[^']*'([^>]*?)\s+width='[^']*'",
            r"\1\2",
            document,
            count=1,
            flags=re.IGNORECASE,
        )
        if "jelica-doc.css" not in document:
            stylesheet = '<link rel="stylesheet" href="jelica-doc.css" />'
            document = re.sub(
                r"</head>", f"{stylesheet}\n</head>", document, count=1, flags=re.IGNORECASE
            )
        path.write_text(document, encoding="utf-8")
    return html_files


def stabilize_section_pages(html_files: list[Path], sections: list[Section]) -> list[Path]:
    rename_map: dict[str, str] = {}
    for section in sections:
        current = anchor_page(html_files, section.section_id)
        if current.name == "index.html":
            continue
        desired = current.with_name(f"{section.section_id}.html")
        if desired.exists() and desired != current:
            raise ValueError(f"Stable HTML page already exists: {desired}")
        if current != desired:
            rename_map[current.name] = desired.name
            current.rename(desired)
            html_files = [desired if item == current else item for item in html_files]

    for path in html_files:
        document = path.read_text(encoding="utf-8")
        for previous, current in rename_map.items():
            document = document.replace(previous, current)
        document = re.sub(
            r"index\.html#[^\"']+\.html",
            "index.html",
            document,
            flags=re.IGNORECASE,
        )
        path.write_text(document, encoding="utf-8")
    return sorted(html_files)


def add_index_navigation(index_path: Path, sections: list[dict[str, object]]) -> None:
    items = []
    for section in sections:
        pages = section["pages"]
        if not isinstance(pages, list) or not pages:
            continue
        page = pages[0]
        if not isinstance(page, dict):
            continue
        path = str(page["path"]).removeprefix("html/")
        anchor = str(page["anchor"])
        items.append(
            f'<li><a href="{html.escape(path + anchor)}">'
            f'{html.escape(str(section["title"]))}</a></li>'
        )
    navigation = (
        '<nav class="documentation-contents" aria-label="Documentation contents">'
        "<h1>Contents</h1><ol>"
        + "".join(items)
        + "</ol></nav>"
    )
    document = index_path.read_text(encoding="utf-8")
    document = re.sub(r"</body>", f"{navigation}\n</body>", document, count=1, flags=re.I)
    index_path.write_text(document, encoding="utf-8")


def anchor_page(html_files: list[Path], anchor: str) -> Path:
    escaped = re.escape(anchor)
    pattern = re.compile(rf"(?:id|name)=[\"']{escaped}[\"']", re.IGNORECASE)
    for path in html_files:
        if pattern.search(path.read_text(encoding="utf-8")):
            return path
    raise ValueError(f"Stable HTML anchor was not generated: #{anchor}")


def main() -> None:
    args = parse_args()
    source_root = args.docs_root / "source" / args.locale
    main_path = source_root / "main.tex"
    main_source = main_path.read_text(encoding="utf-8")
    declared_locale = command_value(main_source, "JelicaSetLocale")
    if declared_locale != args.locale:
        raise ValueError(
            f"Documentation source locale {declared_locale!r} does not match "
            f"the selected locale {args.locale!r}."
        )
    sections = parse_sections(source_root, main_source)

    document_title = command_value(main_source, "JelicaSetTitle")
    document_version = command_value(main_source, "JelicaSetVersion")
    format_version = artifact_format_version(args.docs_root)
    html_dir = args.artifact_root / "html"
    html_files = normalize_html(
        html_dir,
        args.job_name,
        args.docs_root / "template" / "html" / "jelica-doc.css",
        document_title,
        args.size,
    )
    html_files = stabilize_section_pages(html_files, sections)

    section_entries: list[dict[str, object]] = []
    search_documents: list[dict[str, object]] = []
    page_entries: list[dict[str, str]] = []

    for section in sections:
        page = anchor_page(html_files, section.section_id)
        page_path = f"html/{page.name}"
        headings = []
        for heading in section.headings:
            heading_page = anchor_page(html_files, heading.heading_id)
            headings.append(
                {
                    "id": heading.heading_id,
                    "title": heading.title,
                    "path": f"html/{heading_page.name}",
                    "anchor": f"#{heading.heading_id}",
                }
            )
        section_entries.append(
            {
                "id": section.section_id,
                "title": section.title,
                "keywords": list(section.keywords),
                "anchor": f"#{section.section_id}",
                "pages": [{"path": page_path, "anchor": f"#{section.section_id}"}],
                "headings": headings,
            }
        )
        page_entries.append({"id": section.section_id, "title": section.title, "path": page_path})
        search_documents.append(
            {
                "id": section.section_id,
                "title": section.title,
                "headings": [item.title for item in section.headings],
                "headingAnchors": [f"#{item.heading_id}" for item in section.headings],
                "keywords": list(section.keywords),
                "path": page_path,
                "anchor": f"#{section.section_id}",
                "content": html.unescape(section.content),
            }
        )

    add_index_navigation(html_dir / "index.html", section_entries)

    pdf_path = args.artifact_root / "pdf" / args.pdf_name
    manifest = {
        "schemaVersion": format_version,
        "locale": args.locale,
        "title": document_title,
        "subtitle": command_value(main_source, "JelicaSetSubtitle"),
        "version": document_version,
        "year": int(command_value(main_source, "JelicaSetYear")),
        "profile": args.profile,
        "textSize": args.size,
        "paths": {
            "html": "html/index.html",
            "pdf": f"pdf/{args.pdf_name}" if pdf_path.exists() else None,
            "search": "search-index.json",
            "version": "version.json",
        },
        "sections": section_entries,
        "pages": page_entries,
    }
    search_index = {
        "schemaVersion": format_version,
        "locale": args.locale,
        "title": document_title,
        "version": document_version,
        "fieldPriority": ["title", "headings", "keywords", "content"],
        "documents": search_documents,
    }
    version_metadata = {
        "artifactFormatVersion": format_version,
        "documentationVersion": document_version,
    }

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    (args.artifact_root / "documentation-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.artifact_root / "search-index.json").write_text(
        json.dumps(search_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.artifact_root / "version.json").write_text(
        json.dumps(version_metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
