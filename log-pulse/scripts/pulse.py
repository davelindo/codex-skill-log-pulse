#!/usr/bin/env python3
"""Run noisy commands while emitting only decision-relevant status lines."""
from __future__ import annotations

import argparse
import codecs
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Pattern, Sequence, Tuple


DEFAULT_ERROR_REGEXES = (
    r"\berror\b",
    r"\bfatal\b",
    r"\bpanic\b",
    r"\bfail(?:ed|ure)?\b",
    r"\bexception\b",
    r"\btraceback\b",
)
DEFAULT_WARNING_REGEXES = (
    r"\bwarning\b",
    r"\bdeprecationwarning\b",
)

ZERO_COUNT_REGEXES = tuple(
    re.compile(expr, re.IGNORECASE)
    for expr in (
        r"\b0\s+(?:errors?|fail(?:ed|ures?)|exceptions?|warnings?)\b",
        r"\b(?:errors?|fail(?:ed|ures?)|exceptions?|warnings?)\s*[:=]\s*(?:0|none)\b",
        r"\bno\s+(?:errors?|failures?|exceptions?|warnings?)\b",
    )
)
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\b"
    r"(\s*[:=]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|\S+)"
)
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
URL_CREDENTIAL_RE = re.compile(r"(?i)(://)[^/@\s:]+:[^/@\s]+@")
TIMESTAMP_PREFIX_RE = re.compile(
    r"^\s*(?:\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*)"
)
LEVEL_PREFIX_RE = re.compile(
    r"^\s*(?:\[(?:ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE|FATAL|CRIT)\]"
    r"|(?:ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE|FATAL|CRIT)\s*[:|-])\s*",
    re.IGNORECASE,
)
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.I)
NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?![A-Za-z])")

SCAN_INTERVAL_SECONDS = 0.1
MAX_TRACKED_GROUPS = 1000
MAX_IMMEDIATE_ERROR_ALERTS = 3
DEFAULT_EXCERPT_LENGTH = 160
DESCENDANT_TIMEOUT_EXIT = 124


def _short(value: str, limit: int) -> str:
    value = value.strip()
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _quote_excerpt(value: str, limit: int = DEFAULT_EXCERPT_LENGTH) -> str:
    value = _short(value, limit).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def _visible_text(raw: str) -> str:
    text = ANSI_RE.sub("", raw)
    text = CONTROL_RE.sub("", text)
    text = URL_CREDENTIAL_RE.sub(r"\1<redacted>@", text)
    text = BEARER_RE.sub("Bearer <redacted>", text)
    text = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    return re.sub(r"\s+", " ", text).strip()


def _fingerprint(visible: str) -> str:
    value = TIMESTAMP_PREFIX_RE.sub("", visible)
    value = LEVEL_PREFIX_RE.sub("", value)
    value = UUID_RE.sub("<uuid>", value)
    value = HEX_RE.sub("<hex>", value)
    value = NUMBER_RE.sub("<n>", value)
    return value.casefold()


def _compile_regexes(values: Iterable[str], label: str) -> List[Pattern[str]]:
    compiled: List[Pattern[str]] = []
    for value in values:
        try:
            compiled.append(re.compile(value, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"invalid {label} regex {value!r}: {exc}") from exc
    return compiled


def _classify(line: str, errors: Sequence[Pattern[str]], warnings: Sequence[Pattern[str]]) -> Optional[str]:
    candidate = line
    for pattern in ZERO_COUNT_REGEXES:
        candidate = pattern.sub("", candidate)
    if any(pattern.search(candidate) for pattern in errors):
        return "error"
    if any(pattern.search(candidate) for pattern in warnings):
        return "warning"
    return None


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except (ValueError, OSError):
        return f"SIG{number}"


def _normalize_exit_code(returncode: int) -> Tuple[int, Optional[int]]:
    if returncode < 0:
        sig = -returncode
        return 128 + sig, sig
    return int(returncode), None


@dataclass
class Group:
    count: int
    exemplar: str
    first_line: int
    last_line: int


@dataclass
class ScanResult:
    new_error_groups: List[Group]
    progress: Optional[str]


class LogMonitor:
    def __init__(
        self,
        *,
        start_pos: int,
        error_patterns: Sequence[Pattern[str]],
        warning_patterns: Sequence[Pattern[str]],
        progress_patterns: Sequence[Pattern[str]],
        now: float,
    ) -> None:
        self.pos = int(start_pos)
        self.total_lines = 0
        self.error_lines = 0
        self.warning_lines = 0
        self.last_activity = now
        self.last_nonempty = ""
        self.error_groups: Dict[str, Group] = {}
        self.warning_groups: Dict[str, Group] = {}
        self._unreported_warnings: Deque[str] = deque()
        self._error_patterns = list(error_patterns)
        self._warning_patterns = list(warning_patterns)
        self._progress_patterns = list(progress_patterns)
        self._last_progress_key: Optional[str] = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._carry = ""

    def _reset_stream(self) -> None:
        self.pos = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._carry = ""

    @staticmethod
    def _record_group(groups: Dict[str, Group], key: str, exemplar: str, line_number: int) -> Tuple[Group, bool]:
        if key not in groups and len(groups) >= MAX_TRACKED_GROUPS:
            key = "<other>"
            exemplar = "additional distinct messages"
        group = groups.get(key)
        if group is None:
            group = Group(count=1, exemplar=exemplar, first_line=line_number, last_line=line_number)
            groups[key] = group
            return group, True
        group.count += 1
        group.last_line = line_number
        group.exemplar = exemplar
        return group, False

    def _ingest_line(self, raw: str) -> Tuple[Optional[Group], Optional[str]]:
        self.total_lines += 1
        visible = _visible_text(raw)
        if not visible:
            return None, None
        self.last_nonempty = visible
        kind = _classify(visible, self._error_patterns, self._warning_patterns)
        key = _fingerprint(visible)
        new_error: Optional[Group] = None
        if kind == "error":
            self.error_lines += 1
            group, is_new = self._record_group(self.error_groups, key, visible, self.total_lines)
            if is_new:
                new_error = group
        elif kind == "warning":
            self.warning_lines += 1
            group, is_new = self._record_group(self.warning_groups, key, visible, self.total_lines)
            if is_new:
                self._unreported_warnings.append(key)

        progress: Optional[str] = None
        if self._progress_patterns and any(pattern.search(visible) for pattern in self._progress_patterns):
            progress_key = visible.casefold()
            if progress_key != self._last_progress_key:
                self._last_progress_key = progress_key
                progress = visible
        return new_error, progress

    def _collect_line(self, raw: str, new_errors: List[Group], progress: Optional[str]) -> Optional[str]:
        new_error, new_progress = self._ingest_line(raw)
        if new_error is not None:
            new_errors.append(new_error)
        return new_progress if new_progress is not None else progress

    def consume(self, log_path: Path, *, now: float, final: bool = False) -> ScanResult:
        new_errors: List[Group] = []
        progress: Optional[str] = None
        try:
            end_pos = log_path.stat().st_size
        except FileNotFoundError:
            return ScanResult(new_errors, progress)

        if end_pos < self.pos:
            self._reset_stream()
        if end_pos > self.pos:
            with log_path.open("rb") as stream:
                stream.seek(self.pos)
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    decoded = self._decoder.decode(chunk, final=False)
                    text = self._carry + decoded
                    parts = re.split(r"\r\n|\r|\n", text)
                    self._carry = parts.pop() if parts else ""
                    for raw in parts:
                        progress = self._collect_line(raw, new_errors, progress)
            self.pos = end_pos
            self.last_activity = now

        if final:
            remainder = self._carry + self._decoder.decode(b"", final=True)
            self._carry = ""
            if remainder:
                progress = self._collect_line(remainder, new_errors, progress)
        return ScanResult(new_errors, progress)

    def pop_warning_exemplar(self) -> Optional[str]:
        while self._unreported_warnings:
            key = self._unreported_warnings.popleft()
            group = self.warning_groups.get(key)
            if group is not None:
                return group.exemplar
        return None

    def latest_error(self) -> Optional[str]:
        if not self.error_groups:
            return None
        return max(self.error_groups.values(), key=lambda group: group.last_line).exemplar

    def latest_warning(self) -> Optional[str]:
        if not self.warning_groups:
            return None
        return max(self.warning_groups.values(), key=lambda group: group.last_line).exemplar


class _RunSignal(Exception):
    def __init__(self, number: int) -> None:
        super().__init__(number)
        self.number = number


def _build_child_env(values: Optional[Sequence[str]]) -> Dict[str, str]:
    env = os.environ.copy()
    for item in values or ():
        if "=" not in item:
            raise ValueError(f"invalid --env value {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError("invalid --env value; KEY cannot be empty")
        env[key] = value
    return env


def _preflight_command(command: Sequence[str], *, cwd: Optional[str], env: Dict[str, str]) -> None:
    executable = command[0]
    if cwd is not None and not Path(cwd).expanduser().is_dir():
        raise ValueError(f"working directory does not exist: {cwd}")
    if os.sep in executable or (os.altsep and os.altsep in executable):
        candidate = Path(executable).expanduser()
        if not candidate.is_absolute() and cwd:
            candidate = Path(cwd).expanduser() / candidate
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise FileNotFoundError(executable)
    elif shutil.which(executable, path=env.get("PATH")) is None:
        raise FileNotFoundError(executable)


def _group_alive(pgid: Optional[int]) -> bool:
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _forward_signal(proc: subprocess.Popen, pgid: Optional[int], number: int) -> None:
    try:
        if pgid is not None:
            os.killpg(pgid, number)
        elif proc.poll() is None:
            proc.send_signal(number)
    except ProcessLookupError:
        pass


def _format_elapsed(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{int(seconds)}s"


def _emit_final(monitor: LogMonitor, *, raw_returncode: int, elapsed: float, log_path: Path) -> int:
    exit_code, sig = _normalize_exit_code(raw_returncode)
    status = "ok" if exit_code == 0 else "FAILED"
    parts = [
        f"pulse: {status}",
        f"exit={exit_code}",
        f"elapsed={_format_elapsed(elapsed)}",
        f"lines={monitor.total_lines}",
        f"errors={monitor.error_lines}",
        f"warnings={monitor.warning_lines}",
        f"log={log_path}",
    ]
    if sig is not None:
        parts.insert(2, f"signal={_signal_name(sig)}")
    if monitor.error_lines:
        parts.append(f"error={_quote_excerpt(monitor.latest_error() or '')}")
    elif exit_code != 0 and monitor.last_nonempty:
        parts.append(f"tail={_quote_excerpt(monitor.last_nonempty)}")
    elif monitor.warning_lines:
        parts.append(f"warning={_quote_excerpt(monitor.latest_warning() or '')}")
    print(" ".join(parts), flush=True)
    return exit_code


def _resolve_log_path(value: Optional[str]) -> Path:
    if value:
        path = Path(value).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    fd, raw_path = tempfile.mkstemp(prefix="pulse-", suffix=".log")
    os.close(fd)
    return Path(raw_path).resolve()


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("run requires a command after --")
    if args.heartbeat <= 0:
        raise ValueError("--heartbeat must be greater than zero")
    if args.descendant_timeout < 0:
        raise ValueError("--descendant-timeout cannot be negative")
    if args.progress_interval < 0:
        raise ValueError("--progress-interval cannot be negative")

    error_patterns = _compile_regexes(args.error_regex or DEFAULT_ERROR_REGEXES, "error")
    warning_patterns = _compile_regexes(args.warning_regex or DEFAULT_WARNING_REGEXES, "warning")
    progress_patterns = _compile_regexes(args.progress_regex or (), "progress")
    child_env = _build_child_env(args.env)
    cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else None
    try:
        _preflight_command(command, cwd=cwd, env=child_env)
    except FileNotFoundError:
        print(f"pulse: FAILED launch exit=127 executable={shlex.quote(command[0])}", flush=True)
        return 127

    try:
        log_path = _resolve_log_path(args.log)
        start_pos = log_path.stat().st_size if args.append and log_path.exists() else 0
    except OSError as exc:
        print(f"pulse: FAILED log exit=73 reason={_quote_excerpt(str(exc))}", flush=True)
        return 73
    mode = "ab" if args.append else "wb"
    use_process_group = os.name == "posix" and hasattr(os, "killpg")
    popen_kwargs = {
        "stdout": None,
        "stderr": subprocess.STDOUT,
        "cwd": cwd,
        "env": child_env,
    }
    if use_process_group:
        popen_kwargs["start_new_session"] = True

    try:
        log_stream = log_path.open(mode)
    except OSError as exc:
        print(f"pulse: FAILED log exit=73 reason={_quote_excerpt(str(exc))}", flush=True)
        return 73
    try:
        with log_stream:
            popen_kwargs["stdout"] = log_stream
            proc = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        print(f"pulse: FAILED launch exit=126 reason={_quote_excerpt(str(exc))}", flush=True)
        return 126

    pgid = proc.pid if use_process_group else None
    started = time.monotonic()
    monitor = LogMonitor(
        start_pos=start_pos,
        error_patterns=error_patterns,
        warning_patterns=warning_patterns,
        progress_patterns=progress_patterns,
        now=started,
    )
    startup = f"pulse: running log={log_path}"
    if args.show_command:
        startup += f" command={shlex.join(command)}"
    print(startup, flush=True)

    old_handlers: Dict[int, object] = {}

    def _handle(number: int, _frame: object) -> None:
        raise _RunSignal(number)

    for number in (signal.SIGINT, signal.SIGTERM):
        old_handlers[number] = signal.getsignal(number)
        signal.signal(number, _handle)

    last_visible = started
    last_report_lines = 0
    last_progress_emit = float("-inf")
    immediate_alerts = 0
    primary_returncode: Optional[int] = None
    descendant_deadline: Optional[float] = None

    try:
        while True:
            now = time.monotonic()
            scan = monitor.consume(log_path, now=now)

            if scan.new_error_groups and immediate_alerts < MAX_IMMEDIATE_ERROR_ALERTS:
                group = scan.new_error_groups[-1]
                immediate_alerts += 1
                print(
                    f"pulse: alert errors={monitor.error_lines} new={len(scan.new_error_groups)} "
                    f"exemplar={_quote_excerpt(group.exemplar)}",
                    flush=True,
                )
                last_visible = now
                last_report_lines = monitor.total_lines

            if scan.progress is not None and (now - last_progress_emit) >= args.progress_interval:
                print(f"pulse: progress {_quote_excerpt(scan.progress)}", flush=True)
                last_visible = now
                last_report_lines = monitor.total_lines
                last_progress_emit = now

            if primary_returncode is None:
                current = proc.poll()
                if current is not None:
                    primary_returncode = proc.wait()
                    if use_process_group and _group_alive(pgid):
                        descendant_deadline = now + args.descendant_timeout

            descendants_alive = primary_returncode is not None and use_process_group and _group_alive(pgid)
            if primary_returncode is not None and not descendants_alive:
                monitor.consume(log_path, now=now, final=True)
                return _emit_final(
                    monitor,
                    raw_returncode=primary_returncode,
                    elapsed=now - started,
                    log_path=log_path,
                )

            if descendants_alive and descendant_deadline is not None and now >= descendant_deadline:
                monitor.consume(log_path, now=now, final=True)
                main_exit, _ = _normalize_exit_code(primary_returncode or 0)
                print(
                    f"pulse: ATTENTION descendant-timeout={_format_elapsed(args.descendant_timeout)} "
                    f"main_exit={main_exit} pgid={pgid} log={log_path}",
                    flush=True,
                )
                return DESCENDANT_TIMEOUT_EXIT

            if (now - last_visible) >= args.heartbeat:
                warning = monitor.pop_warning_exemplar()
                idle = max(0.0, now - monitor.last_activity)
                parts = [
                    "pulse: alive",
                    f"elapsed={_format_elapsed(now - started)}",
                    f"lines={monitor.total_lines}",
                    f"(+{monitor.total_lines - last_report_lines})",
                    f"errors={monitor.error_lines}",
                    f"warnings={monitor.warning_lines}",
                    f"idle={_format_elapsed(idle)}",
                ]
                if warning:
                    parts.append(f"warning={_quote_excerpt(warning)}")
                print(" ".join(parts), flush=True)
                last_visible = now
                last_report_lines = monitor.total_lines

            time.sleep(SCAN_INTERVAL_SECONDS)
    except _RunSignal as interrupted:
        _forward_signal(proc, pgid, interrupted.number)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _forward_signal(proc, pgid, signal.SIGKILL)
        now = time.monotonic()
        monitor.consume(log_path, now=now, final=True)
        normalized = 128 + interrupted.number
        print(
            f"pulse: FAILED signal={_signal_name(interrupted.number)} exit={normalized} "
            f"elapsed={_format_elapsed(now - started)} lines={monitor.total_lines} "
            f"errors={monitor.error_lines} warnings={monitor.warning_lines} log={log_path}",
            flush=True,
        )
        return normalized
    finally:
        for number, handler in old_handlers.items():
            signal.signal(number, handler)


def _iter_log_lines(log_path: Path) -> Iterable[Tuple[int, str]]:
    with log_path.open("r", encoding="utf-8", errors="replace", newline=None) as stream:
        for number, line in enumerate(stream, start=1):
            yield number, line.rstrip("\r\n")


def cmd_extract(args: argparse.Namespace) -> int:
    log_path = Path(args.log).expanduser().resolve()
    if not log_path.is_file():
        raise ValueError(f"log does not exist: {log_path}")
    if args.max_groups < 0 or args.max_line_len < 0 or args.tail_lines < 0:
        raise ValueError("extract limits cannot be negative")
    errors = _compile_regexes(args.error_regex or DEFAULT_ERROR_REGEXES, "error")
    warnings = _compile_regexes(args.warning_regex or DEFAULT_WARNING_REGEXES, "warning")

    total = error_count = warning_count = 0
    error_groups: Dict[str, Group] = {}
    warning_groups: Dict[str, Group] = {}
    tail: Deque[str] = deque(maxlen=args.tail_lines)
    for number, raw in _iter_log_lines(log_path):
        total = number
        visible = _visible_text(raw)
        if args.tail_lines:
            tail.append(visible)
        if not visible:
            continue
        kind = _classify(visible, errors, warnings)
        key = _fingerprint(visible)
        if kind == "error":
            error_count += 1
            LogMonitor._record_group(error_groups, key, visible, number)
        elif kind == "warning":
            warning_count += 1
            LogMonitor._record_group(warning_groups, key, visible, number)

    print(
        f"pulse: extract log={log_path} lines={total} errors={error_count} warnings={warning_count}",
        flush=True,
    )
    for prefix, groups in (("E", error_groups), ("W", warning_groups)):
        ranked = sorted(groups.values(), key=lambda group: (-group.count, group.first_line))[: args.max_groups]
        for group in ranked:
            excerpt = _short(group.exemplar, args.max_line_len)
            print(f"  {prefix} {group.count}x L{group.last_line}: {excerpt}")
    if args.show_tail:
        print(f"pulse: tail lines={len(tail)}")
        for line in tail:
            print(_short(line, args.max_line_len))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulse.py")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    run = subparsers.add_parser("run", help="Run a command with token-efficient monitoring.")
    run.add_argument("--log", help="Full-output log path; defaults to a private temporary file.")
    run.add_argument("--append", action="store_true", help="Append and monitor only newly written output.")
    run.add_argument("--heartbeat", type=float, default=60.0, help="Seconds without a visible event before liveness output.")
    run.add_argument(
        "--descendant-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for descendants after the main process exits.",
    )
    run.add_argument("--progress-regex", action="append", help="Emit matching progress lines (repeatable).")
    run.add_argument(
        "--progress-interval",
        type=float,
        default=30.0,
        help="Minimum seconds between progress excerpts.",
    )
    run.add_argument("--error-regex", action="append", help="Replace default error matching (repeatable).")
    run.add_argument("--warning-regex", action="append", help="Replace default warning matching (repeatable).")
    run.add_argument("--show-command", action="store_true", help="Include the full command in startup output.")
    run.add_argument("--cwd")
    run.add_argument("--env", action="append", help="KEY=VALUE passed to the child (repeatable).")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    extract = subparsers.add_parser("extract", help="Print a bounded diagnostic report from a log.")
    extract.add_argument("--log", required=True)
    extract.add_argument("--max-groups", type=int, default=5)
    extract.add_argument("--max-line-len", type=int, default=200)
    extract.add_argument("--show-tail", action="store_true")
    extract.add_argument("--tail-lines", type=int, default=20)
    extract.add_argument("--error-regex", action="append")
    extract.add_argument("--warning-regex", action="append")
    extract.set_defaults(func=cmd_extract)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        print(f"pulse: FAILED argument exit=2 reason={_quote_excerpt(str(exc))}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"pulse: FAILED io exit=74 reason={_quote_excerpt(str(exc))}", file=sys.stderr)
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
