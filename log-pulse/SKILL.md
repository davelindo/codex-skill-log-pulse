---
name: log-pulse
description: Run noisy, non-interactive tests, builds, and integration commands without flooding model context. Keep complete stdout/stderr in a log while surfacing only bounded failures, explicit progress, sparse liveness, and the final outcome. Do not use for short commands, interactive programs, or when the user needs live verbatim output.
---

# Log Pulse

Use `scripts/pulse.py` from this skill's loaded directory. Resolve that directory from the active `SKILL.md`; do not assume a fixed installation root.

## Run a noisy command

```bash
python3 <skill-directory>/scripts/pulse.py run -- <command...>
```

The wrapper writes combined stdout/stderr to a private temporary log by default. Its first line gives the absolute log path. Use `--log PATH` only when a stable path is useful.

For a command with a known milestone format, add a narrow progress matcher:

```bash
python3 <skill-directory>/scripts/pulse.py run \
  --progress-regex '^phase [0-9]+/[0-9]+' \
  -- \
  <command...>
```

Do not invent a broad progress regex. Without one, rely on error alerts, the 60-second heartbeat, and the final result.

## Interpret visible output

- `pulse: alert`: a new error pattern needs attention. At most three error exemplars are emitted during one run; counts continue after that limit.
- `pulse: progress`: an opt-in progress pattern changed. Progress excerpts are rate-limited.
- `pulse: alive`: no other visible event occurred for 60 seconds. It reports activity and aggregate counts without repeating arbitrary log lines.
- `pulse: ok` or `pulse: FAILED`: the main command and tracked descendants finished. Report this outcome and the log path to the user.
- `pulse: ATTENTION descendant-timeout`: the main command exited, but descendants remained after 30 seconds. The wrapper returned 124 and left that process group running.

Warnings are heuristic matches. They are batched into a heartbeat or the final line instead of emitted immediately. Error and warning excerpts are bounded and redact common credential assignments; the full log remains unchanged and can still contain sensitive data.

Descendant waiting and process-group handoff require POSIX process groups. On other platforms, the wrapper follows the main process only.

## Diagnose a failure

Use the bounded extractor only when the final line does not explain the failure:

```bash
python3 <skill-directory>/scripts/pulse.py extract --log <log-path>
```

Add `--show-tail --tail-lines 20` only when grouped error and warning exemplars are insufficient. Do not paste the full log into context.

Override matching narrowly when a tool uses domain-specific status words:

```bash
python3 <skill-directory>/scripts/pulse.py run \
  --error-regex '^BROKEN:' \
  --warning-regex '^NOTICE:' \
  -- \
  <command...>
```

## Handle descendant timeout

First inspect the reported process group and log without changing them:

```bash
ps -o pid,ppid,stat,etime,command -g <pgid>
python3 <skill-directory>/scripts/pulse.py extract --log <log-path>
```

Decide from that evidence whether the descendants are expected work, stuck work, or an unwanted background service. Continue low-frequency inspection when work is expected. Terminate the exact reported process group only when that action is within the task scope; the wrapper does not make that decision automatically.
