from __future__ import annotations

from pathlib import Path

import pytest

from jelica_core.sample_metadata import (
    SampleMetadataCandidate,
    SampleMetadataError,
    SequenceMetadata,
    load_sample_metadata_csv,
    merge_sequence_metadata,
    resolve_sample_metadata_overrides,
)


def _candidate(
    candidate_id: str,
    *,
    record_id: str | None,
    source_path: str | None,
    automatic: SequenceMetadata | None = None,
) -> SampleMetadataCandidate:
    return SampleMetadataCandidate(
        candidate_id=candidate_id,
        record_id=record_id,
        source_path=source_path,
        automatic_metadata=automatic or SequenceMetadata(),
    )


def _write_csv(path: Path, payload: str, *, bom: bool = False) -> None:
    path.write_text(payload, encoding="utf-8-sig" if bom else "utf-8")


def test_csv_normalizes_bom_unicode_quoted_values_and_source_against_csv_parent(
    tmp_path: Path,
) -> None:
    metadata_csv = tmp_path / "dataset" / "metadata.csv"
    metadata_csv.parent.mkdir()
    _write_csv(
        metadata_csv,
        (
            "geo_loc_name,source,record_id,note\n"
            '"Serbia, Belgrade",subdir/../sample.fasta,ABC123.1,  Živa  \n'
        ),
        bom=True,
    )

    overrides = load_sample_metadata_csv(
        metadata_csv=str(metadata_csv),
        submission_base_dir=tmp_path / "different-cwd",
    )

    assert len(overrides) == 1
    override = overrides[0]
    assert override.selector.record_id == "ABC123.1"
    assert override.selector.source_path == str(metadata_csv.parent / "sample.fasta")
    assert override.metadata.geo_loc_name == "Serbia, Belgrade"
    assert override.metadata.note == "Živa"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("host\nHomo sapiens\n", "metadata_csv_missing_identifier"),
        ("record_id,unexpected\nABC,one\n", "metadata_csv_unknown_column"),
        ("record_id,host\n,Human\n", "metadata_csv_missing_identifier"),
        ("record_id,host\nABC,   \n", "metadata_csv_no_metadata_values"),
    ],
)
def test_csv_rejects_invalid_structure(
    tmp_path: Path,
    payload: str,
    code: str,
) -> None:
    metadata_csv = tmp_path / "metadata.csv"
    _write_csv(metadata_csv, payload)

    with pytest.raises(SampleMetadataError) as error_info:
        load_sample_metadata_csv(metadata_csv=str(metadata_csv), submission_base_dir=tmp_path)

    assert error_info.value.code == code


def test_csv_ignores_blank_rows_and_preserves_exact_case_sensitive_record_ids(
    tmp_path: Path,
) -> None:
    metadata_csv = tmp_path / "metadata.csv"
    _write_csv(metadata_csv, "record_id,host\n\nABC123.1,Homo sapiens\n")
    overrides = load_sample_metadata_csv(
        metadata_csv=str(metadata_csv), submission_base_dir=tmp_path
    )
    candidates = (
        _candidate("lower", record_id="abc123.1", source_path=None),
        _candidate("upper", record_id="ABC123.1", source_path=None),
    )

    resolved = resolve_sample_metadata_overrides(candidates=candidates, overrides=overrides)

    assert resolved["lower"].host is None
    assert resolved["upper"].host == "Homo sapiens"


def test_matching_supports_record_source_and_combined_selectors(tmp_path: Path) -> None:
    metadata_csv = tmp_path / "metadata.csv"
    _write_csv(
        metadata_csv,
        "record_id,source,host,geo_loc_name\n"
        "REC_A,,Human,\n"
        ",one.fasta,,Serbia\n"
        "REC_C,two.fasta,Human,France\n",
    )
    overrides = load_sample_metadata_csv(
        metadata_csv=str(metadata_csv), submission_base_dir=tmp_path
    )
    candidates = (
        _candidate("a", record_id="REC_A", source_path=str(tmp_path / "a.fasta")),
        _candidate("one", record_id="REC_B", source_path=str(tmp_path / "one.fasta")),
        _candidate("c", record_id="REC_C", source_path=str(tmp_path / "two.fasta")),
    )

    resolved = resolve_sample_metadata_overrides(candidates=candidates, overrides=overrides)

    assert resolved["a"].host == "Human"
    assert resolved["one"].geo_loc_name == "Serbia"
    assert resolved["c"].host == "Human"
    assert resolved["c"].geo_loc_name == "France"


@pytest.mark.parametrize(
    ("overrides_payload", "candidates", "code"),
    [
        (
            "record_id,host\nMISSING,Human\n",
            (_candidate("a", record_id="A", source_path=None),),
            "metadata_csv_target_not_found",
        ),
        (
            "record_id,host\nDUP,Human\n",
            (
                _candidate("a", record_id="DUP", source_path=None),
                _candidate("b", record_id="DUP", source_path=None),
            ),
            "metadata_csv_target_ambiguous",
        ),
        (
            "source,host\n/dataset/multi.fasta,Human\n",
            (
                _candidate("a", record_id="A", source_path="/dataset/multi.fasta"),
                _candidate("b", record_id="B", source_path="/dataset/multi.fasta"),
            ),
            "metadata_csv_target_ambiguous",
        ),
        (
            "record_id,host\nA,Human\nA,Dog\n",
            (_candidate("a", record_id="A", source_path=None),),
            "metadata_csv_duplicate_target",
        ),
    ],
)
def test_matching_rejects_not_found_ambiguous_and_duplicate_targets(
    tmp_path: Path,
    overrides_payload: str,
    candidates: tuple[SampleMetadataCandidate, ...],
    code: str,
) -> None:
    metadata_csv = tmp_path / "metadata.csv"
    _write_csv(metadata_csv, overrides_payload)
    overrides = load_sample_metadata_csv(
        metadata_csv=str(metadata_csv), submission_base_dir=tmp_path
    )

    with pytest.raises(SampleMetadataError) as error_info:
        resolve_sample_metadata_overrides(candidates=candidates, overrides=overrides)

    assert error_info.value.code == code


def test_merge_overrides_only_non_empty_values() -> None:
    merged = merge_sequence_metadata(
        automatic=SequenceMetadata(
            collection_date="2021",
            geo_loc_name="Serbia",
            host="Homo sapiens",
        ),
        override=SequenceMetadata(
            collection_date="2021-03-10",
            geo_loc_name="Serbia: Belgrade",
        ),
    )

    assert merged.collection_date == "2021-03-10"
    assert merged.geo_loc_name == "Serbia: Belgrade"
    assert merged.host == "Homo sapiens"
