"""XML prompt loader for the PaRAG prompt library.

Reads all ``*.xml`` files in the prompts directory and merges them into a
single flat ``dict[str, str | list[str]]``.

XML schema (POML — see prompt_library.xsd):
  <prompt_library version="1.0" domain="...">
    <constants>
      <const id="KEY" value="VALUE"/>            <!-- single-line constants -->
    </constants>
    <prompt id="KEY" type="system|user|template|constant|examples" role="...">
      <meta>
        <description>...</description>
        <variables>
          <var name="VAR_NAME" type="string" required="true"/>
        </variables>
        <aitg_controls>...</aitg_controls>
      </meta>
      <content><![CDATA[...prompt text...]]></content>   <!-- string prompts -->
      <!-- OR for type="examples" -->
      <item index="0"><![CDATA[...]]></item>
    </prompt>
  </prompt_library>

Validation:
  When a <variables> block is present, every {field_name} used in the
  <content> template must be declared as a <var>.  Undeclared fields raise
  ValueError at load time so broken edits are caught at process start, not
  mid-request.
"""

from __future__ import annotations

import string
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).parent


def load_prompts(directory: Path | None = None) -> dict[str, Any]:
    """Load and merge all XML prompt files in *directory*.

    Files are processed in alphabetical order; later files override earlier
    ones for duplicate keys (intended only for deliberate overrides).

    Raises ValueError if any prompt declares <variables> but the template
    references undeclared field names.
    """
    dir_path = directory or PROMPTS_DIR
    prompts: dict[str, Any] = {}
    for xml_file in sorted(dir_path.glob("*.xml")):
        _merge_from_file(xml_file, prompts)
    return prompts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_template_fields(content: str) -> set[str]:
    """Return the set of named {field} bases referenced in *content*."""
    fields: set[str] = set()
    try:
        for _, field_name, _, _ in string.Formatter().parse(content):
            if field_name is not None and field_name != "":
                # Strip attribute/index access: "obj.attr" or "obj[0]" → "obj"
                base = field_name.split(".")[0].split("[")[0]
                if base:
                    fields.add(base)
    except (ValueError, KeyError):
        pass  # malformed template — skip; runtime .format() will surface it
    return fields


def _validate_declared_vars(key: str, content: str, declared: set[str]) -> None:
    """Raise ValueError if *content* uses variables not listed in *declared*."""
    used = _extract_template_fields(content)
    undeclared = used - declared
    if undeclared:
        raise ValueError(
            f"Prompt '{key}': template references undeclared variables "
            f"{sorted(undeclared)}. Add them to the <variables> block in the XML."
        )


def _merge_from_file(path: Path, prompts: dict[str, Any]) -> None:
    tree = ET.parse(path)
    root = tree.getroot()

    # --- constants -----------------------------------------------------------
    for const in root.findall("constants/const"):
        key = const.get("id")
        val = const.get("value")
        if val is None:
            val = const.text or ""
        if key:
            prompts[key] = val

    # --- prompts -------------------------------------------------------------
    for prompt_el in root.findall("prompt"):
        key = prompt_el.get("id")
        if not key:
            continue

        # Collect declared variable names from optional <meta><variables> block
        declared_vars: set[str] = set()
        meta_el = prompt_el.find("meta")
        if meta_el is not None:
            for var in meta_el.findall("variables/var"):
                name = var.get("name")
                if name:
                    declared_vars.add(name)

        if prompt_el.get("type") == "examples":
            items = [
                item.text or ""
                for item in sorted(
                    prompt_el.findall("item"),
                    key=lambda e: int(e.get("index", "0")),
                )
            ]
            prompts[key] = items
        else:
            content_el = prompt_el.find("content")
            if content_el is not None and content_el.text is not None:
                content = content_el.text
                # Only validate when variables are explicitly declared
                if declared_vars:
                    _validate_declared_vars(key, content, declared_vars)
                prompts[key] = content
