# Library Migration Agent

This repository contains the implementation code for a mini-SWE-agent based Python library migration agent.

## Included Code

- `src/minisweagent/agents`: agent control flow.
- `src/minisweagent/environments`: local/container execution environments.
- `src/minisweagent/models`: model adapter layer.
- `src/minisweagent/migration`: PIG-style migration context construction, API discovery, candidate ranking, code slicing, and static verification.
- `src/minisweagent/run/migrate.py`: CLI entry point for structured migration tasks.
- `src/minisweagent/run/migration_tools.py`: agent-callable PIG-style helper commands for discovery, slices, candidates, and verification.
- `src/minisweagent/config/migration.yaml`: migration-agent configuration and prompt layer.

## Not Included

This upload copy intentionally excludes benchmark datasets, run outputs, trajectories, papers, slides, audio files, generated report assets, Skill plugins, and local runtime artifacts.

## Basic Usage

Install in editable mode:

```bash
pip install -e .
```

Run a migration task:

```bash
mini-migrate --project /path/to/project --source old_library --target new_library
```

Or use the natural-language wrapper:

```bash
mini-migrate-chat "请把 /path/to/project 这个项目从 Flask 迁移到 Quart"
```

Or start a local browser UI with a natural-language input box:

```bash
mini-migrate-ui --port 8765
```

Then open `http://127.0.0.1:8765`, provide only:

- repository folder path;
- natural-language migration instruction.

Internal PIG context and strict verification reports are generated automatically under `outputs/migration_ui/`.
The full PIG report is written as JSON; the agent prompt receives only a concise summary plus helper commands, so PIG is used as an automated workflow support layer rather than a full prompt dump.

For the demo project:

```bash
mini-migrate-chat \
  "请把 demo_projects/flask_weather_api 这个项目从 Flask 迁移到 Quart" \
  --test-command "/opt/anaconda3/bin/python3 -m pytest tests/test_quart_migration.py -q" \
  --strict-static-check \
  --strict-report outputs/teacher_review_2026-05-20/demo_flask_weather_strict_report.json
```

Browser UI without installing the package:

```bash
cd /Users/wangwenjing/Desktop/LLM4SE
PYTHONPATH=github_upload/mini-swe-agent-agent-code/src \
/opt/anaconda3/bin/python3 -m minisweagent.run.migrate_ui --port 8765
```

Optionally provide a local PyMigBench-style YAML file for migration facts:

```bash
mini-migrate --project /path/to/project --source old_library --target new_library --pymigbench-yaml /path/to/sample.yaml
```

Run PIG-style static/API verification without invoking the agent:

```bash
mini-migrate --project /path/to/project --source old_library --target new_library --verify-only
```

Use the PIG helper tools directly:

```bash
mini-migrate-tools discover --project /path/to/project --source old_library --target new_library
mini-migrate-tools slices --project /path/to/project --source old_library --target new_library --max-slices 12
mini-migrate-tools candidates --project /path/to/project --source old_library --target new_library
mini-migrate-tools verify --project /path/to/project --source old_library --target new_library
```

For benchmark runs, strict verification can use a PyMigBench-style YAML record as ground truth and write a JSON report:

```bash
mini-migrate --project /path/to/project --pymigbench-yaml /path/to/sample.yaml --verify-only --strict-report migration-check.json
```

Run the agent and then fail the CLI if strict static/API checks fail:

```bash
mini-migrate --project /path/to/project --pymigbench-yaml /path/to/sample.yaml --strict-static-check --strict-report migration-check.json
```
