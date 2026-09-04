from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

SUPPORTED_FASTA_EXTENSIONS: tuple[str, ...] = (
    ".fa",
    ".fasta",
    ".fna",
    ".fas",
    ".fsa",
    ".seq",
    ".mfa",
    ".afa",
)
SUPPORTED_GENBANK_EXTENSIONS: tuple[str, ...] = (
    ".gb",
    ".gbk",
    ".gbff",
    ".genbank",
)
SUPPORTED_TEXT_EXTENSIONS: tuple[str, ...] = (".txt",)
SUPPORTED_INPUT_EXTENSIONS: tuple[str, ...] = (
    *SUPPORTED_FASTA_EXTENSIONS,
    *SUPPORTED_GENBANK_EXTENSIONS,
    *SUPPORTED_TEXT_EXTENSIONS,
)
_SUPPORTED_INPUT_EXTENSION_SET = frozenset(SUPPORTED_INPUT_EXTENSIONS)

_IUPAC_INLINE_PATTERN = re.compile(r"^[ACGTRYSWKMBDHVNU]+$", flags=re.IGNORECASE)
_NCBI_ACCESSION_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*\d(?:\.\d+)?$")
_NCBI_ACCESSION_CANDIDATE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_.]*$", flags=re.IGNORECASE)


class InputSourceKind(StrEnum):
    LOCAL_PATH = "local_path"
    NCBI_NUCLEOTIDE_URL = "ncbi_nucleotide_url"
    NCBI_NUCLEOTIDE_ACCESSION = "ncbi_nucleotide_accession"
    INLINE_SEQUENCE = "inline_sequence"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class InputSourceClassification:
    kind: InputSourceKind
    original: str
    normalized: str
    local_path: Path | None = None
    accession: str | None = None
    inline_sequence: str | None = None
    inline_length: int | None = None


def classify_input_source(source: str) -> InputSourceClassification:
    normalized = source.strip()
    path_candidate = Path(normalized).expanduser()
    try:
        path_exists = path_candidate.exists()
    except OSError:
        path_exists = False
    if path_exists:
        return InputSourceClassification(
            kind=InputSourceKind.LOCAL_PATH,
            original=source,
            normalized=normalized,
            local_path=path_candidate,
        )

    url_accession = extract_ncbi_nucleotide_accession_from_url(normalized)
    if url_accession is not None:
        return InputSourceClassification(
            kind=InputSourceKind.NCBI_NUCLEOTIDE_URL,
            original=source,
            normalized=normalized,
            accession=url_accession,
        )

    accession = normalize_ncbi_accession(normalized)
    if accession is not None:
        return InputSourceClassification(
            kind=InputSourceKind.NCBI_NUCLEOTIDE_ACCESSION,
            original=source,
            normalized=normalized,
            accession=accession,
        )

    inline_sequence = normalize_inline_sequence(normalized)
    if inline_sequence is not None:
        return InputSourceClassification(
            kind=InputSourceKind.INLINE_SEQUENCE,
            original=source,
            normalized=normalized,
            inline_sequence=inline_sequence,
            inline_length=len(inline_sequence),
        )

    return InputSourceClassification(
        kind=InputSourceKind.UNSUPPORTED,
        original=source,
        normalized=normalized,
    )


def normalize_inline_sequence(value: str) -> str | None:
    compact = "".join(value.split())
    if compact == "":
        return None
    if _IUPAC_INLINE_PATTERN.fullmatch(compact) is None:
        return None
    return compact.upper()


def normalize_ncbi_accession(value: str) -> str | None:
    normalized = value.strip().upper()
    if normalized == "":
        return None
    if _NCBI_ACCESSION_PATTERN.fullmatch(normalized) is None:
        return None
    return normalized


def looks_like_ncbi_accession(value: str) -> bool:
    normalized = value.strip()
    if normalized == "":
        return False
    if "/" in normalized or "\\" in normalized:
        return False
    return _NCBI_ACCESSION_CANDIDATE_PATTERN.fullmatch(normalized) is not None


def extract_ncbi_nucleotide_accession_from_url(value: str) -> str | None:
    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "ncbi.nlm.nih.gov":
        return None

    path = parsed.path
    segments = [segment for segment in path.split("/") if segment != ""]
    if len(segments) >= 2 and segments[0].lower() in {"nuccore", "nucleotide"}:
        candidate = normalize_ncbi_accession(unquote(segments[1]))
        if candidate is not None:
            return candidate

    if path.lower() in {"/entrez/viewer.fcgi", "/sviewer/viewer.fcgi"}:
        query = parse_qs(parsed.query, keep_blank_values=False)
        db_values = [value.lower() for value in query.get("db", []) if value.strip() != ""]
        if len(db_values) > 0 and all(value != "nuccore" for value in db_values):
            return None
        for key in ("id", "val"):
            values = query.get(key, [])
            if len(values) == 0:
                continue
            candidate = normalize_ncbi_accession(unquote(values[0]))
            if candidate is not None:
                return candidate

    return None


def is_supported_input_extension(path: Path) -> bool:
    return normalized_input_extension(path) is not None


def normalized_input_extension(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _SUPPORTED_INPUT_EXTENSION_SET:
        return suffix
    return None


def looks_like_local_path(value: str) -> bool:
    normalized = value.strip()
    if normalized == "":
        return False
    if normalized.startswith(("~", ".", "/")):
        return True
    if "/" in normalized or "\\" in normalized:
        return True
    suffix = Path(normalized).suffix
    if suffix == "":
        return False
    if suffix[1:].isdigit():
        return False
    return True
