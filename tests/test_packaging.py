import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / "scripts" / "quick_validate.py"
PACKAGE = ROOT / "scripts" / "package_skill.py"
SKILL = ROOT / "log-pulse"


def _run_script(script: Path, *args: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_skill_validates_and_packages_deterministically(tmp_path: Path) -> None:
    validation = _run_script(VALIDATE, SKILL)
    assert validation.returncode == 0, validation.stdout + validation.stderr

    outputs = [tmp_path / "first", tmp_path / "second"]
    archives = []
    for output in outputs:
        result = _run_script(PACKAGE, SKILL, output)
        assert result.returncode == 0, result.stdout + result.stderr
        archives.append(output / "log-pulse.skill")

    assert archives[0].read_bytes() == archives[1].read_bytes()
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.namelist() == [
            "log-pulse/LICENSE.txt",
            "log-pulse/SKILL.md",
            "log-pulse/scripts/pulse.py",
        ]
        script_info = archive.getinfo("log-pulse/scripts/pulse.py")
        assert (script_info.external_attr >> 16) & 0o111


def test_validator_rejects_empty_metadata_and_name_mismatch(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "SKILL.md").write_text("---\nname: empty\ndescription: ''\n---\n", encoding="utf-8")
    empty_result = _run_script(VALIDATE, empty)
    assert empty_result.returncode == 1
    assert "Description cannot be empty" in empty_result.stdout

    mismatch = tmp_path / "folder"
    mismatch.mkdir()
    (mismatch / "SKILL.md").write_text(
        "---\nname: different\ndescription: Useful test skill.\n---\n",
        encoding="utf-8",
    )
    mismatch_result = _run_script(VALIDATE, mismatch)
    assert mismatch_result.returncode == 1
    assert "must match frontmatter name" in mismatch_result.stdout
