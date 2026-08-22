#!/usr/bin/env python3
"""Perform lightweight validation of a skill directory."""

import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # Keep local validation usable without build dependencies.
    yaml = None

MAX_SKILL_NAME_LENGTH = 64


def _fallback_frontmatter(frontmatter_text):
    """Parse the top-level scalar fields needed by this validator."""
    frontmatter = {}
    for raw_line in frontmatter_text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[0].isspace():
            continue
        if ":" not in raw_line:
            raise ValueError(f"Invalid frontmatter line: {raw_line}")
        key, value = raw_line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid frontmatter line: {raw_line}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        frontmatter[key] = value
    return frontmatter


def validate_skill(skill_path):
    """Basic validation of a skill"""
    skill_path = Path(skill_path)

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"

    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)

    try:
        frontmatter = yaml.safe_load(frontmatter_text) if yaml is not None else _fallback_frontmatter(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except Exception as exc:
        return False, f"Invalid YAML in frontmatter: {exc}"

    allowed_properties = {"name", "description", "license", "allowed-tools", "metadata"}

    unexpected_keys = set(frontmatter.keys()) - allowed_properties
    if unexpected_keys:
        allowed = ", ".join(sorted(allowed_properties))
        unexpected = ", ".join(sorted(unexpected_keys))
        return (
            False,
            f"Unexpected key(s) in SKILL.md frontmatter: {unexpected}. Allowed properties are: {allowed}",
        )

    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if not name:
        return False, "Name cannot be empty"
    if not re.match(r"^[a-z0-9-]+$", name):
        return (
            False,
            f"Name '{name}' should be hyphen-case (lowercase letters, digits, and hyphens only)",
        )
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return (
            False,
            f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens",
        )
    if len(name) > MAX_SKILL_NAME_LENGTH:
        return (
            False,
            f"Name is too long ({len(name)} characters). "
            f"Maximum is {MAX_SKILL_NAME_LENGTH} characters.",
        )
    if skill_path.name != name:
        return False, f"Skill folder '{skill_path.name}' must match frontmatter name '{name}'"

    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if not description:
        return False, "Description cannot be empty"
    if "<" in description or ">" in description:
        return False, "Description cannot contain angle brackets (< or >)"
    if len(description) > 1024:
        return (
            False,
            f"Description is too long ({len(description)} characters). Maximum is 1024 characters.",
        )

    unfinished_markers = ("TODO: describe", "[TODO", "replace with a description")
    folded_content = content.casefold()
    for marker in unfinished_markers:
        if marker.casefold() in folded_content:
            return False, f"Unfinished scaffold marker found: {marker}"

    return True, "Skill is valid!"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)
