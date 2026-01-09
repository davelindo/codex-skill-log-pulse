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
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

    mode = "ab" if args.append else "wb"
    with log_path.open(mode) as log_f:
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, cwd=args.cwd, env=_build_env(args))

    last_emit = 0.0
    while True:
        rc = proc.poll()
        now = _now()
        if rc is None and (now - last_emit) < float(args.interval):
            time.sleep(0.1)
            continue

        print(pulse_once(log_path, state_path, window_s=args.window, include_last_line=not args.no_last_line))
        last_emit = now

        if rc is not None:
            status = "ok" if rc == 0 else "FAILED"
            print(f"pulse: {status} exit={rc} log={log_path}")
            if rc != 0:
                print(
                    "pulse: extract: python3 "
                    + shlex.quote(str(Path(__file__).resolve()))
                    + " extract --log "
                    + shlex.quote(str(log_path))
                )
            return int(rc)


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
