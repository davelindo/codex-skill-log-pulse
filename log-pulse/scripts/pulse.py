#!/usr/bin/env python3
"""
pulse.py - keep noisy command output out of the Codex conversation.

Core idea:
- write *all* stdout/stderr to a log file
- periodically print a single "pulse" line summarizing recent log activity:
    last 10s: +243 lines | errors:0 warnings:0 | total:12034

This reduces token usage during long-running test/build/debug sessions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

DEFAULT_ERROR_REGEXES = [
    r"\berror\b",
    r"\bfatal\b",
    r"\bpanic\b",
    r"\bfail(?:ed|ure)?\b",
    r"\bexception\b",
    r"\btraceback\b",
]

DEFAULT_WARNING_REGEXES = [
    r"\bwarning\b",
    r"\bdeprecationwarning\b",
]

PROC_POLL_INTERVAL_S = 0.1
_PS_PGID_UNAVAILABLE = object()
_PS_PGID_ARGS: Any = None


def _now() -> float:
    return time.time()


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _parse_regex_list(val: str) -> List[str]:
    # Accept comma or semicolon separated.
    parts = [p.strip() for p in re.split(r"[;,]\s*", val) if p.strip()]
    return parts


def _compile_regexes(regexes: Sequence[str]) -> List[re.Pattern]:
    return [re.compile(r, re.IGNORECASE) for r in regexes]


def _load_patterns() -> Tuple[List[re.Pattern], List[re.Pattern]]:
    err = os.environ.get("PULSE_ERROR_REGEX")
    warn = os.environ.get("PULSE_WARNING_REGEX")
    err_list = _parse_regex_list(err) if err else DEFAULT_ERROR_REGEXES
    warn_list = _parse_regex_list(warn) if warn else DEFAULT_WARNING_REGEXES
    return _compile_regexes(err_list), _compile_regexes(warn_list)


def _match_any(patterns: List[re.Pattern], line: str) -> bool:
    return any(p.search(line) for p in patterns)


def _short(s: str, n: int) -> str:
    s = s.strip()
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    if n <= 3:
        return s[:n]
    return s[: n - 3] + "..."


def _signal_name(sig_num: int) -> str:
    try:
        return signal.Signals(sig_num).name
    except Exception:
        return f"SIG{sig_num}"


def _ps_pgid_candidates() -> List[List[str]]:
    if sys.platform.startswith("darwin"):
        return [
            ["ps", "-o", "pid=", "-g"],
            ["ps", "-o", "pid=", "--pgid"],
        ]
    if sys.platform.startswith("linux"):
        return [
            ["ps", "-o", "pid=", "--pgid"],
            ["ps", "-o", "pid=", "-g"],
        ]
    return [
        ["ps", "-o", "pid=", "-g"],
        ["ps", "-o", "pid=", "--pgid"],
    ]


def _can_track_group() -> bool:
    return os.name == "posix" and hasattr(os, "killpg")


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_pid_list(text: str) -> Set[int]:
    pids: Set[int] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.add(int(line))
        except ValueError:
            continue
    return pids


def _run_ps_pgid(args: List[str], pgid: int) -> Tuple[Optional[Set[int]], bool]:
    try:
        result = subprocess.run(
            [*args, str(pgid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except (FileNotFoundError, PermissionError, OSError):
        return None, False
    if result.returncode != 0 and result.stderr.strip():
        return None, False
    return _parse_pid_list(result.stdout), True


def _list_pgid_pids(pgid: int) -> Optional[Set[int]]:
    global _PS_PGID_ARGS
    if _PS_PGID_ARGS is _PS_PGID_UNAVAILABLE:
        return None
    if isinstance(_PS_PGID_ARGS, list):
        pids, ok = _run_ps_pgid(_PS_PGID_ARGS, pgid)
        if ok:
            return pids
        _PS_PGID_ARGS = _PS_PGID_UNAVAILABLE
        return None
    for args in _ps_pgid_candidates():
        pids, ok = _run_ps_pgid(args, pgid)
        if ok:
            _PS_PGID_ARGS = args
            return pids
    _PS_PGID_ARGS = _PS_PGID_UNAVAILABLE
    return None


def _diff_proc_changes(previous: Set[int], current: Set[int]) -> List[str]:
    events: List[str] = []
    started = sorted(current - previous)
    exited = sorted(previous - current)
    for pid in started:
        events.append(f"pulse: proc-start pid={pid}")
    for pid in exited:
        events.append(f"pulse: proc-exit pid={pid}")
    return events


def _emit_exit(exit_code: int, log_path: Path) -> int:
    normalized = exit_code
    if exit_code < 0:
        sig = -exit_code
        normalized = 128 + sig
        print(f"pulse: FAILED signal={_signal_name(sig)} exit={normalized} log={log_path}")
    else:
        status = "ok" if exit_code == 0 else "FAILED"
        print(f"pulse: {status} exit={exit_code} log={log_path}")
    if normalized != 0:
        print(
            "pulse: extract: python3 "
            + shlex.quote(str(Path(__file__).resolve()))
            + " extract --log "
            + shlex.quote(str(log_path))
        )
    return int(normalized)


class _ProcTracker:
    def __init__(self, proc: subprocess.Popen, pgid: Optional[int], track_group_alive: bool) -> None:
        self.track_group_alive = track_group_alive
        self.track_group_list = False
        self.pgid = pgid
        self.last_pids: Set[int] = set()
        self._events: List[str] = []
        self._seed(proc)

    def _seed(self, proc: subprocess.Popen) -> None:
        current_pids: Optional[Set[int]] = None
        if self.track_group_alive and self.pgid is not None:
            current_pids = _list_pgid_pids(self.pgid)
            if current_pids is not None:
                self.track_group_list = True
                if not current_pids and proc.poll() is None:
                    current_pids = None
        if current_pids is None:
            current_pids = {proc.pid}
        self.last_pids = current_pids
        self._events.extend(_diff_proc_changes(set(), current_pids))

    def scan(self, proc: subprocess.Popen, rc: Optional[int]) -> Optional[Set[int]]:
        current_pids: Optional[Set[int]] = None
        if self.track_group_list and self.pgid is not None:
            current_pids = _list_pgid_pids(self.pgid)
            if current_pids is not None and not current_pids and rc is None:
                current_pids = None
        elif not self.track_group_list:
            current_pids = {proc.pid} if rc is None else set()
        if current_pids is not None:
            self._events.extend(_diff_proc_changes(self.last_pids, current_pids))
            self.last_pids = current_pids
        return current_pids

    def group_alive(self, current_pids: Optional[Set[int]], rc: Optional[int]) -> bool:
        if self.track_group_alive and self.pgid is not None:
            if self.track_group_list and current_pids is not None:
                return len(current_pids) > 0
            return _pgid_alive(self.pgid)
        return rc is None

    def ensure_exit_events(self, group_alive: bool, current_pids: Optional[Set[int]]) -> None:
        if not group_alive and current_pids is None and self.last_pids:
            self._events.extend(_diff_proc_changes(self.last_pids, set()))
            self.last_pids = set()

    def drain_events(self) -> List[str]:
        if not self._events:
            return []
        events = self._events
        self._events = []
        return events


def _default_state() -> Dict[str, Any]:
    return {"created_at": _now(), "pos": 0, "total_lines": 0, "samples": [], "last_line": ""}


def _load_state(state_path: Path) -> Dict[str, Any]:
    st = _load_json(state_path)
    if not isinstance(st, dict):
        st = _default_state()
    for k, v in _default_state().items():
        st.setdefault(k, v)
    if not isinstance(st.get("samples"), list):
        st["samples"] = []
    return st


def _read_delta(
    log_path: Path,
    start_pos: int,
    err_pats: List[re.Pattern],
    warn_pats: List[re.Pattern],
) -> Tuple[int, int, int, str, int]:
    """
    Returns (new_lines, err_lines, warn_lines, last_line, end_pos).
    Counts lines by '\n'. Treats file truncation as reset.
    """
    try:
        end_pos = log_path.stat().st_size
    except FileNotFoundError:
        return 0, 0, 0, "", start_pos

    if end_pos < start_pos:
        start_pos = 0

    if end_pos == start_pos:
        return 0, 0, 0, "", end_pos

    new_lines = err_lines = warn_lines = 0
    last_line = ""
    carry = ""

    with log_path.open("rb") as f:
        f.seek(start_pos)
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if carry:
                text = carry + text
                carry = ""
            parts = text.split("\n")
            carry = parts.pop() if parts else ""
            for line in parts:
                new_lines += 1
                if line:
                    if _match_any(err_pats, line):
                        err_lines += 1
                    if _match_any(warn_pats, line):
                        warn_lines += 1
                    last_line = line

    if carry:
        last_line = carry

    return new_lines, err_lines, warn_lines, _short(last_line, 120), end_pos


def _prune_samples(samples: List[Dict[str, Any]], cutoff: float) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for s in samples:
        try:
            if float(s.get("t", 0)) >= cutoff:
                out.append(s)
        except Exception:
            pass
    return out


def pulse_once(log_path: Path, state_path: Path, window_s: int, include_last_line: bool = True) -> str:
    err_pats, warn_pats = _load_patterns()
    now = _now()
    state = _load_state(state_path)

    # Read new data since last pos
    new_lines, err_lines, warn_lines, last_line, end_pos = _read_delta(
        log_path, int(state.get("pos", 0)), err_pats, warn_pats
    )

    state["pos"] = int(end_pos)
    state["total_lines"] = int(state.get("total_lines", 0)) + int(new_lines)
    if last_line:
        state["last_line"] = last_line

    # Record sample and prune
    samples = list(state.get("samples", []))
    samples.append({"t": now, "lines": new_lines, "err": err_lines, "warn": warn_lines})
    cutoff = now - float(window_s) - 1.0
    samples = _prune_samples(samples, cutoff)
    state["samples"] = samples

    # Window totals
    window_cutoff = now - float(window_s)
    w_lines = w_err = w_warn = 0
    for s in samples:
        if float(s.get("t", 0)) < window_cutoff:
            continue
        w_lines += int(s.get("lines", 0))
        w_err += int(s.get("err", 0))
        w_warn += int(s.get("warn", 0))

    _atomic_write_json(state_path, state)

    parts = [
        f"last {window_s}s: +{w_lines} lines",
        f"errors:{w_err}",
        f"warnings:{w_warn}",
        f"total:{int(state['total_lines'])}",
    ]
    if include_last_line and state.get("last_line"):
        parts.append(f'last:"{state["last_line"]}"')
    if not log_path.exists():
        parts.append(f"log-missing:{log_path}")
    return " | ".join(parts)


def cmd_pulse(args: argparse.Namespace) -> int:
    log_path = Path(args.log).expanduser()
    state_path = Path(args.state or (str(log_path) + ".pulse.json")).expanduser()
    print(pulse_once(log_path, state_path, window_s=args.window, include_last_line=not args.no_last_line))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise SystemExit("Usage: pulse.py run [opts] -- <command...>")

    if args.log:
        log_path = Path(args.log).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not args.append:
            log_path.write_bytes(b"")
    else:
        fd, p = tempfile.mkstemp(prefix="pulse-", suffix=".log")
        os.close(fd)
        log_path = Path(p)

    state_path = Path(args.state or (str(log_path) + ".pulse.json")).expanduser()
    if not args.reuse_state and state_path.exists():
        try:
            state_path.unlink()
        except Exception:
            pass

    print(f"pulse: log={log_path}")
    print(f"pulse: cmd={' '.join(shlex.quote(x) for x in cmd)}")

    track_group_alive = _can_track_group()
    mode = "ab" if args.append else "wb"
    with log_path.open(mode) as log_f:
        popen_kwargs = {
            "stdout": log_f,
            "stderr": subprocess.STDOUT,
            "cwd": args.cwd,
            "env": _build_env(args),
        }
        if track_group_alive:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)
    pgid = proc.pid if track_group_alive else None

    tracker = _ProcTracker(proc, pgid, track_group_alive)
    interval_s = max(0.0, float(args.interval))
    last_emit = _now()
    next_pulse = last_emit + interval_s
    next_proc_scan = last_emit
    primary_rc: Optional[int] = None
    while True:
        rc = proc.poll()
        if rc is not None and primary_rc is None:
            primary_rc = rc
        now = _now()

        current_pids: Optional[Set[int]] = None
        if now >= next_proc_scan:
            current_pids = tracker.scan(proc, rc)
            next_proc_scan = now + PROC_POLL_INTERVAL_S

        group_alive = tracker.group_alive(current_pids, rc)
        tracker.ensure_exit_events(group_alive, current_pids)

        if now < next_pulse and group_alive:
            sleep_for = min(next_pulse, next_proc_scan) - now
            if sleep_for > 0:
                time.sleep(min(sleep_for, 0.1))
            continue

        for line in tracker.drain_events():
            print(line)
        print(pulse_once(log_path, state_path, window_s=args.window, include_last_line=not args.no_last_line))
        last_emit = now
        next_pulse = last_emit + interval_s

        if not group_alive:
            exit_code = primary_rc if primary_rc is not None else (proc.poll() or 0)
            return _emit_exit(exit_code, log_path)


def _build_env(args: argparse.Namespace) -> Optional[Dict[str, str]]:
    if not args.env:
        return None
    env = os.environ.copy()
    for kv in args.env:
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k] = v
    return env


def cmd_extract(args: argparse.Namespace) -> int:
    log_path = Path(args.log).expanduser()
    if not log_path.exists():
        raise SystemExit(f"log missing: {log_path}")

    err_pats, warn_pats = _load_patterns()

    max_matches = int(args.max_matches)
    tail_n = int(args.tail_lines)

    total = 0
    err: List[Tuple[int, str]] = []
    warn: List[Tuple[int, str]] = []
    tail: List[str] = []

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            total += 1
            line = line.rstrip("\n\r")
            tail.append(line)
            if len(tail) > tail_n:
                tail.pop(0)

            if _match_any(err_pats, line) and len(err) < max_matches:
                err.append((total, _short(line, int(args.max_line_len))))
            if _match_any(warn_pats, line) and len(warn) < max_matches:
                warn.append((total, _short(line, int(args.max_line_len))))

    print(f"pulse: log={log_path} lines={total} errors~={len(err)} warnings~={len(warn)}")
    if err:
        print("pulse: first error matches:")
        for ln, txt in err:
            print(f"  [E] L{ln}: {txt}")
    if warn:
        print("pulse: first warning matches:")
        for ln, txt in warn:
            print(f"  [W] L{ln}: {txt}")

    if args.show_tail:
        print(f"pulse: tail (last {tail_n} lines):")
        for t in tail:
            print(t)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pulse.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pulse", help="Print one pulse line (stateful).")
    sp.add_argument("--log", required=True)
    sp.add_argument("--state", help="Default: <log>.pulse.json")
    sp.add_argument("--window", type=int, default=10)
    sp.add_argument("--no-last-line", action="store_true")
    sp.set_defaults(func=cmd_pulse)

    sr = sub.add_parser("run", help="Run a command and emit pulse lines.")
    sr.add_argument("--log", help="Default: temp file")
    sr.add_argument("--append", action="store_true", help="Append to an existing log file")
    sr.add_argument("--state", help="Default: <log>.pulse.json")
    sr.add_argument("--reuse-state", action="store_true")
    sr.add_argument("--interval", type=float, default=5.0)
    sr.add_argument("--window", type=int, default=10)
    sr.add_argument("--cwd")
    sr.add_argument("--env", action="append", help="KEY=VALUE (repeatable)")
    sr.add_argument("--no-last-line", action="store_true")
    sr.add_argument("command", nargs=argparse.REMAINDER)
    sr.set_defaults(func=cmd_run)

    se = sub.add_parser("extract", help="Compact error/warn report from a log file.")
    se.add_argument("--log", required=True)
    se.add_argument("--max-matches", type=int, default=10)
    se.add_argument("--max-line-len", type=int, default=240)
    se.add_argument("--tail-lines", type=int, default=60)
    se.add_argument("--show-tail", action="store_true")
    se.set_defaults(func=cmd_extract)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
