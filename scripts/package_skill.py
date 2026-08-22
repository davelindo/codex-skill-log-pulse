#!/usr/bin/env python3
"""Create a deterministic, distributable .skill archive.

Usage:
    python scripts/package_skill.py <path/to/skill-folder> [output-directory]

Example:
    python scripts/package_skill.py log-pulse
    python scripts/package_skill.py log-pulse ./dist
"""

import stat
import sys
import zipfile
from pathlib import Path

from quick_validate import validate_skill


IGNORED_NAMES = {".DS_Store", "__pycache__"}


def _included_files(skill_path):
    for file_path in sorted(skill_path.rglob("*")):
        if not file_path.is_file():
            continue
        if any(part in IGNORED_NAMES for part in file_path.parts):
            continue
        if file_path.suffix in {".pyc", ".pyo"}:
            continue
        yield file_path


def _zip_info(file_path, arcname):
    info = zipfile.ZipInfo(str(arcname).replace("\\", "/"), date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = stat.S_IMODE(file_path.stat().st_mode)
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.create_system = 3
    return info


def package_skill(skill_path, output_dir=None):
    """
    Package a skill folder into a .skill file.

    Args:
        skill_path: Path to the skill folder
        output_dir: Optional output directory for the .skill file (defaults to current directory)

    Returns:
        Path to the created .skill file, or None if error
    """
    skill_path = Path(skill_path).resolve()

    if not skill_path.exists():
        print(f"[ERROR] Skill folder not found: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"[ERROR] Path is not a directory: {skill_path}")
        return None

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"[ERROR] SKILL.md not found in {skill_path}")
        return None

    print("Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"[ERROR] Validation failed: {message}")
        print("   Please fix the validation errors before packaging.")
        return None
    print(f"[OK] {message}\n")

    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path.cwd()

    skill_filename = output_path / f"{skill_name}.skill"

    try:
        with zipfile.ZipFile(skill_filename, "w") as archive:
            for file_path in _included_files(skill_path):
                arcname = file_path.relative_to(skill_path.parent)
                archive.writestr(_zip_info(file_path, arcname), file_path.read_bytes())
                print(f"  Added: {arcname}")

        print(f"\n[OK] Successfully packaged skill to: {skill_filename}")
        return skill_filename

    except Exception as exc:
        print(f"[ERROR] Error creating .skill file: {exc}")
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/package_skill.py <path/to/skill-folder> [output-directory]")
        print("\nExample:")
        print("  python scripts/package_skill.py log-pulse")
        print("  python scripts/package_skill.py log-pulse ./dist")
        sys.exit(1)

    skill_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Packaging skill: {skill_path}")
    if output_dir:
        print(f"   Output directory: {output_dir}")
    print()

    result = package_skill(skill_path, output_dir)

    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
