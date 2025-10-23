import importlib
import os
import uuid


def test_default_headers_set_on_import(monkeypatch):
    # Ensure a clean import
    monkeypatch.delenv("PLEXAPI_HEADER_IDENTIFIER", raising=False)
    monkeypatch.delenv("PLEXAPI_HEADER_DEVICE_NAME", raising=False)
    if "plex_generate_previews" in list(importlib.sys.modules.keys()):
        importlib.reload(importlib.import_module("plex_generate_previews"))
    else:
        importlib.import_module("plex_generate_previews")

    expected_identifier = uuid.uuid3(uuid.NAMESPACE_DNS, "PlexGeneratePreviews").hex
    assert os.environ.get("PLEXAPI_HEADER_IDENTIFIER") == expected_identifier
    assert os.environ.get("PLEXAPI_HEADER_DEVICE_NAME") == "PlexGeneratePreviews"


def test_env_overrides_respected(monkeypatch):
    monkeypatch.setenv("PLEXAPI_HEADER_IDENTIFIER", "custom-id")
    monkeypatch.setenv("PLEXAPI_HEADER_DEVICE_NAME", "custom-name")

    # Re-import to apply setdefault logic
    if "plex_generate_previews" in list(importlib.sys.modules.keys()):
        importlib.reload(importlib.import_module("plex_generate_previews"))
    else:
        importlib.import_module("plex_generate_previews")

    assert os.environ.get("PLEXAPI_HEADER_IDENTIFIER") == "custom-id"
    assert os.environ.get("PLEXAPI_HEADER_DEVICE_NAME") == "custom-name"


