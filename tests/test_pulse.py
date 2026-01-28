import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PULSE = ROOT / "log-pulse" / "scripts" / "pulse.py"


def _write_script(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _run_pulse(args, *, env=None, timeout=10):
    run_env = os.environ.copy()
    run_env["PYTHONUNBUFFERED"] = "1"
    if env:
        run_env.update(env)
    cmd = [sys.executable, str(PULSE), "run", *args]
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=run_env,
        timeout=timeout,
        check=False,
    )


def _run_pulse_cmd(args, *, timeout=10):
    cmd = [sys.executable, str(PULSE), *args]
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_run_captures_output_and_pulses(tmp_path: Path) -> None:
    script = tmp_path / "emit.py"
    _write_script(
        script,
        """
        import sys
        print("hello stdout")
        print("hello stderr", file=sys.stderr)
        """,
    )
    log = tmp_path / "out.log"
    result = _run_pulse(
        [
            "--log",
            str(log),
            "--interval",
            "0.2",
            "--window",
            "1",
            "--",
            sys.executable,
            str(script),
        ],
        timeout=10,
    )
    assert result.returncode == 0
    assert "pulse: log=" in result.stdout
    assert "pulse: cmd=" in result.stdout
    assert "pulse: ok exit=0 log=" in result.stdout
    log_text = log.read_text(encoding="utf-8", errors="replace")
    assert "hello stdout" in log_text
    assert "hello stderr" in log_text


def test_run_logs_process_group_changes(tmp_path: Path) -> None:
    script = tmp_path / "spawn.py"
    _write_script(
        script,
        """
        import subprocess
        import sys
        import time

        print("parent-start", flush=True)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time,sys; print('child-start', flush=True); time.sleep(0.4); print('child-done', flush=True)",
            ]
        )
        time.sleep(0.2)
        print("parent-mid", flush=True)
        child.wait()
        print("parent-done", flush=True)
        """,
    )
    log = tmp_path / "group.log"
    result = _run_pulse(
        [
            "--log",
            str(log),
            "--interval",
            "0.2",
            "--window",
            "1",
            "--",
            sys.executable,
            str(script),
        ],
        timeout=10,
    )
    assert result.returncode == 0
    starts = re.findall(r"pulse: proc-start pid=\d+", result.stdout)
    exits = re.findall(r"pulse: proc-exit pid=\d+", result.stdout)
    assert len(starts) >= 1
    assert len(exits) >= 1


def test_run_propagates_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "fail.py"
    _write_script(
        script,
        """
        import sys
        print("boom", flush=True)
        sys.exit(3)
        """,
    )
    log = tmp_path / "fail.log"
    result = _run_pulse(
        [
            "--log",
            str(log),
            "--interval",
            "0.2",
            "--window",
            "1",
            "--",
            sys.executable,
            str(script),
        ],
        timeout=10,
    )
    assert result.returncode == 3
    assert "pulse: FAILED exit=3" in result.stdout
    assert "pulse: extract:" in result.stdout


def test_pulse_command_summary(tmp_path: Path) -> None:
    log = tmp_path / "summary.log"
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = _run_pulse_cmd(
        ["pulse", "--log", str(log), "--window", "10"],
        timeout=5,
    )
    assert result.returncode == 0
    assert "total:3" in result.stdout
    state_path = Path(str(log) + ".pulse.json")
    assert state_path.exists()


def test_extract_command_reports_matches(tmp_path: Path) -> None:
    log = tmp_path / "extract.log"
    log.write_text("ok\nERROR boom\nwarning oops\n", encoding="utf-8")
    result = _run_pulse_cmd(
        [
            "extract",
            "--log",
            str(log),
            "--max-matches",
            "2",
            "--show-tail",
            "--tail-lines",
            "2",
        ],
        timeout=5,
    )
    assert result.returncode == 0
    assert "errors~=" in result.stdout
    assert "[E]" in result.stdout
    assert "[W]" in result.stdout


def test_run_fallback_without_ps(tmp_path: Path) -> None:
    script = tmp_path / "simple.py"
    _write_script(
        script,
        """
        import time
        print("hello", flush=True)
        time.sleep(0.3)
        """,
    )
    log = tmp_path / "no-ps.log"
    result = _run_pulse(
        [
            "--log",
            str(log),
            "--interval",
            "0.2",
            "--window",
            "1",
            "--",
            sys.executable,
            str(script),
        ],
        env={"PATH": ""},
        timeout=10,
    )
    assert result.returncode == 0
    assert "pulse: proc-start pid=" in result.stdout
    assert "pulse: proc-exit pid=" in result.stdout


def test_run_signal_exit_reports_and_code(tmp_path: Path) -> None:
    script = tmp_path / "sigterm.py"
    _write_script(
        script,
        """
        import os
        import signal
        print("before", flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
        """,
    )
    log = tmp_path / "sigterm.log"
    result = _run_pulse(
        [
            "--log",
            str(log),
            "--interval",
            "0.2",
            "--window",
            "1",
            "--",
            sys.executable,
            str(script),
        ],
        timeout=10,
    )
    assert result.returncode == 128 + signal.SIGTERM
    assert "FAILED signal=SIGTERM" in result.stdout
    assert "pulse: extract:" in result.stdout


def test_run_waits_for_child_processes(tmp_path: Path) -> None:
    script = tmp_path / "fork_exit.py"
    _write_script(
        script,
        """
        import subprocess
        import sys

        subprocess.Popen([
            sys.executable,
            "-c",
            "import time; time.sleep(0.5)",
        ])
        """,
    )
    log = tmp_path / "wait.log"
    start = time.monotonic()
    result = _run_pulse(
        [
            "--log",
            str(log),
            "--interval",
            "0.2",
            "--window",
            "1",
            "--",
            sys.executable,
            str(script),
        ],
        timeout=10,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0
    assert elapsed >= 0.3
