#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


SUPPORTED_SCHEMA_KEYS = {
    "$schema",
    "$id",
    "$ref",
    "$defs",
    "$comment",
    "title",
    "description",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "uniqueItems",
    "minLength",
    "pattern",
    "minimum",
    "maximum",
    "minProperties",
}

HTML_REFERENCE_ATTRIBUTES = {
    "a": ("href",),
    "audio": ("src",),
    "iframe": ("src",),
    "img": ("src",),
    "link": ("href",),
    "object": ("data",),
    "script": ("src",),
    "source": ("src",),
    "video": ("src", "poster"),
}

CSS_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
SUPPORTED_LOCALES = {"en", "ru", "sr-Latn", "sr-Cyrl"}
SUPPORTED_PROFILES = {"screen", "print"}
SUPPORTED_TEXT_SIZES = {"small", "standard", "large"}


class ContractError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("Documentation artifact validation failed.")
        self.errors = errors


class HtmlContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: set[str] = set()
        self.references: list[str] = []
        self.security_errors: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {name.lower(): value for name, value in attrs if value is not None}
        if tag.lower() == "script":
            self.security_errors.append("script elements are not allowed")
        if any(name.lower().startswith("on") for name, _value in attrs):
            self.security_errors.append("inline event handlers are not allowed")
        for name in ("id", "name"):
            value = values.get(name)
            if value:
                self.anchors.add(value)
        for name in HTML_REFERENCE_ATTRIBUTES.get(tag.lower(), ()):
            value = values.get(name)
            if value:
                self.references.append(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate generated JELICA documentation artifacts."
    )
    parser.add_argument("--docs-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument(
        "--inventory-output",
        type=Path,
        help="Write the validated, distributable artifact paths to this JSON file.",
    )
    return parser.parse_args()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def load_json(file_path: Path, label: str) -> Any:
    try:
        return json.loads(
            file_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except FileNotFoundError as error:
        raise ContractError([f"{label}: file does not exist: {file_path}"]) from error
    except UnicodeDecodeError as error:
        raise ContractError([f"{label}: file is not valid UTF-8: {error}"]) from error
    except json.JSONDecodeError as error:
        raise ContractError(
            [
                f"{label}: invalid JSON at line {error.lineno}, "
                f"column {error.colno}: {error.msg}"
            ]
        ) from error
    except ValueError as error:
        raise ContractError([f"{label}: invalid JSON: {error}"]) from error


def schema_location(parent: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{parent}.{key}"
    return f"{parent}[{key!r}]"


def check_schema_keywords(schema: Any, location: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{location}: schema node must be an object")
        return
    for key in schema:
        if key not in SUPPORTED_SCHEMA_KEYS:
            errors.append(f"{location}: unsupported JSON Schema keyword {key!r}")
    for collection_name in ("$defs", "properties"):
        collection = schema.get(collection_name, {})
        if not isinstance(collection, dict):
            errors.append(f"{location}.{collection_name}: must be an object")
            continue
        for name, child in collection.items():
            check_schema_keywords(
                child, schema_location(f"{location}.{collection_name}", name), errors
            )
    for child_name in ("items", "additionalProperties"):
        child = schema.get(child_name)
        if isinstance(child, dict):
            check_schema_keywords(child, f"{location}.{child_name}", errors)


def json_identity(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def matches_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


class SchemaValidator:
    def __init__(self, schema: dict[str, Any], label: str) -> None:
        self.schema = schema
        self.label = label

    def validate(self, instance: Any) -> list[str]:
        errors: list[str] = []
        check_schema_keywords(self.schema, f"{self.label}:schema", errors)
        if errors:
            return errors
        self._validate(instance, self.schema, "$", errors)
        return errors

    def _resolve_ref(self, reference: str, location: str, errors: list[str]) -> Any:
        if not reference.startswith("#/"):
            errors.append(f"{self.label}:{location}: only local $ref values are supported")
            return None
        current: Any = self.schema
        for token in reference[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or token not in current:
                errors.append(
                    f"{self.label}:{location}: unresolved schema reference {reference!r}"
                )
                return None
            current = current[token]
        return current

    def _validate(
        self,
        instance: Any,
        schema: dict[str, Any],
        location: str,
        errors: list[str],
    ) -> None:
        reference = schema.get("$ref")
        if reference is not None:
            resolved = self._resolve_ref(reference, location, errors)
            if isinstance(resolved, dict):
                self._validate(instance, resolved, location, errors)

        expected_types = schema.get("type")
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if isinstance(expected_types, list) and not any(
            matches_json_type(instance, expected) for expected in expected_types
        ):
            errors.append(
                f"{self.label}:{location}: expected type "
                f"{' or '.join(str(item) for item in expected_types)}"
            )
            return

        if "const" in schema and json_identity(instance) != json_identity(schema["const"]):
            errors.append(
                f"{self.label}:{location}: expected constant value {schema['const']!r}"
            )
        if "enum" in schema and not any(
            json_identity(instance) == json_identity(item) for item in schema["enum"]
        ):
            errors.append(f"{self.label}:{location}: value is not in the allowed enum")

        if isinstance(instance, str):
            minimum_length = schema.get("minLength")
            if isinstance(minimum_length, int) and len(instance) < minimum_length:
                errors.append(
                    f"{self.label}:{location}: must contain at least "
                    f"{minimum_length} character(s)"
                )
            pattern = schema.get("pattern")
            if isinstance(pattern, str):
                try:
                    matches = re.search(pattern, instance) is not None
                except re.error as error:
                    errors.append(
                        f"{self.label}:{location}: invalid schema pattern {pattern!r}: {error}"
                    )
                else:
                    if not matches:
                        errors.append(
                            f"{self.label}:{location}: does not match pattern {pattern!r}"
                        )

        if isinstance(instance, (int, float)) and not isinstance(instance, bool):
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            if isinstance(minimum, (int, float)) and instance < minimum:
                errors.append(f"{self.label}:{location}: must be at least {minimum}")
            if isinstance(maximum, (int, float)) and instance > maximum:
                errors.append(f"{self.label}:{location}: must be at most {maximum}")

        if isinstance(instance, list):
            minimum_items = schema.get("minItems")
            if isinstance(minimum_items, int) and len(instance) < minimum_items:
                errors.append(
                    f"{self.label}:{location}: must contain at least "
                    f"{minimum_items} item(s)"
                )
            if schema.get("uniqueItems") is True:
                identities = [json_identity(item) for item in instance]
                if len(set(identities)) != len(identities):
                    errors.append(f"{self.label}:{location}: items must be unique")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(instance):
                    self._validate(item, item_schema, f"{location}[{index}]", errors)

        if isinstance(instance, dict):
            minimum_properties = schema.get("minProperties")
            if isinstance(minimum_properties, int) and len(instance) < minimum_properties:
                errors.append(
                    f"{self.label}:{location}: must contain at least "
                    f"{minimum_properties} propertie(s)"
                )
            required = schema.get("required", [])
            if isinstance(required, list):
                for name in required:
                    if name not in instance:
                        errors.append(
                            f"{self.label}:{location}: missing required property {name!r}"
                        )
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            if isinstance(properties, dict):
                for name, value in instance.items():
                    child_location = schema_location(location, name)
                    if name in properties and isinstance(properties[name], dict):
                        self._validate(value, properties[name], child_location, errors)
                    elif additional is False:
                        errors.append(
                            f"{self.label}:{child_location}: additional property is not allowed"
                        )
                    elif isinstance(additional, dict):
                        self._validate(value, additional, child_location, errors)


def add_unique_errors(values: list[str], label: str, errors: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            errors.append(f"{label}: duplicate identifier {value!r}")
        seen.add(value)


class ArtifactResolver:
    def __init__(self, artifact_root: Path, errors: list[str]) -> None:
        self.root = artifact_root.resolve()
        self.errors = errors
        self.referenced_files: set[Path] = set()

    def _inside_root(self, candidate: Path, label: str) -> Path | None:
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            self.errors.append(f"{label}: path escapes artifact root: {candidate}")
            return None
        if not resolved.is_file():
            self.errors.append(f"{label}: referenced file does not exist: {candidate}")
            return None
        self.referenced_files.add(resolved)
        return resolved

    def metadata_path(self, value: str, label: str) -> Path | None:
        parsed = urlsplit(value)
        decoded = unquote(parsed.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            self.errors.append(f"{label}: must be a plain relative artifact path")
            return None
        if not decoded or decoded.startswith("/") or "\\" in decoded:
            self.errors.append(f"{label}: must be a portable relative artifact path")
            return None
        parts = decoded.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            self.errors.append(f"{label}: dot or empty path segments are not allowed")
            return None
        return self._inside_root(self.root.joinpath(*parts), label)

    def html_reference(
        self, owner: Path, value: str, label: str
    ) -> tuple[Path, str] | None:
        parsed = urlsplit(value)
        if parsed.scheme:
            if parsed.scheme.lower() in {"http", "https", "mailto", "data"}:
                return None
            self.errors.append(f"{label}: unsafe URL scheme {parsed.scheme!r}")
            return None
        if parsed.netloc:
            self.errors.append(f"{label}: protocol-relative URLs are not allowed")
            return None
        decoded = unquote(parsed.path)
        fragment = unquote(parsed.fragment)
        if not decoded:
            candidate = owner
        else:
            if decoded.startswith("/") or "\\" in decoded:
                self.errors.append(f"{label}: local reference must remain relative")
                return None
            parts = decoded.split("/")
            if any(part in {"", ".", ".."} for part in parts):
                self.errors.append(f"{label}: local reference contains an unsafe path segment")
                return None
            candidate = owner.parent.joinpath(*parts)
        resolved = self._inside_root(candidate, label)
        if resolved is None:
            return None
        return resolved, fragment


def parse_html(
    file_path: Path,
    cache: dict[Path, HtmlContractParser],
    errors: list[str],
) -> HtmlContractParser | None:
    if file_path in cache:
        return cache[file_path]
    try:
        document = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        errors.append(f"HTML {file_path}: cannot be read as UTF-8: {error}")
        return None
    parser = HtmlContractParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as error:
        errors.append(f"HTML {file_path}: cannot be parsed: {error}")
        return None
    cache[file_path] = parser
    for error in parser.security_errors:
        errors.append(f"HTML {file_path}: {error}")
    return parser


def check_html_anchor(
    file_path: Path | None,
    anchor: str,
    label: str,
    cache: dict[Path, HtmlContractParser],
    errors: list[str],
) -> None:
    if file_path is None or file_path.suffix.lower() not in {".html", ".htm"}:
        errors.append(f"{label}: anchors must reference an HTML page")
        return
    parser = parse_html(file_path, cache, errors)
    if parser is not None and anchor.removeprefix("#") not in parser.anchors:
        errors.append(f"{label}: anchor {anchor!r} is missing from {file_path.name}")


def validate_html_references(
    initial_pages: set[Path],
    resolver: ArtifactResolver,
    cache: dict[Path, HtmlContractParser],
    errors: list[str],
) -> None:
    pending = list(initial_pages)
    visited: set[Path] = set()
    css_files: set[Path] = set()
    anchor_checks: list[tuple[Path, str, str]] = []

    while pending:
        page = pending.pop()
        if page in visited:
            continue
        visited.add(page)
        parser = parse_html(page, cache, errors)
        if parser is None:
            continue
        for index, reference in enumerate(parser.references):
            label = f"HTML {page.name} reference #{index + 1}"
            resolved = resolver.html_reference(page, reference, label)
            if resolved is None:
                continue
            target, fragment = resolved
            suffix = target.suffix.lower()
            if suffix in {".html", ".htm"}:
                pending.append(target)
            elif suffix == ".css":
                css_files.add(target)
            if fragment:
                anchor_checks.append((target, f"#{fragment}", label))

    for stylesheet in css_files:
        try:
            content = stylesheet.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f"CSS {stylesheet}: cannot be read as UTF-8: {error}")
            continue
        for index, match in enumerate(CSS_URL_PATTERN.finditer(content)):
            resolver.html_reference(
                stylesheet,
                match.group(2),
                f"CSS {stylesheet.name} url() reference #{index + 1}",
            )

    for file_path, anchor, label in anchor_checks:
        check_html_anchor(file_path, anchor, label, cache, errors)


def validate_contract(
    artifact_root: Path,
    manifest: dict[str, Any],
    search_index: dict[str, Any],
    source_version: dict[str, Any],
    generated_version: dict[str, Any],
) -> tuple[list[str], set[Path]]:
    errors: list[str] = []
    resolver = ArtifactResolver(artifact_root, errors)
    html_cache: dict[Path, HtmlContractParser] = {}
    html_pages: set[Path] = set()

    format_version = source_version.get("artifactFormatVersion")
    if (
        not isinstance(format_version, int)
        or isinstance(format_version, bool)
        or format_version < 1
    ):
        errors.append("source version.json: artifactFormatVersion must be a positive integer")
    if generated_version.get("artifactFormatVersion") != format_version:
        errors.append("version.json: artifactFormatVersion does not match the source contract")
    if generated_version.get("documentationVersion") != manifest["version"]:
        errors.append("version.json: documentationVersion does not match manifest.version")
    if manifest["schemaVersion"] != format_version:
        errors.append("manifest.schemaVersion does not match artifactFormatVersion")
    if search_index["schemaVersion"] != format_version:
        errors.append("search-index.schemaVersion does not match artifactFormatVersion")

    for field in ("locale", "title", "version"):
        if manifest[field] != search_index[field]:
            errors.append(f"manifest.{field} does not match search-index.{field}")
    if manifest["locale"] not in SUPPORTED_LOCALES:
        errors.append(f"manifest.locale is not supported: {manifest['locale']!r}")
    if manifest["profile"] not in SUPPORTED_PROFILES:
        errors.append(f"manifest.profile is not supported: {manifest['profile']!r}")
    if manifest["textSize"] not in SUPPORTED_TEXT_SIZES:
        errors.append(f"manifest.textSize is not supported: {manifest['textSize']!r}")

    resolved_contract_paths: dict[str, Path] = {}
    for name, value in manifest["paths"].items():
        if value is None:
            continue
        resolved = resolver.metadata_path(value, f"manifest.paths.{name}")
        if resolved is not None:
            resolved_contract_paths[name] = resolved
            if resolved.suffix.lower() in {".html", ".htm"}:
                html_pages.add(resolved)
            elif name == "pdf":
                try:
                    if resolved.read_bytes()[:5] != b"%PDF-":
                        errors.append("manifest.paths.pdf is not a PDF file")
                except OSError as error:
                    errors.append(f"manifest.paths.pdf cannot be read: {error}")

    expected_files = {
        "search": (artifact_root / "search-index.json").resolve(),
        "version": (artifact_root / "version.json").resolve(),
    }
    for name, expected in expected_files.items():
        actual = resolved_contract_paths.get(name)
        if actual != expected:
            errors.append(f"manifest.paths.{name} must reference {expected.name}")

    section_ids = [section["id"] for section in manifest["sections"]]
    page_ids = [page["id"] for page in manifest["pages"]]
    document_ids = [document["id"] for document in search_index["documents"]]
    heading_ids = [
        heading["id"]
        for section in manifest["sections"]
        for heading in section["headings"]
    ]
    add_unique_errors(section_ids, "manifest.sections", errors)
    add_unique_errors(page_ids, "manifest.pages", errors)
    add_unique_errors(document_ids, "search-index.documents", errors)
    add_unique_errors(heading_ids, "manifest headings", errors)
    overlap = set(section_ids).intersection(heading_ids)
    if overlap:
        errors.append(f"public section/heading identifiers overlap: {sorted(overlap)}")

    pages_by_id = {page["id"]: page for page in manifest["pages"]}
    documents_by_id = {
        document["id"]: document for document in search_index["documents"]
    }
    if set(page_ids) != set(document_ids):
        missing = sorted(set(page_ids) - set(document_ids))
        extra = sorted(set(document_ids) - set(page_ids))
        if missing:
            errors.append(f"search index is missing manifest page IDs: {missing}")
        if extra:
            errors.append(f"search index contains unknown page IDs: {extra}")

    for index, page in enumerate(manifest["pages"]):
        resolved = resolver.metadata_path(page["path"], f"manifest.pages[{index}].path")
        if resolved is not None and resolved.suffix.lower() in {".html", ".htm"}:
            html_pages.add(resolved)

    for section_index, section in enumerate(manifest["sections"]):
        expected_anchor = f"#{section['id']}"
        if section["anchor"] != expected_anchor:
            errors.append(
                f"manifest.sections[{section_index}].anchor must equal {expected_anchor!r}"
            )
        for page_index, page_reference in enumerate(section["pages"]):
            label = f"manifest.sections[{section_index}].pages[{page_index}]"
            resolved = resolver.metadata_path(page_reference["path"], f"{label}.path")
            if resolved is not None:
                html_pages.add(resolved)
            check_html_anchor(
                resolved,
                page_reference["anchor"],
                f"{label}.anchor",
                html_cache,
                errors,
            )
        for heading_index, heading in enumerate(section["headings"]):
            label = f"manifest.sections[{section_index}].headings[{heading_index}]"
            expected_heading_anchor = f"#{heading['id']}"
            if heading["anchor"] != expected_heading_anchor:
                errors.append(f"{label}.anchor must equal {expected_heading_anchor!r}")
            resolved = resolver.metadata_path(heading["path"], f"{label}.path")
            if resolved is not None:
                html_pages.add(resolved)
            check_html_anchor(
                resolved, heading["anchor"], f"{label}.anchor", html_cache, errors
            )

        page = pages_by_id.get(section["id"])
        document = documents_by_id.get(section["id"])
        if page is None:
            errors.append(f"manifest section {section['id']!r} has no matching page")
            continue
        first_section_page = section["pages"][0]
        if page["title"] != section["title"]:
            errors.append(f"manifest page {page['id']!r} title differs from its section")
        if page["path"] != first_section_page["path"]:
            errors.append(f"manifest page {page['id']!r} path differs from its section")
        if document is None:
            continue
        expected_values = {
            "title": page["title"],
            "path": page["path"],
            "anchor": section["anchor"],
            "headings": [heading["title"] for heading in section["headings"]],
            "keywords": section["keywords"],
        }
        for field, expected in expected_values.items():
            if document[field] != expected:
                errors.append(
                    f"search document {document['id']!r} {field} differs from manifest"
                )
        heading_anchors = document.get("headingAnchors")
        if heading_anchors is not None and heading_anchors != [
            heading["anchor"] for heading in section["headings"]
        ]:
            errors.append(
                f"search document {document['id']!r} headingAnchors differs from manifest"
            )

    for index, document in enumerate(search_index["documents"]):
        label = f"search-index.documents[{index}]"
        expected_anchor = f"#{document['id']}"
        if document["anchor"] != expected_anchor:
            errors.append(f"{label}.anchor must equal {expected_anchor!r}")
        resolved = resolver.metadata_path(document["path"], f"{label}.path")
        if resolved is not None:
            html_pages.add(resolved)
        check_html_anchor(
            resolved, document["anchor"], f"{label}.anchor", html_cache, errors
        )

    validate_html_references(html_pages, resolver, html_cache, errors)
    return errors, resolver.referenced_files


def main() -> int:
    args = parse_args()
    docs_root = args.docs_root.resolve()
    artifact_root = args.artifact_root.resolve()

    try:
        manifest = load_json(
            artifact_root / "documentation-manifest.json", "documentation manifest"
        )
        search_index = load_json(artifact_root / "search-index.json", "search index")
        source_version = load_json(docs_root / "version.json", "source version")
        generated_version = load_json(artifact_root / "version.json", "artifact version")
        manifest_schema = load_json(
            docs_root / "schema" / "documentation-manifest.schema.json",
            "manifest schema",
        )
        search_schema = load_json(
            docs_root / "schema" / "search-index.schema.json", "search schema"
        )
    except ContractError as error:
        for message in error.errors:
            print(f"ERROR: {message}", file=sys.stderr)
        return 1

    schema_errors = SchemaValidator(
        manifest_schema, "documentation-manifest.json"
    ).validate(manifest)
    schema_errors.extend(
        SchemaValidator(search_schema, "search-index.json").validate(search_index)
    )
    if schema_errors:
        for message in sorted(schema_errors):
            print(f"ERROR: {message}", file=sys.stderr)
        return 1

    contract_errors, referenced_files = validate_contract(
        artifact_root,
        manifest,
        search_index,
        source_version,
        generated_version,
    )
    if contract_errors:
        for message in sorted(set(contract_errors)):
            print(f"ERROR: {message}", file=sys.stderr)
        return 1

    if args.inventory_output is not None:
        inventory_paths = {"documentation-manifest.json"}
        inventory_paths.update(
            file_path.relative_to(artifact_root).as_posix()
            for file_path in referenced_files
        )
        args.inventory_output.parent.mkdir(parents=True, exist_ok=True)
        args.inventory_output.write_text(
            json.dumps({"files": sorted(inventory_paths)}, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        "Documentation artifacts valid: "
        f"format v{source_version['artifactFormatVersion']}, "
        f"locale {manifest['locale']}, "
        f"{len(manifest['pages'])} pages, "
        f"{len(search_index['documents'])} search documents, "
        f"{len(referenced_files)} referenced files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
