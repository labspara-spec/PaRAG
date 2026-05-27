from __future__ import annotations
import os
from pathlib import Path
from typing import Any, Mapping, TypedDict

import yaml

from madrag.prompts import load_prompts as _load_prompts

# All prompts loaded from madrag/prompts/*.xml (POML format).
PROMPTS: dict[str, Any] = _load_prompts()


class EntityExtractionPromptProfile(TypedDict):
    entity_types_guidance: str
    entity_extraction_examples: list[str]
    entity_extraction_json_examples: list[str]


def get_default_entity_extraction_prompt_profile() -> EntityExtractionPromptProfile:
    """Return a copy of the built-in entity extraction prompt profile."""

    return {
        "entity_types_guidance": PROMPTS["default_entity_types_guidance"].rstrip(),
        "entity_extraction_examples": [
            example.rstrip() for example in PROMPTS["entity_extraction_examples"]
        ],
        "entity_extraction_json_examples": [
            example.rstrip() for example in PROMPTS["entity_extraction_json_examples"]
        ],
    }


_ALLOWED_PROMPT_SUFFIXES = frozenset({".yml", ".yaml"})
_DEFAULT_PROMPT_DIR = "./prompts"
_ENTITY_TYPE_SUBDIR = "entity_type"


def get_entity_type_prompt_dir() -> Path:
    """Return the directory for entity type prompt profiles.

    Resolves ``PROMPT_DIR`` (defaults to ``./prompts`` relative to the current
    working directory, mirroring ``INPUT_DIR`` / ``WORKING_DIR``) and appends
    the hard-coded ``entity_type`` subdirectory. Profile files are provided by
    the user at runtime and are not shipped with the distribution. The
    file-name sandbox in :func:`resolve_entity_type_prompt_path` ensures
    user-supplied file names cannot escape the resolved directory.
    """

    configured = os.getenv("PROMPT_DIR", "").strip() or _DEFAULT_PROMPT_DIR
    return (Path(configured).expanduser() / _ENTITY_TYPE_SUBDIR).resolve()


def resolve_entity_type_prompt_path(prompt_file_name: str | Path) -> Path:
    """Resolve an allowlisted prompt profile file name to an absolute path."""

    file_name = str(prompt_file_name).strip()
    if not file_name:
        raise ValueError(
            "ENTITY_TYPE_PROMPT_FILE must be a file name such as "
            "'entity_type_prompt.sample.yml'."
        )
    if "\\" in file_name:
        raise ValueError(
            "ENTITY_TYPE_PROMPT_FILE must not contain directory separators. "
            "Only file names inside PROMPT_DIR/entity_type are allowed."
        )

    candidate = Path(file_name)
    if (
        candidate.is_absolute()
        or candidate.name != file_name
        or ".." in candidate.parts
    ):
        raise ValueError(
            "ENTITY_TYPE_PROMPT_FILE must be a file name only. "
            "Files are loaded from PROMPT_DIR/entity_type "
            "(PROMPT_DIR defaults to ./prompts)."
        )
    if candidate.suffix.lower() not in _ALLOWED_PROMPT_SUFFIXES:
        raise ValueError(
            "ENTITY_TYPE_PROMPT_FILE must use a '.yml' or '.yaml' extension."
        )

    return get_entity_type_prompt_dir() / candidate.name


def _normalize_prompt_examples(
    value: Any, field_name: str, profile_path: Path
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(
            f"ENTITY_TYPE_PROMPT_FILE '{profile_path}' field '{field_name}' "
            "must be a list of strings."
        )
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"ENTITY_TYPE_PROMPT_FILE '{profile_path}' field '{field_name}' "
                f"item {index} must be a non-empty string."
            )
        normalized.append(item.rstrip())
    return normalized


def load_entity_extraction_prompt_profile(
    prompt_file: str | Path,
) -> dict[str, Any]:
    """Load and validate an entity extraction prompt profile from YAML."""

    profile_path = Path(prompt_file)
    if not profile_path.exists():
        raise FileNotFoundError(
            f"ENTITY_TYPE_PROMPT_FILE '{profile_path}' does not exist."
        )
    if not profile_path.is_file():
        raise ValueError(
            f"ENTITY_TYPE_PROMPT_FILE '{profile_path}' must point to a file."
        )

    try:
        content = profile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(
            f"Failed to read ENTITY_TYPE_PROMPT_FILE '{profile_path}': {exc}"
        ) from exc

    try:
        raw_profile = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(
            f"ENTITY_TYPE_PROMPT_FILE '{profile_path}' contains invalid YAML: {exc}"
        ) from exc

    if raw_profile is None:
        raw_profile = {}
    if not isinstance(raw_profile, dict):
        raise ValueError(
            f"ENTITY_TYPE_PROMPT_FILE '{profile_path}' must contain a YAML mapping."
        )

    profile: dict[str, Any] = {}

    guidance = raw_profile.get("entity_types_guidance")
    if guidance is not None:
        if not isinstance(guidance, str) or not guidance.strip():
            raise ValueError(
                f"ENTITY_TYPE_PROMPT_FILE '{profile_path}' field "
                "'entity_types_guidance' must be a non-empty string."
            )
        profile["entity_types_guidance"] = guidance.rstrip()

    for field_name in (
        "entity_extraction_examples",
        "entity_extraction_json_examples",
    ):
        if field_name in raw_profile:
            profile[field_name] = _normalize_prompt_examples(
                raw_profile[field_name], field_name, profile_path
            )

    return profile


def resolve_entity_extraction_prompt_profile(
    addon_params: Mapping[str, Any] | None,
    use_json: bool,
) -> EntityExtractionPromptProfile:
    """Resolve and merge the configured entity extraction prompt profile."""

    default_profile = get_default_entity_extraction_prompt_profile()
    addon_params = addon_params or {}
    prompt_file = addon_params.get("entity_type_prompt_file")

    file_profile: dict[str, Any] = {}
    if prompt_file:
        prompt_path = resolve_entity_type_prompt_path(prompt_file)
        file_profile = load_entity_extraction_prompt_profile(prompt_path)
        required_examples_key = (
            "entity_extraction_json_examples"
            if use_json
            else "entity_extraction_examples"
        )
        if required_examples_key not in file_profile:
            mode_name = "json" if use_json else "text"
            raise ValueError(
                f"ENTITY_TYPE_PROMPT_FILE '{prompt_file}' must define "
                f"'{required_examples_key}' when entity extraction runs in "
                f"{mode_name} mode."
            )

    guidance = addon_params.get("entity_types_guidance")
    if guidance is None:
        guidance = file_profile.get(
            "entity_types_guidance", default_profile["entity_types_guidance"]
        )
    elif not isinstance(guidance, str) or not guidance.strip():
        raise ValueError(
            "addon_params['entity_types_guidance'] must be a non-empty string."
        )

    return {
        "entity_types_guidance": guidance,
        "entity_extraction_examples": list(
            file_profile.get(
                "entity_extraction_examples",
                default_profile["entity_extraction_examples"],
            )
        ),
        "entity_extraction_json_examples": list(
            file_profile.get(
                "entity_extraction_json_examples",
                default_profile["entity_extraction_json_examples"],
            )
        ),
    }


def validate_entity_extraction_prompt_profile_for_mode(
    prompt_profile: Mapping[str, Any],
    use_json: bool,
    prompt_file_name: str | None = None,
) -> EntityExtractionPromptProfile:
    """Validate that the resolved profile contains the active-mode examples."""

    required_examples_key = (
        "entity_extraction_json_examples" if use_json else "entity_extraction_examples"
    )
    if (
        required_examples_key not in prompt_profile
        or not prompt_profile[required_examples_key]
    ):
        mode_name = "json" if use_json else "text"
        source = (
            f"ENTITY_TYPE_PROMPT_FILE '{prompt_file_name}'"
            if prompt_file_name
            else "the resolved prompt profile"
        )
        raise ValueError(
            f"{source} must define '{required_examples_key}' when entity extraction "
            f"runs in {mode_name} mode."
        )

    return {
        "entity_types_guidance": str(prompt_profile["entity_types_guidance"]).rstrip(),
        "entity_extraction_examples": [
            str(example).rstrip()
            for example in prompt_profile["entity_extraction_examples"]
        ],
        "entity_extraction_json_examples": [
            str(example).rstrip()
            for example in prompt_profile["entity_extraction_json_examples"]
        ],
    }
