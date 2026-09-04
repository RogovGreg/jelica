from jelica_contracts import load_notification_catalog


def test_notification_catalog_is_single_machine_readable_source() -> None:
    catalog = load_notification_catalog()
    assert catalog.schema_version == 1
    assert catalog.event("task.completed").default_enabled is True
    assert catalog.event("project_discussion.comment.mentioned").supersedes == (
        "project_discussion.comment.created",
    )
    assert catalog.event("project.frozen").default_enabled is False
    assert catalog.event("project.unfrozen").default_enabled is False
    assert catalog.event("task.scheduler_resumed") is None
