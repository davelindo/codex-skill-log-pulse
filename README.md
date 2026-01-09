# log-pulse

Reduce token usage for long-running tests/builds by logging full output to a file and emitting periodic one-line "pulse" summaries.

## Repository layout

- `log-pulse/` - Codex skill (SKILL.md + scripts)
- `scripts/` - build/validation helpers for packaging
- `.github/workflows/build-skill.yml` - CI build for the .skill artifact

## Install (Codex)

Use the Codex skill installer with the repo path:

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo davelindo/codex-skill-log-pulse \
  --path log-pulse
```

Restart Codex to pick up the new skill.

## Build the .skill locally

```bash
python3 scripts/package_skill.py log-pulse dist
```

Output:

- `dist/log-pulse.skill`

## Example usage

```bash
python3 ~/.codex/skills/log-pulse/scripts/pulse.py run --window 10 --interval 5 -- <COMMAND...>
```

Optional regex tuning:

```bash
export PULSE_ERROR_REGEX="ERROR;FAILED;Traceback;panic"
export PULSE_WARNING_REGEX="WARNING;DeprecationWarning"
```
