from pathlib import Path

import pytest

from jelica_cli.system_config import CliSystemConfigService
from jelica_core.system_config import CoreConfigUnknownParameterError


def test_local_notification_config_roundtrip_preserves_unrelated_values(tmp_path: Path) -> None:
    service = CliSystemConfigService(jelica_home=tmp_path / "home")
    service.core_service.initialize_system_config(data_directory="kept-data")
    service.core_service.set_parameter(
        parameter="notifications.device.events.task.completed", value="false"
    )
    service.core_service.set_parameter(
        parameter="desktop.notifications.in_app.enabled", value="false"
    )
    document = service.show_document()
    assert document["data"]["directory"] == "kept-data"
    assert document["notifications"]["device"]["events"]["task.completed"] is False
    assert document["desktop"]["notifications"]["in_app"]["enabled"] is False
    with pytest.raises(CoreConfigUnknownParameterError):
        service.core_service.set_parameter(
            parameter="desktop.notifications.in_app.events.project.deleted", value="true"
        )
