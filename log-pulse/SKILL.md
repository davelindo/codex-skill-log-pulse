---
name: log-pulse
description: Reduce token usage for long-running tests/builds by logging full output to a file and emitting periodic 1-line "pulse" summaries. Use for noisy commands like full test sweeps, builds, docker compose integration runs, or when asked to tee output to a log and show periodic summaries.
---

# log-pulse

Use this skill to keep noisy command output out of the conversation by logging stdout/stderr to a file and emitting periodic one-line pulses.

## Default workflow

1. Create a log file (prefer `mktemp`) and tell the user where it is.
2. Start required services (for example, `docker compose up -d postgres`) if needed.
3. Run the noisy command with pulse monitoring using `scripts/pulse.py run`:
   - write full output to the log
   - print only one-line pulses to the terminal/LLM
4. On completion:
   - report exit code and log path
   - if it failed or pulses show errors/warnings, run `scripts/pulse.py extract` to show a compact report
5. Clean up only if asked (do not tear down shared services by default).

## Commands

### Repo-scoped skill path

```bash
python3 .codex/skills/log-pulse/scripts/pulse.py run --window 10 --interval 5 -- <COMMAND...>
```

### User-scoped skill path

```bash
python3 ~/.codex/skills/log-pulse/scripts/pulse.py run --window 10 --interval 5 -- <COMMAND...>
```

## Example: Docker Compose + Postgres + full test sweep

```bash
LOG="$(mktemp -t test-sweep.XXXXXX.log)"

docker compose up -d postgres

python3 .codex/skills/log-pulse/scripts/pulse.py run \
  --log "$LOG" \
  --window 10 \
  --interval 5 \
  -- \
  docker compose run --rm app pytest -q

# If it failed (or you saw errors/warnings in pulses):
python3 .codex/skills/log-pulse/scripts/pulse.py extract --log "$LOG" --show-tail --tail-lines 80

# Optional cleanup:
docker compose down
```

## Pulse line format

```
last 10s: +243 lines | errors:0 | warnings:0 | total:12034 | last:"...optional excerpt..."
```

Interpretation:
- Interpret `last 10s: +X lines` as the scrollback avoided in the last window.
- Treat `errors/warnings` as regex matches on new lines in that time window (heuristic).
- Treat `total` as total lines written so far.

## Tuning error/warning matching

```bash
export PULSE_ERROR_REGEX="ERROR;FAILED;Traceback;panic"
export PULSE_WARNING_REGEX="WARNING;DeprecationWarning"
```

(Comma-separated also works.)

## Script entry points

- Use `scripts/pulse.py run` to execute a command and emit periodic pulse lines.
- Use `scripts/pulse.py pulse` to emit a single pulse line for an existing log.
- Use `scripts/pulse.py extract` to show a compact error/warning summary and optional tail.
