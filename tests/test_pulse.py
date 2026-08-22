import importlib.util
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PULSE = ROOT / "log-pulse" / "scripts" / "pulse.py"
POSIX_PROCESS_GROUPS = os.name == "posix" and hasattr(os, "killpg")


def _write_script(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _run(args, *, timeout=10, env=None):
    run_env = os.environ.copy()
    run_env["PYTHONUNBUFFERED"] = "1"
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(PULSE), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=run_env,
        timeout=timeout,
        check=False,
    )


def _run_command(tmp_path: Path, script_body: str, *options: str, timeout=10):
    script = tmp_path / "command.py"
    _write_script(script, script_body)
    log = tmp_path / "command.log"
    result = _run(["run", "--log", str(log), *options, "--", sys.executable, str(script)], timeout=timeout)
    return result, log


def _output_lines(result) -> list[str]:
    return [line for line in result.stdout.splitlines() if line]


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_short_healthy_run_emits_only_start_and_final(tmp_path: Path) -> None:
    result, log = _run_command(
        tmp_path,
        """
        import sys
        print("hello stdout")
        print("hello stderr", file=sys.stderr)
        """,
    )
    assert result.returncode == 0
    lines = _output_lines(result)
    assert len(lines) == 2
    assert lines[0].startswith("pulse: running log=")
    assert "command=" not in lines[0]
    assert "pulse: ok exit=0" in lines[1]
    assert "lines=2" in lines[1]
    log_lines = log.read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 2
    assert set(log_lines) == {"hello stdout", "hello stderr"}


def test_sparse_heartbeats_do_not_repeat_raw_last_line(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        import time
        print("started", flush=True)
        time.sleep(0.52)
        """,
        "--heartbeat",
        "0.15",
    )
    assert result.returncode == 0
    lines = _output_lines(result)
    assert 4 <= len(lines) <= 6
    assert sum("pulse: alive" in line for line in lines) in {2, 3}
    assert "started" not in result.stdout
    assert all(len(line) < 500 for line in lines)


def test_high_volume_healthy_run_stays_at_two_visible_lines(tmp_path: Path) -> None:
    result, log = _run_command(
        tmp_path,
        """
        for number in range(5000):
            print(f"routine line {number}")
        """,
    )
    assert result.returncode == 0
    assert len(_output_lines(result)) == 2
    assert "lines=5000 errors=0 warnings=0" in _output_lines(result)[-1]
    assert len(log.read_text(encoding="utf-8").splitlines()) == 5000


def test_zero_count_phrases_are_not_alerts(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        print("12 passed, 0 failed")
        print("errors: 0, warnings: none")
        print("no exceptions")
        """,
    )
    assert result.returncode == 0
    assert "pulse: alert" not in result.stdout
    assert "errors=0 warnings=0" in result.stdout


def test_error_alerts_are_immediate_deduplicated_and_bounded(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        import time
        for label in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
            print(f"ERROR {label}", flush=True)
            time.sleep(0.12)
        """,
        "--heartbeat",
        "5",
    )
    assert result.returncode == 0
    alerts = [line for line in _output_lines(result) if line.startswith("pulse: alert")]
    assert len(alerts) == 3
    assert "errors=6" in _output_lines(result)[-1]


def test_warnings_wait_for_heartbeat_and_are_summarized_at_final(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        import time
        print("WARNING deprecated option", flush=True)
        time.sleep(0.34)
        """,
        "--heartbeat",
        "0.15",
    )
    assert result.returncode == 0
    assert "pulse: alert" not in result.stdout
    alive = [line for line in _output_lines(result) if line.startswith("pulse: alive")]
    assert any('warning="WARNING deprecated option"' in line for line in alive)
    assert 'warning="WARNING deprecated option"' in _output_lines(result)[-1]


def test_progress_is_opt_in_deduplicated_and_rate_limited(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        import time
        for value in ("phase 1/3", "phase 1/3", "phase 2/3", "phase 3/3"):
            print(value, flush=True)
            time.sleep(0.12)
        """,
        "--progress-regex",
        "^phase",
        "--progress-interval",
        "0.1",
        "--heartbeat",
        "5",
    )
    assert result.returncode == 0
    progress = [line for line in _output_lines(result) if line.startswith("pulse: progress")]
    assert len(progress) == 3
    assert progress[0].endswith('"phase 1/3"')
    assert progress[-1].endswith('"phase 3/3"')


def test_carriage_return_progress_is_treated_as_complete_updates(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        import sys
        import time
        sys.stdout.write("phase 1/2\\r")
        sys.stdout.flush()
        time.sleep(0.12)
        sys.stdout.write("phase 2/2\\r")
        sys.stdout.flush()
        time.sleep(0.12)
        """,
        "--progress-regex",
        "^phase",
        "--progress-interval",
        "0.05",
    )
    assert result.returncode == 0
    progress = [line for line in _output_lines(result) if line.startswith("pulse: progress")]
    assert len(progress) == 2
    assert "lines=2" in _output_lines(result)[-1]


def test_visible_output_redacts_secrets_but_log_remains_raw(tmp_path: Path) -> None:
    result, log = _run_command(
        tmp_path,
        """
        import sys
        print("ERROR password=secret123 token: abcdef Authorization=BearerValue", flush=True)
        sys.exit(2)
        """,
    )
    assert result.returncode == 2
    assert "secret123" not in result.stdout
    assert "abcdef" not in result.stdout
    assert "BearerValue" not in result.stdout
    assert result.stdout.count("<redacted>") >= 3
    raw = log.read_text(encoding="utf-8")
    assert "secret123" in raw and "abcdef" in raw and "BearerValue" in raw


def test_failure_without_matching_error_uses_bounded_tail(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        import sys
        print("compiler stopped unexpectedly")
        sys.exit(7)
        """,
    )
    assert result.returncode == 7
    final = _output_lines(result)[-1]
    assert "pulse: FAILED exit=7" in final
    assert 'tail="compiler stopped unexpectedly"' in final


def test_partial_invalid_utf8_and_ansi_are_handled(tmp_path: Path) -> None:
    result, log = _run_command(
        tmp_path,
        """
        import sys
        sys.stdout.buffer.write(b"\\x1b[31mERROR token=abc\\xff")
        sys.stdout.buffer.flush()
        """,
    )
    assert result.returncode == 0
    assert "errors=1" in _output_lines(result)[-1]
    assert "abc" not in result.stdout
    assert "\\x1b" not in result.stdout
    assert log.read_bytes().endswith(b"\xff")


def test_append_counts_only_new_output(tmp_path: Path) -> None:
    log = tmp_path / "append.log"
    log.write_text("old one\nold ERROR\n", encoding="utf-8")
    result = _run(
        [
            "run",
            "--log",
            str(log),
            "--append",
            "--",
            sys.executable,
            "-c",
            "print('new line')",
        ]
    )
    assert result.returncode == 0
    assert "lines=1 errors=0 warnings=0" in _output_lines(result)[-1]
    assert log.read_text(encoding="utf-8").startswith("old one\nold ERROR\n")


def test_invalid_command_and_regex_do_not_truncate_existing_log(tmp_path: Path) -> None:
    log = tmp_path / "existing.log"
    log.write_text("keep me\n", encoding="utf-8")
    missing = _run(["run", "--log", str(log), "--", "command-that-does-not-exist-xyz"])
    assert missing.returncode == 127
    assert log.read_text(encoding="utf-8") == "keep me\n"

    invalid = _run(["run", "--log", str(log), "--error-regex", "[", "--", sys.executable, "-c", "pass"])
    assert invalid.returncode == 2
    assert "invalid error regex" in invalid.stderr
    assert log.read_text(encoding="utf-8") == "keep me\n"


def test_invalid_environment_cwd_and_log_path_fail_without_tracebacks(tmp_path: Path) -> None:
    log = tmp_path / "existing.log"
    log.write_text("keep me\n", encoding="utf-8")
    bad_env = _run(["run", "--log", str(log), "--env", "MALFORMED", "--", sys.executable, "-c", "pass"])
    assert bad_env.returncode == 2
    assert "expected KEY=VALUE" in bad_env.stderr
    assert "Traceback" not in bad_env.stderr
    assert log.read_text(encoding="utf-8") == "keep me\n"

    bad_cwd = _run(
        ["run", "--log", str(log), "--cwd", str(tmp_path / "missing"), "--", sys.executable, "-c", "pass"]
    )
    assert bad_cwd.returncode == 2
    assert "working directory does not exist" in bad_cwd.stderr
    assert log.read_text(encoding="utf-8") == "keep me\n"

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")
    bad_log = _run(["run", "--log", str(blocker / "child.log"), "--", sys.executable, "-c", "pass"])
    assert bad_log.returncode == 73
    assert "pulse: FAILED log exit=73" in bad_log.stdout
    assert "Traceback" not in bad_log.stderr


def test_monitor_recovers_when_log_is_truncated(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("pulse_runtime_for_test", PULSE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    log = tmp_path / "truncate.log"
    log.write_text("ERROR a deliberately long first message\n", encoding="utf-8")
    monitor = module.LogMonitor(
        start_pos=0,
        error_patterns=module._compile_regexes(module.DEFAULT_ERROR_REGEXES, "error"),
        warning_patterns=module._compile_regexes(module.DEFAULT_WARNING_REGEXES, "warning"),
        progress_patterns=[],
        now=0.0,
    )
    monitor.consume(log, now=1.0)
    log.write_text("ERROR x\n", encoding="utf-8")
    monitor.consume(log, now=2.0, final=True)
    assert monitor.total_lines == 2
    assert monitor.error_lines == 2


def test_custom_error_and_warning_patterns_replace_defaults(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        print("ERROR ignored by override")
        print("BROKEN custom failure")
        print("NOTICE custom warning")
        """,
        "--error-regex",
        "^BROKEN",
        "--warning-regex",
        "^NOTICE",
    )
    assert result.returncode == 0
    assert "errors=1 warnings=1" in _output_lines(result)[-1]


def test_extract_reports_exact_counts_and_grouped_exemplars(tmp_path: Path) -> None:
    log = tmp_path / "extract.log"
    log.write_text(
        "\n".join(
            [
                "12 passed, 0 failed",
                "ERROR job 101 failed",
                "ERROR job 102 failed",
                "ERROR job 103 failed",
                "ERROR job 104 failed",
                "WARNING retrying",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = _run(["extract", "--log", str(log), "--max-groups", "1", "--show-tail", "--tail-lines", "2"])
    assert result.returncode == 0
    lines = _output_lines(result)
    assert "lines=6 errors=4 warnings=1" in lines[0]
    assert sum(line.startswith("  E ") for line in lines) == 1
    assert "E 4x L5: ERROR job 104 failed" in result.stdout
    assert "pulse: tail lines=2" in result.stdout


@pytest.mark.skipif(not POSIX_PROCESS_GROUPS, reason="POSIX process groups required")
def test_run_waits_for_descendant_within_timeout(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        import subprocess
        import sys
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.35)"])
        """,
        "--descendant-timeout",
        "1",
    )
    assert result.returncode == 0
    assert "descendant-timeout" not in result.stdout
    elapsed_match = re.search(r"elapsed=([0-9.]+)s", _output_lines(result)[-1])
    assert elapsed_match and float(elapsed_match.group(1)) >= 0.3


@pytest.mark.skipif(not POSIX_PROCESS_GROUPS, reason="POSIX process groups required")
def test_descendant_timeout_returns_control_and_leaves_group_running(tmp_path: Path) -> None:
    result, _ = _run_command(
        tmp_path,
        """
        import subprocess
        import sys
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        """,
        "--descendant-timeout",
        "0.2",
    )
    assert result.returncode == 124
    match = re.search(r"main_exit=0 pgid=(\d+)", result.stdout)
    assert match
    pgid = int(match.group(1))
    group_processes = subprocess.run(
        ["ps", "-o", "pid=", "-g", str(pgid)],
        capture_output=True,
        check=False,
    ).stdout
    assert _process_exists(pgid) or group_processes
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass


@pytest.mark.skipif(not POSIX_PROCESS_GROUPS, reason="POSIX process groups required")
def test_sigterm_is_forwarded_and_reported(tmp_path: Path) -> None:
    script = tmp_path / "sleep.py"
    _write_script(
        script,
        """
        import os
        import time
        print(os.getpid(), flush=True)
        time.sleep(20)
        """,
    )
    log = tmp_path / "signal.log"
    wrapper = subprocess.Popen(
        [sys.executable, str(PULSE), "run", "--log", str(log), "--", sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    deadline = time.monotonic() + 5
    child_pid = None
    while time.monotonic() < deadline:
        log_text = log.read_text(encoding="utf-8").strip() if log.exists() else ""
        if log_text:
            child_pid = int(log_text)
            break
        time.sleep(0.05)
    assert child_pid is not None
    wrapper.send_signal(signal.SIGTERM)
    stdout, stderr = wrapper.communicate(timeout=10)
    assert wrapper.returncode == 128 + signal.SIGTERM
    assert "signal=SIGTERM" in stdout
    assert stderr == ""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _process_exists(child_pid):
        time.sleep(0.05)
    assert not _process_exists(child_pid)
