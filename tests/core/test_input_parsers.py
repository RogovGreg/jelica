from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TextIO

import pytest
from Bio import Entrez, SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord

from jelica_core.config import AnalysisAlignmentMode
from jelica_core.runtime.input_parsers import (
    PARSER_ISSUE_FASTA_MALFORMED,
    PARSER_ISSUE_FORMAT_NOT_ALLOWED_FOR_ALIGNMENT_MODE,
    PARSER_ISSUE_GAP_NOT_ALLOWED_FOR_ALIGNMENT_MODE,
    PARSER_ISSUE_GENBANK_MALFORMED,
    PARSER_ISSUE_RECORD_DUPLICATE_ID,
    PARSER_ISSUE_RECORD_ID_MISSING,
    PARSER_ISSUE_RECORD_SEQUENCE_EMPTY,
    PARSER_ISSUE_SEQUENCE_ABSENT,
    InputRecordParser,
    MaterializedInputFile,
    ParsedInputFileResult,
)


def _materialized_file(
    *,
    relative_path: str,
    format_hint: str,
    source_reference: str = "source-a",
) -> MaterializedInputFile:
    return MaterializedInputFile(
        relative_path=relative_path,
        source_type="local_file",
        source_reference=source_reference,
        format_hint=format_hint,
        size_bytes=1,
        sha256="a" * 64,
    )


def _write_text(stage_dir: Path, *, relative_path: str, payload: str) -> None:
    absolute_path = stage_dir / relative_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_text(payload, encoding="utf-8")


def _write_genbank(stage_dir: Path, *, relative_path: str, records: list[SeqRecord]) -> None:
    absolute_path = stage_dir / relative_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    with absolute_path.open("w", encoding="utf-8") as handle:
        SeqIO.write(records, handle, "genbank")


def _parse_text_file(
    tmp_path: Path,
    *,
    payload: str,
    format_hint: str,
    alignment_mode: AnalysisAlignmentMode = AnalysisAlignmentMode.COMPUTE,
    source_reference: str = "source-a",
    relative_path: str | None = None,
) -> tuple[InputRecordParser, ParsedInputFileResult]:
    stage_dir = tmp_path / "staging"
    resolved_relative_path = relative_path or f"inputs/files/0001_sample{format_hint}"
    _write_text(stage_dir, relative_path=resolved_relative_path, payload=payload)
    parser = InputRecordParser()
    result = parser.parse_materialized_file(
        stage_staging_directory=stage_dir,
        materialized_file=_materialized_file(
            relative_path=resolved_relative_path,
            format_hint=format_hint,
            source_reference=source_reference,
        ),
        alignment_mode=alignment_mode,
    )
    return parser, result


def test_fasta_single_record_preserves_provenance_and_description(tmp_path: Path) -> None:
    parser, result = _parse_text_file(
        tmp_path,
        payload=">rec-1 demo description\nacgt\n",
        format_hint=".fasta",
    )

    assert len(result.records) == 1
    assert len(result.issues) == 0
    record = result.records[0]
    assert record.input_manifest_source_reference == "source-a"
    assert record.input_manifest_relative_path == "inputs/input_manifest.json"
    assert record.materialized_relative_path == "inputs/files/0001_sample.fasta"
    assert record.format_hint == ".fasta"
    assert record.record_index == 0
    assert record.record_id == "rec-1"
    assert record.description == "demo description"
    assert record.raw_sequence == "ACGT"

    iterated = parser.iter_records(
        stage_staging_directory=tmp_path / "staging",
        materialized_file=_materialized_file(
            relative_path="inputs/files/0001_sample.fasta",
            format_hint=".fasta",
            source_reference="source-a",
        ),
        alignment_mode=AnalysisAlignmentMode.COMPUTE,
    )
    assert tuple(iterated) == result.records


def test_fasta_multi_record_indexes_and_whitespace_normalization(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">a first\nac gt\n>b\nT\tG\nC A\n",
        format_hint=".fasta",
    )

    assert [item.record_index for item in result.records] == [0, 1]
    assert [item.record_id for item in result.records] == ["a", "b"]
    assert [item.description for item in result.records] == ["first", None]
    assert [item.raw_sequence for item in result.records] == ["ACGT", "TGCA"]
    assert len(result.issues) == 0


def test_fasta_without_header_reports_malformed_issue(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload="ACGT\n",
        format_hint=".fasta",
    )

    assert len(result.records) == 0
    assert [issue.code for issue in result.issues] == [PARSER_ISSUE_FASTA_MALFORMED]


def test_fasta_empty_header_reports_missing_id(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">\nACGT\n",
        format_hint=".fasta",
    )

    assert len(result.records) == 1
    assert result.records[0].record_id is None
    assert [issue.code for issue in result.issues] == [PARSER_ISSUE_RECORD_ID_MISSING]
    assert result.issues[0].record_index == 0


def test_fasta_empty_sequence_reports_structural_issue(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">rec\n \n\t\n",
        format_hint=".fasta",
    )

    assert len(result.records) == 0
    assert [issue.code for issue in result.issues] == [PARSER_ISSUE_RECORD_SEQUENCE_EMPTY]


def test_fasta_duplicate_id_in_single_file_reports_issue(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">dup\nACGT\n>dup\nTGCA\n",
        format_hint=".fasta",
    )

    assert len(result.records) == 2
    assert [issue.code for issue in result.issues] == [PARSER_ISSUE_RECORD_DUPLICATE_ID]
    assert result.issues[0].record_index == 1


def test_same_fasta_id_in_different_files_is_allowed(tmp_path: Path) -> None:
    _parser_a, result_a = _parse_text_file(
        tmp_path,
        payload=">dup\nACGT\n",
        format_hint=".fasta",
        relative_path="inputs/files/0001_a.fasta",
    )
    _parser_b, result_b = _parse_text_file(
        tmp_path,
        payload=">dup\nTGCA\n",
        format_hint=".fasta",
        relative_path="inputs/files/0002_b.fasta",
    )

    assert len(result_a.issues) == 0
    assert len(result_b.issues) == 0
    assert result_a.records[0].record_id == "dup"
    assert result_b.records[0].record_id == "dup"


@pytest.mark.parametrize("format_hint", [".afa", ".mfa"])
def test_compute_mode_rejects_alignment_container_extensions(
    tmp_path: Path, format_hint: str
) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">a\nACGT\n",
        format_hint=format_hint,
        alignment_mode=AnalysisAlignmentMode.COMPUTE,
    )

    assert len(result.records) == 0
    assert [issue.code for issue in result.issues] == [
        PARSER_ISSUE_FORMAT_NOT_ALLOWED_FOR_ALIGNMENT_MODE
    ]


@pytest.mark.parametrize("sequence", ["AC-GT", "AC.GT"])
def test_compute_mode_reports_gap_symbols(sequence: str, tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=f">a\n{sequence}\n",
        format_hint=".fasta",
        alignment_mode=AnalysisAlignmentMode.COMPUTE,
    )

    assert len(result.records) == 1
    assert [issue.code for issue in result.issues] == [
        PARSER_ISSUE_GAP_NOT_ALLOWED_FOR_ALIGNMENT_MODE
    ]


@pytest.mark.parametrize("format_hint", [".afa", ".mfa", ".fasta"])
def test_prealigned_mode_accepts_fasta_variants(format_hint: str, tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">a\nAC.G-T\n",
        format_hint=format_hint,
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )

    assert len(result.records) == 1
    assert len(result.issues) == 0
    assert result.records[0].raw_sequence == "AC-G-T"
    assert "N" not in result.records[0].raw_sequence
    assert result.records[0].metadata == {"normalization": {"dot_to_gap_replacements": 1}}


@pytest.mark.parametrize("format_hint", [".afa", ".mfa"])
def test_none_mode_rejects_alignment_container_extensions(tmp_path: Path, format_hint: str) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">a\nACGT\n",
        format_hint=format_hint,
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert len(result.records) == 0
    assert [issue.code for issue in result.issues] == [
        PARSER_ISSUE_FORMAT_NOT_ALLOWED_FOR_ALIGNMENT_MODE
    ]


def test_none_mode_allows_gaps_and_normalizes_dot_to_dash(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">a\nAC.G-T\n",
        format_hint=".fasta",
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert len(result.records) == 1
    assert len(result.issues) == 0
    assert result.records[0].raw_sequence == "AC-G-T"
    assert "N" not in result.records[0].raw_sequence


def test_parser_preserves_u_and_does_not_apply_u_to_t(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload="auug",
        format_hint=".txt",
    )
    _parser_control, control_result = _parse_text_file(
        tmp_path,
        payload="attg",
        format_hint=".txt",
        relative_path="inputs/files/0002_control.txt",
    )

    assert result.records[0].raw_sequence == "AUUG"
    assert control_result.records[0].raw_sequence == "ATTG"
    assert result.records[0].raw_sequence != control_result.records[0].raw_sequence


def test_parser_preserves_potentially_invalid_symbols_for_validator(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload="acxgt",
        format_hint=".txt",
    )

    assert result.records[0].raw_sequence == "ACXGT"
    assert len(result.issues) == 0


def test_txt_file_is_single_record_without_artificial_id(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=" ac\nt\tg \n",
        format_hint=".txt",
    )

    assert len(result.records) == 1
    assert result.records[0].record_index == 0
    assert result.records[0].record_id is None
    assert result.records[0].description is None
    assert result.records[0].raw_sequence == "ACTG"


def test_txt_payload_with_fasta_like_header_is_not_reinterpreted_as_fasta(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=">demo\nACGT\n",
        format_hint=".txt",
    )

    assert len(result.records) == 1
    assert result.records[0].record_id is None
    assert result.records[0].raw_sequence == ">DEMOACGT"


@pytest.mark.parametrize("payload", ["", " \n\t "])
def test_txt_empty_after_whitespace_removal_is_structural_error(
    payload: str, tmp_path: Path
) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload=payload,
        format_hint=".txt",
    )

    assert len(result.records) == 0
    assert [issue.code for issue in result.issues] == [PARSER_ISSUE_RECORD_SEQUENCE_EMPTY]


def test_genbank_single_and_multi_record_parsing(tmp_path: Path) -> None:
    stage_dir = tmp_path / "staging"
    relative_path = "inputs/files/0001_records.gbk"
    records = [
        SeqRecord(
            Seq("ACGU"),
            id="NC_000001.1",
            description="record one",
            annotations={"molecule_type": "RNA", "organism": "synthetic one"},
        ),
        SeqRecord(
            Seq("TGCA"),
            id="NC_000002.1",
            description="record two",
            annotations={"molecule_type": "DNA", "organism": "synthetic two"},
        ),
    ]
    _write_genbank(stage_dir, relative_path=relative_path, records=records)
    parser = InputRecordParser()
    result = parser.parse_materialized_file(
        stage_staging_directory=stage_dir,
        materialized_file=_materialized_file(relative_path=relative_path, format_hint=".gbk"),
        alignment_mode=AnalysisAlignmentMode.PREALIGNED,
    )

    assert len(result.issues) == 0
    assert [item.record_index for item in result.records] == [0, 1]
    assert [item.record_id for item in result.records] == ["NC_000001.1", "NC_000002.1"]
    assert [item.description for item in result.records] == ["record one", "record two"]
    assert result.records[0].metadata != result.records[1].metadata
    assert json.dumps(result.records[0].metadata)
    assert json.dumps(result.records[1].metadata)


def test_genbank_record_without_sequence_reports_issue(tmp_path: Path) -> None:
    stage_dir = tmp_path / "staging"
    relative_path = "inputs/files/0001_missing_seq.gbk"
    record = SeqRecord(
        Seq(""),
        id="NC_000010.1",
        description="missing sequence",
        annotations={"molecule_type": "DNA"},
    )
    _write_genbank(stage_dir, relative_path=relative_path, records=[record])
    parser = InputRecordParser()
    result = parser.parse_materialized_file(
        stage_staging_directory=stage_dir,
        materialized_file=_materialized_file(relative_path=relative_path, format_hint=".gbk"),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert len(result.records) == 0
    assert [issue.code for issue in result.issues] == [PARSER_ISSUE_SEQUENCE_ABSENT]


def test_genbank_malformed_file_reports_file_issue(tmp_path: Path) -> None:
    _parser, result = _parse_text_file(
        tmp_path,
        payload="LOCUS bad record\nthis is not genbank\n",
        format_hint=".gbk",
    )

    assert len(result.records) == 0
    assert [issue.code for issue in result.issues] == [PARSER_ISSUE_GENBANK_MALFORMED]


def test_genbank_parser_does_not_issue_network_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    def _forbidden_efetch(*args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal called
        called = True
        raise AssertionError("network call is forbidden in parser")

    monkeypatch.setattr(Entrez, "efetch", _forbidden_efetch)

    stage_dir = tmp_path / "staging"
    relative_path = "inputs/files/0001_local.gbk"
    _write_genbank(
        stage_dir,
        relative_path=relative_path,
        records=[
            SeqRecord(
                Seq("ACGT"),
                id="NC_000020.1",
                description="local only",
                annotations={"molecule_type": "DNA"},
            )
        ],
    )
    parser = InputRecordParser()
    result = parser.parse_materialized_file(
        stage_staging_directory=stage_dir,
        materialized_file=_materialized_file(relative_path=relative_path, format_hint=".gbk"),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert called is False
    assert len(result.records) == 1
    assert len(result.issues) == 0


def test_parser_opens_txt_materialized_file_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stage_dir = tmp_path / "staging"
    relative_path = "inputs/files/0001_single_open.txt"
    _write_text(stage_dir, relative_path=relative_path, payload="acgt")
    target_path = stage_dir / relative_path
    original_open = Path.open
    open_calls = 0

    def _counted_open(
        self: Path,
        mode: Literal["r"] = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> TextIO:
        nonlocal open_calls
        if self == target_path:
            open_calls += 1
        return original_open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "open", _counted_open)

    parser = InputRecordParser()
    result = parser.parse_materialized_file(
        stage_staging_directory=stage_dir,
        materialized_file=_materialized_file(relative_path=relative_path, format_hint=".txt"),
        alignment_mode=AnalysisAlignmentMode.NONE,
    )

    assert len(result.records) == 1
    assert len(result.issues) == 0
    assert open_calls == 1
