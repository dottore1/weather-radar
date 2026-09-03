"""Structural checks for manifest.json — the fields Home Assistant
requires from a config_flow-enabled custom integration."""
from __future__ import annotations

import json
from pathlib import Path

MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "custom_components" / "weather_radar_dmi" / "manifest.json"
)

VALID_IOT_CLASSES = {
    "assumed_state", "cloud_polling", "cloud_push",
    "local_polling", "local_push", "calculated",
}


def test_manifest_is_valid_json():
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert isinstance(manifest, dict)


def test_manifest_has_required_fields():
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["domain"] == "weather_radar_dmi"
    assert manifest["config_flow"] is True
    assert isinstance(manifest["name"], str) and manifest["name"]
    assert isinstance(manifest["codeowners"], list) and manifest["codeowners"]
    assert all(c.startswith("@") for c in manifest["codeowners"])
    assert manifest["iot_class"] in VALID_IOT_CLASSES
    assert isinstance(manifest["version"], str) and manifest["version"]
    assert isinstance(manifest["documentation"], str) and manifest["documentation"].startswith("http")


def test_manifest_requirements_are_pinned_with_a_minimum_version():
    manifest = json.loads(MANIFEST_PATH.read_text())
    requirements = manifest["requirements"]
    assert isinstance(requirements, list) and requirements
    for req in requirements:
        assert isinstance(req, str) and req
        assert ">=" in req, f"{req!r} should pin a minimum version"
