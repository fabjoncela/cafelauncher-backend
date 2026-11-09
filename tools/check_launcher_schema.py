#!/usr/bin/env python3
"""Check that the backend viewer/API schema covers fields used by CafeLauncher."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from validation import (  # noqa: E402
    GRAPHICS_RULES,
    PROFILE_CFG_FILES,
    PROFILE_FIELDS,
    PROFILE_NUMERIC_RULES,
    VIEWER_FIELD_METADATA,
    VIEWER_GROUP_ORDER,
)


def fail(message: str) -> None:
    raise SystemExit(f"Profile schema parity check failed: {message}")


def launcher_schema(launcher_root: Path) -> tuple[dict[str, str], set[str], set[str]]:
    source = (launcher_root / "cafe.bat").read_text(encoding="utf-8-sig")
    profile_match = re.search(
        r"(?ms)^\$script:GamingProfiles\s*=\s*\[ordered\]@\{(.*?)^\}", source
    )
    if not profile_match:
        fail("could not locate $script:GamingProfiles in cafe.bat")
    profile_body = profile_match.group(1)
    profile_starts = list(re.finditer(r"(?m)^[ \t]{4}([A-Za-z][A-Za-z0-9_]*)\s*=\s*@\{", profile_body))
    if not profile_starts:
        fail("no profile definitions were found in cafe.bat")

    profiles: dict[str, str] = {}
    profile_fields: set[str] = set()
    for index, match in enumerate(profile_starts):
        end = profile_starts[index + 1].start() if index + 1 < len(profile_starts) else len(profile_body)
        block = profile_body[match.end():end]
        cfg_match = re.search(r"(?:^|[;\s])ConfigFile\s*=\s*'([^']+)'", block)
        if not cfg_match:
            fail(f"profile {match.group(1)} has no literal ConfigFile")
        profiles[match.group(1)] = cfg_match.group(1)
        profile_fields.update(re.findall(r"(?:^|[;\s])([A-Za-z][A-Za-z0-9_]*)\s*=", block))

    profile_fields.discard("ConfigFile")
    profile_fields.update(("SchemaVersion", "ProfileName"))

    graphics_start = source.find("function ConvertTo-CafeGraphicsPreset")
    graphics_end = source.find("function Get-CafeActiveSteamVideoFile", graphics_start)
    if graphics_start < 0 or graphics_end < 0:
        fail("could not locate ConvertTo-CafeGraphicsPreset in cafe.bat")
    graphics_function = source[graphics_start:graphics_end]
    rules_start = graphics_function.find("$rules=[ordered]@{")
    rules_end = graphics_function.find("\n    }", rules_start)
    if rules_start < 0 or rules_end < 0:
        fail("could not locate the launcher graphics validation rules")
    rules_body = graphics_function[rules_start + len("$rules=[ordered]@{"):rules_end]
    graphics_fields = set(re.findall(r"(?:^|[; \t])([A-Za-z][A-Za-z0-9_]*)\s*=\s*@\{", rules_body))
    if not graphics_fields:
        fail("no launcher graphics fields were found")
    return profiles, profile_fields, graphics_fields


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher-root", type=Path, required=True)
    args = parser.parse_args()
    launcher_root = args.launcher_root.resolve()
    profiles, profile_fields, graphics_fields = launcher_schema(launcher_root)

    launcher_profiles = set(profiles)
    backend_profiles = set(PROFILE_CFG_FILES)
    if launcher_profiles - backend_profiles:
        fail(f"backend is missing launcher profiles: {sorted(launcher_profiles - backend_profiles)}")
    cfg_mismatches = {
        name: (cfg, PROFILE_CFG_FILES[name])
        for name, cfg in profiles.items()
        if PROFILE_CFG_FILES.get(name) != cfg
    }
    if cfg_mismatches:
        fail(f"profile CFG mappings differ (launcher, backend): {cfg_mismatches}")

    missing_profile_fields = profile_fields - PROFILE_FIELDS
    if missing_profile_fields:
        fail(f"backend validation is missing launcher profile fields: {sorted(missing_profile_fields)}")
    missing_graphics_fields = graphics_fields - set(GRAPHICS_RULES)
    if missing_graphics_fields:
        fail(f"backend validation is missing launcher graphics fields: {sorted(missing_graphics_fields)}")

    all_backend_fields = PROFILE_FIELDS | set(GRAPHICS_RULES) | {"DisplayPolicy", "RefreshPolicy"}
    missing_metadata = all_backend_fields - set(VIEWER_FIELD_METADATA)
    if missing_metadata:
        fail(f"viewer metadata is missing field labels/groups: {sorted(missing_metadata)}")
    unknown_groups = {
        metadata.get("group", "Other")
        for metadata in VIEWER_FIELD_METADATA.values()
    } - set(VIEWER_GROUP_ORDER)
    if unknown_groups:
        fail(f"viewer metadata uses groups missing from VIEWER_GROUP_ORDER: {sorted(unknown_groups)}")

    for field, rule in GRAPHICS_RULES.items():
        if rule[0] != "enum":
            continue
        allowed = rule[1]
        labels = VIEWER_FIELD_METADATA[field].get("values", {})
        missing_values = {str(value) for value in allowed} - set(labels)
        if missing_values:
            fail(f"viewer enum labels are missing {field} values: {sorted(missing_values)}")

    scaling_labels = VIEWER_FIELD_METADATA["ScalingMode"].get("values", {})
    missing_scaling_values = {str(value) for value in PROFILE_NUMERIC_RULES["ScalingMode"]} - set(scaling_labels)
    if missing_scaling_values:
        fail(f"viewer enum labels are missing profile ScalingMode values: {sorted(missing_scaling_values)}")

    print(
        "Profile schema parity passed: "
        f"{len(launcher_profiles)} launcher profiles, "
        f"{len(profile_fields)} launcher profile fields, "
        f"{len(graphics_fields)} launcher graphics fields."
    )


if __name__ == "__main__":
    main()
