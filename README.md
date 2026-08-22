# log-pulse

Run noisy tests, builds, and integration commands without sending their full output into an LLM context window. `log-pulse` keeps combined stdout/stderr in a file and emits only bounded error alerts, opt-in progress, sparse heartbeats, and the final outcome.

## Install

Ask Codex to use `$skill-installer` with this skill directory:

```text
$skill-installer install https://github.com/davelindo/codex-skill-log-pulse/tree/main/log-pulse
```

For direct local discovery, repository skills belong under `.agents/skills/` and personal skills under `~/.agents/skills/`.

## Use

From a personal installation:

```bash
python3 ~/.agents/skills/log-pulse/scripts/pulse.py run -- <command...>
```

From a repository installation:

```bash
python3 .agents/skills/log-pulse/scripts/pulse.py run -- <command...>
```

By default, a short healthy command emits two lines: the generated log path and the final result. The command itself is hidden unless `--show-command` is supplied.

Useful options:

```text
--log PATH                  Use a stable full-output log path
--append                    Append and monitor only newly written output
--heartbeat SECONDS         Liveness interval after no other visible event (default: 60)
--progress-regex REGEX      Surface known milestone lines; repeatable
--progress-interval SECONDS Minimum interval between progress excerpts (default: 30)
--error-regex REGEX         Replace default error matching; repeatable
--warning-regex REGEX       Replace default warning matching; repeatable
--descendant-timeout SEC    Wait after the main process exits (default: 30)
```

Warnings are batched into heartbeats and the final result. Up to three unique error patterns are surfaced immediately; later errors still contribute to exact totals. LLM-visible excerpts strip terminal controls and redact common credential assignments, while the raw log remains unchanged.

If descendants outlive the timeout, the wrapper returns 124 and reports their process-group ID without terminating them. Inspect that group and the log before deciding whether to keep waiting or terminate it.

Process-group waiting and timeout handoff are available on POSIX systems. On other platforms, the wrapper follows the main process only.

For bounded offline diagnosis:

```bash
python3 ~/.agents/skills/log-pulse/scripts/pulse.py extract --log <log-path>
```

## Develop and package

Install test dependencies and run the complete checks:

```bash
python3 -m pip install pytest
pytest -q
python3 scripts/quick_validate.py log-pulse
python3 scripts/package_skill.py log-pulse dist
```

The package is written to `dist/log-pulse.skill`.

Repository layout:

- `log-pulse/`: installable skill and runtime script
- `tests/`: behavioral, lifecycle, and packaging tests
- `scripts/`: validation and deterministic packaging helpers
