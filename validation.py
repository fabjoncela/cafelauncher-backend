"""Validation and normalization rules mirrored from CafeLauncher current source."""

from __future__ import annotations

import math
import re
from urllib.parse import urlsplit


PROFILE_CFG_FILES = {
    "FabMode": "fab.cfg",
    "ArliMode": "arli.cfg",
    "BrileMode": "brile.cfg",
    "ArmlMode": "arml.cfg",
    "OliMode": "oli.cfg",
    "Provmode": "prov.cfg",
}

PROFILE_DEFAULTS = {
    "FabMode": dict(PointerSpeed=10, LogitechDPI=800, DisableAcceleration=True, ApplyNvidia=True, Width=1920, Height=1080, RefreshHz=360, Vibrance=75, Hue=-1, ScalingMode=2, VoicePushToTalkKey="", LaunchApps=True, LaunchWebsiteEnabled=True, LaunchWebsiteUrl="https://metastack.gg/"),
    "ArliMode": dict(PointerSpeed=10, LogitechDPI=800, DisableAcceleration=True, ApplyNvidia=True, Width=1920, Height=1080, RefreshHz=360, Vibrance=75, Hue=-1, ScalingMode=2, VoicePushToTalkKey="", LaunchApps=True, LaunchWebsiteEnabled=True, LaunchWebsiteUrl="https://metastack.gg/"),
    "BrileMode": dict(PointerSpeed=10, LogitechDPI=800, DisableAcceleration=True, ApplyNvidia=True, Width=1920, Height=1080, RefreshHz=360, Vibrance=75, Hue=-1, ScalingMode=2, VoicePushToTalkKey="", LaunchApps=True, LaunchWebsiteEnabled=True, LaunchWebsiteUrl="https://metastack.gg/"),
    "ArmlMode": dict(PointerSpeed=10, LogitechDPI=800, DisableAcceleration=True, ApplyNvidia=True, Width=1920, Height=1080, RefreshHz=360, Vibrance=75, Hue=-1, ScalingMode=2, VoicePushToTalkKey="", LaunchApps=True, LaunchWebsiteEnabled=True, LaunchWebsiteUrl="https://metastack.gg/"),
    "OliMode": dict(PointerSpeed=10, LogitechDPI=800, DisableAcceleration=True, ApplyNvidia=True, Width=1920, Height=1080, RefreshHz=360, Vibrance=75, Hue=-1, ScalingMode=2, VoicePushToTalkKey="", LaunchApps=True, LaunchWebsiteEnabled=True, LaunchWebsiteUrl="https://metastack.gg/"),
    "Provmode": dict(PointerSpeed=8, LogitechDPI=350, DisableAcceleration=True, ApplyNvidia=True, Width=1920, Height=1080, RefreshHz=300, Vibrance=75, Hue=-1, ScalingMode=2, VoicePushToTalkKey="", LaunchApps=True, LaunchWebsiteEnabled=True, LaunchWebsiteUrl="https://metastack.gg/"),
}

PROFILE_NUMERIC_RULES = {
    "PointerSpeed": (1, 20),
    "LogitechDPI": (100, 25600),
    "Width": (640, 7680),
    "Height": (480, 4320),
    "RefreshHz": (30, 600),
    "Vibrance": (0, 100),
    "Hue": (-1, 359),
    "ScalingMode": (2, 3, 5),
}
PROFILE_BOOL_FIELDS = ("DisableAcceleration", "ApplyNvidia", "LaunchApps", "LaunchWebsiteEnabled")
PROFILE_FIELDS = {
    "SchemaVersion", "ProfileName", "PointerSpeed", "LogitechDPI", "DisableAcceleration", "ApplyNvidia",
    "Width", "Height", "RefreshHz", "Vibrance", "Hue", "ScalingMode", "VoicePushToTalkKey",
    "CfgDownloadUrl", "LaunchApps", "LaunchWebsiteEnabled", "LaunchWebsiteUrl",
}
VOICE_KEY = re.compile(r"(?:[A-Z]|[0-9]|F(?:[1-9]|1[0-2])|MOUSE[1-5]|CAPSLOCK|ALT|SHIFT|CTRL|SPACE|TAB)\Z")
CFG_URL = re.compile(r"/v1/cfg/([a-z0-9_-]+\.cfg)\Z")
LEGACY_CFG_URL = re.compile(
    r"https://raw\.githubusercontent\.com/fabjoncela/Cs-script/main/([a-z0-9_-]+\.cfg)\Z",
    re.IGNORECASE,
)

GRAPHICS_RULES = {
    "Width": ("range", 640, 7680),
    "Height": ("range", 480, 4320),
    "RefreshHz": ("range", 24, 600),
    "AspectRatioMode": ("enum", (0, 1, 2)),
    "Fullscreen": ("enum", (0, 1)),
    "MSAASamples": ("enum", (0, 2, 4, 8, 16)),
    "ShadowQuality": ("enum", (0, 1, 2, 3)),
    "DynamicShadows": ("enum", (0, 1)),
    "TextureDetail": ("enum", (0, 1, 2, 3)),
    "ShaderQuality": ("enum", (0, 1, 2)),
    "ParticleDetail": ("enum", (0, 1, 2, 3)),
    "AmbientOcclusion": ("enum", (0, 1, 2, 3)),
    "HDRDetail": ("enum", (-1, 0, 1, 2, 3)),
    "NvidiaReflex": ("enum", (0, 1, 2)),
    "TextureFiltering": ("enum", (0, 1, 2, 3, 4, 5)),
    "FSRDetail": ("enum", (0, 1, 2, 3, 4)),
    "VSync": ("enum", (0, 1)),
    "CMAA": ("enum", (0, 1)),
}
GRAPHICS_REQUIRED_SETTINGS = {
    "AspectRatioMode", "Fullscreen", "MSAASamples", "ShadowQuality", "DynamicShadows", "TextureDetail",
    "ShaderQuality", "ParticleDetail", "AmbientOcclusion", "HDRDetail", "NvidiaReflex", "TextureFiltering",
    "FSRDetail", "VSync", "CMAA",
}
SENSITIVITY_LINE = re.compile(r"^[ \t]*sensitivity\b[^\r\n]*", re.IGNORECASE)
SENSITIVITY_VALUE = re.compile(
    r'^[ \t]*sensitivity[ \t]+(?:"(?P<quoted>[^"\r\n]+)"|(?P<plain>[^ \t\r\n]+))[ \t]*(?://[^\r\n]*)?$',
    re.IGNORECASE,
)
SENSITIVITY_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def _is_int(value: object) -> bool:
    return type(value) is int


def _check_integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if not _is_int(value) or value < minimum or value > maximum:
        raise ValueError(f"{field} must be an integer from {minimum} through {maximum}.")
    return value


def _canonical_cfg_url(profile_name: str, value: object, *, allow_legacy: bool) -> str:
    expected = PROFILE_CFG_FILES[profile_name]
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("CfgDownloadUrl must identify this profile's CFG endpoint.")
    route_match = CFG_URL.fullmatch(value)
    if route_match:
        filename = route_match.group(1)
    else:
        legacy_match = LEGACY_CFG_URL.fullmatch(value) if allow_legacy else None
        if not legacy_match:
            raise ValueError("CfgDownloadUrl must use the backend /v1/cfg/{name} route.")
        filename = legacy_match.group(1)
    if filename != expected:
        raise ValueError("CfgDownloadUrl does not match this profile's CFG file.")
    return f"/v1/cfg/{expected}"


def _validate_website_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("LaunchWebsiteUrl must be a web URL no longer than 2048 characters.")
    try:
        value.encode("utf-8")
        parsed = urlsplit(value)
        _ = parsed.port
    except (UnicodeEncodeError, ValueError) as error:
        raise ValueError("LaunchWebsiteUrl is invalid.") from error
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("LaunchWebsiteUrl must be an absolute HTTP or HTTPS URL without credentials.")
    return value


def normalize_profile(profile_name: str, value: object, *, allow_legacy_cfg_url: bool = True) -> dict:
    if profile_name not in PROFILE_CFG_FILES or not isinstance(value, dict):
        raise ValueError("Unknown profile or profile payload is not an object.")
    if type(value.get("SchemaVersion")) is not int or value["SchemaVersion"] != 1:
        raise ValueError("Profile SchemaVersion must be 1.")
    if value.get("ProfileName") != profile_name:
        raise ValueError("ProfileName does not match the requested profile.")
    unknown = set(value) - PROFILE_FIELDS
    if unknown:
        raise ValueError("Profile contains unsupported fields.")

    result = {"SchemaVersion": 1, "ProfileName": profile_name, **PROFILE_DEFAULTS[profile_name]}
    result.update(value)
    for field, rule in PROFILE_NUMERIC_RULES.items():
        if field == "ScalingMode":
            if type(result[field]) is not int or result[field] not in rule:
                raise ValueError("ScalingMode must be 2, 3, or 5.")
        else:
            _check_integer(result[field], field, rule[0], rule[1])
    for field in PROFILE_BOOL_FIELDS:
        if type(result[field]) is not bool:
            raise ValueError(f"{field} must be a boolean.")
    voice_key = result["VoicePushToTalkKey"]
    if not isinstance(voice_key, str) or (voice_key and not VOICE_KEY.fullmatch(voice_key)):
        raise ValueError("VoicePushToTalkKey is not a supported key name.")
    result["CfgDownloadUrl"] = _canonical_cfg_url(profile_name, result["CfgDownloadUrl"], allow_legacy=allow_legacy_cfg_url)
    result["LaunchWebsiteUrl"] = _validate_website_url(result["LaunchWebsiteUrl"])
    return result


def normalize_graphics(profile_name: str, value: object, profile: dict) -> dict:
    if profile_name not in PROFILE_CFG_FILES or not isinstance(value, dict):
        raise ValueError("Unknown profile or graphics payload is not an object.")
    unknown = set(value) - {"SchemaVersion", "ProfileName", "DisplayPolicy", "RefreshPolicy", "ScalingMode", "Settings"}
    if unknown:
        raise ValueError("Graphics payload contains unsupported fields.")
    if type(value.get("SchemaVersion")) is not int or value["SchemaVersion"] != 1:
        raise ValueError("Graphics SchemaVersion must be 1.")
    if value.get("ProfileName") != profile_name:
        raise ValueError("Graphics ProfileName does not match the requested profile.")
    settings_value = value.get("Settings")
    if not isinstance(settings_value, dict):
        raise ValueError("Graphics Settings must be an object.")

    display_policy = value.get("DisplayPolicy", "PreserveCurrent" if profile_name == "BrileMode" else "ApplyPreset")
    if display_policy not in ("ApplyPreset", "PreserveCurrent"):
        raise ValueError("DisplayPolicy must be ApplyPreset or PreserveCurrent.")
    refresh_policy = value.get("RefreshPolicy", "Highest")
    if refresh_policy not in ("Highest", "Exact"):
        raise ValueError("RefreshPolicy must be Highest or Exact.")
    scaling_mode = value.get("ScalingMode")
    if "ScalingMode" not in value:
        scaling_mode = profile["ScalingMode"]
    if type(scaling_mode) is not int or scaling_mode not in (2, 3, 5):
        raise ValueError("Graphics ScalingMode must be 2, 3, or 5.")

    settings = {}
    unknown_settings = set(settings_value) - GRAPHICS_RULES.keys()
    if unknown_settings:
        raise ValueError("Graphics Settings contains unsupported values.")
    for field, rule in GRAPHICS_RULES.items():
        if field not in settings_value:
            continue
        raw = settings_value[field]
        if not _is_int(raw):
            raise ValueError(f"Graphics {field} must be an integer.")
        if rule[0] == "range":
            if raw < rule[1] or raw > rule[2]:
                raise ValueError(f"Graphics {field} is outside its supported range.")
        elif raw not in rule[1]:
            raise ValueError(f"Graphics {field} has an unsupported value.")
        settings[field] = raw

    width_present = "Width" in settings
    height_present = "Height" in settings
    if width_present != height_present:
        raise ValueError("Graphics Width and Height must both be set or both be omitted.")
    if display_policy == "PreserveCurrent":
        prohibited = {"Width", "Height", "RefreshHz", "AspectRatioMode", "Fullscreen"} & settings.keys()
        if prohibited:
            raise ValueError("PreserveCurrent display policy cannot set: " + ", ".join(sorted(prohibited)) + ".")
    elif not width_present:
        settings["Width"], settings["Height"] = (1920, 1080) if profile_name == "BrileMode" else (1280, 960)

    missing = GRAPHICS_REQUIRED_SETTINGS - settings.keys()
    if missing:
        raise ValueError("Graphics Settings is missing required values: " + ", ".join(sorted(missing)) + ".")
    if refresh_policy == "Exact" and "RefreshHz" not in settings:
        raise ValueError("Exact RefreshPolicy requires RefreshHz.")

    return {
        "SchemaVersion": 1,
        "ProfileName": profile_name,
        "DisplayPolicy": display_policy,
        "RefreshPolicy": refresh_policy,
        "ScalingMode": scaling_mode,
        "Settings": settings,
    }


def validate_cfg(value: object, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("CFG must be UTF-8 text without NUL bytes.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("CFG must contain valid Unicode text.") from error
    if len(encoded) > max_bytes:
        raise ValueError("CFG exceeds the 2 MiB size limit.")
    commands = [line for line in value.splitlines() if SENSITIVITY_LINE.match(line)]
    if len(commands) != 1:
        raise ValueError(f"CFG must contain exactly one active sensitivity command; found {len(commands)}.")
    match = SENSITIVITY_VALUE.fullmatch(commands[0])
    if not match:
        raise ValueError("CFG sensitivity command has an invalid format.")
    raw = match.group("quoted") if match.group("quoted") is not None else match.group("plain")
    if not SENSITIVITY_NUMBER.fullmatch(raw):
        raise ValueError("CS2 sensitivity must be a decimal number greater than 0 and at most 20.")
    if len(raw) > 32:
        raise ValueError("CS2 sensitivity number is too long.")
    number = float(raw)
    if not math.isfinite(number) or number <= 0 or number > 20:
        raise ValueError("CS2 sensitivity must be a decimal number greater than 0 and at most 20.")
    return value
