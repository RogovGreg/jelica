from __future__ import annotations

from pathlib import Path

import pytest

from jelica_cli.results_import import (
    ResultImportItem,
    ResultImportPlanError,
    build_result_import_groups,
    expand_result_import_items,
    parse_rename_option,
    render_result_import_name,
)
from jelica_core.result_package import content_digest_from_content_id


def _content_id(letter: str = "a") -> str:
    return f"sha256:{letter * 64}"


def test_parse_rename_option_requires_source_equals_template() -> None:
    with pytest.raises(ResultImportPlanError, match="SOURCE=TEMPLATE"):
        parse_rename_option("source-only")
    with pytest.raises(ResultImportPlanError, match="source must not be empty"):
        parse_rename_option("=Foo")
    with pytest.raises(ResultImportPlanError, match="template must not be empty"):
        parse_rename_option("source=   ")


def test_render_single_file_default_and_hash_templates() -> None:
    item = ResultImportItem(
        source=Path("source.jelica"),
        file_path=Path("My result.jelica"),
        template=None,
        is_directory=False,
        index=None,
    )
    content_id = _content_id()
    rendered = render_result_import_name(item=item, content_id=content_id)
    assert rendered.result_name == "My_result"
    assert rendered.filename_stem is None

    hashed = render_result_import_name(
        item=item.__class__(
            source=item.source,
            file_path=item.file_path,
            template="{hash}",
            is_directory=False,
            index=None,
        ),
        content_id=content_id,
    )
    assert hashed.result_name is None
    assert hashed.filename_stem == content_digest_from_content_id(content_id)


@pytest.mark.parametrize(
    ("template", "expected_result_name", "expected_filename_stem"),
    (
        ("Imported", "Imported_1", None),
        (
            "{index}_Imported_{name}",
            None,
            "1_Imported_" + "a" * 64 + "__" + "a" * 64,
        ),
    ),
)
def test_explicit_directory_rename_overrides_hash_only_source_name(
    template: str,
    expected_result_name: str | None,
    expected_filename_stem: str | None,
) -> None:
    item = ResultImportItem(
        source=Path("folder"),
        file_path=Path(("a" * 64) + ".jelica"),
        template=template,
        is_directory=True,
        index=1,
    )

    rendered = render_result_import_name(item=item, content_id=_content_id())

    assert rendered.result_name == expected_result_name
    assert rendered.filename_stem == expected_filename_stem


def test_hash_only_source_without_rename_remains_unnamed() -> None:
    item = ResultImportItem(
        source=Path("folder"),
        file_path=Path(("a" * 64) + ".jelica"),
        template=None,
        is_directory=True,
        index=1,
    )

    rendered = render_result_import_name(item=item, content_id=_content_id())

    assert rendered.result_name is None
    assert rendered.filename_stem is None


def test_implicit_current_directory_rename_uses_template_only() -> None:
    groups = build_result_import_groups(
        positional_sources=(),
        rename_values=("{index}_Imported",),
        implicit_current_directory=True,
    )

    assert groups[0].source == Path(".")
    assert groups[0].template == "{index}_Imported"
    assert groups[0].is_directory is True


def test_implicit_current_directory_rename_rejects_source_template_form() -> None:
    with pytest.raises(ResultImportPlanError, match="only the rename template"):
        build_result_import_groups(
            positional_sources=(),
            rename_values=(".={index}_Imported",),
            implicit_current_directory=True,
        )


def test_explicit_source_rename_rejects_template_only_form() -> None:
    with pytest.raises(ResultImportPlanError, match="SOURCE=TEMPLATE"):
        build_result_import_groups(
            positional_sources=(Path("."),),
            rename_values=("{index}_Imported",),
        )


def test_explicit_source_rename_binds_to_existing_positional_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "a.jelica"
    groups = build_result_import_groups(
        positional_sources=(source,),
        rename_values=(f"{source}=Imported",),
    )

    assert groups[0].source == source
    assert groups[0].template == "Imported"


def test_explicit_source_rename_must_match_positional_source(tmp_path: Path) -> None:
    with pytest.raises(ResultImportPlanError, match="not present"):
        build_result_import_groups(
            positional_sources=(tmp_path / "a.jelica",),
            rename_values=(f"{tmp_path / 'b.jelica'}=Imported",),
        )


def test_explicit_source_cannot_have_multiple_renames(tmp_path: Path) -> None:
    source = tmp_path / "a.jelica"
    with pytest.raises(ResultImportPlanError, match="more than once"):
        build_result_import_groups(
            positional_sources=(source,),
            rename_values=(f"{source}=First", f"{source}=Second"),
        )


def test_directory_indices_are_assigned_before_import_and_templates_are_rendered(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    for name in ("a.jelica", "b.jelica", "c.jelica"):
        (folder / name).write_bytes(b"placeholder")
    groups = build_result_import_groups(
        positional_sources=(folder,),
        rename_values=(),
    )
    assert groups[0].is_directory is True
    items = expand_result_import_items(groups=groups, no_name=False)
    first = items[0].__class__(
        source=items[0].source,
        file_path=items[0].file_path,
        template="{index}_Imported_{name}",
        is_directory=True,
        index=items[0].index,
    )
    third = first.__class__(
        source=items[2].source,
        file_path=items[2].file_path,
        template=first.template,
        is_directory=True,
        index=items[2].index,
    )
    assert render_result_import_name(
        item=first, content_id=_content_id()
    ).result_name == ("1_Imported_a")
    assert render_result_import_name(
        item=third, content_id=_content_id("b")
    ).result_name == ("3_Imported_c")


def test_directory_index_variables_are_rejected_for_single_file() -> None:
    item = ResultImportItem(
        source=Path("source.jelica"),
        file_path=Path("source.jelica"),
        template="{index}_{name}",
        is_directory=False,
        index=None,
    )
    with pytest.raises(ValueError, match="only available for directory"):
        render_result_import_name(item=item, content_id=_content_id())


def test_system_directory_index_does_not_consume_64_character_name_limit() -> None:
    item = ResultImportItem(
        source=Path("folder"),
        file_path=Path("a.jelica"),
        template="A" * 64,
        is_directory=True,
        index=1,
    )

    rendered = render_result_import_name(item=item, content_id=_content_id())

    assert rendered.result_name is None
    assert rendered.filename_stem == (
        f"{'A' * 64}_1__{content_digest_from_content_id(_content_id())}"
    )


def test_no_name_conflicts_with_rename_before_import() -> None:
    group = build_result_import_groups(
        positional_sources=(Path("folder"),),
        rename_values=("folder=Imported",),
    )
    with pytest.raises(ResultImportPlanError, match="mutually exclusive"):
        expand_result_import_items(groups=group, no_name=True)
