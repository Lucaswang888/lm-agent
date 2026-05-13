# GitHub Upload Manifest

This folder is the cleaned upload copy for the library migration agent implementation.

Included:

- `src/minisweagent/`: runnable mini-SWE-agent implementation code.
- `src/minisweagent/migration/`: PIG-style migration context, slicing, candidate ranking, and static verification code.
- `src/minisweagent/run/migrate.py`: migration-agent CLI entrypoint.
- `src/minisweagent/run/migrate_chat.py`: natural-language wrapper for migration requests.
- `src/minisweagent/run/migrate_ui.py`: local browser UI with a natural-language input box.
- `src/minisweagent/run/migration_tools.py`: agent-callable PIG-style helper commands.
- `src/minisweagent/config/migration.yaml`: migration prompt/config layer.
- `pyproject.toml`, `README.md`, `LICENSE.md`, `.gitignore`: minimal project metadata.

Key CLI capabilities:

- `mini-migrate --print-task`: render the project-level migration task without running the agent.
- `mini-migrate-chat "请把 /path/to/project 这个项目从 Flask 迁移到 Quart"`: parse a natural-language migration request and delegate to `mini-migrate`.
- `mini-migrate-ui --port 8765`: open a local browser UI for natural-language migration requests.
- `mini-migrate --pig-report`: export PIG-style discovery/slicing/candidate context as JSON.
- `mini-migrate-tools discover/slices/candidates/verify`: run PIG-style steps on demand instead of relying on a full prompt dump.
- `mini-migrate --verify-only`: run static/API checks without invoking the model.
- `mini-migrate --strict-static-check --strict-report`: run strict static/API verification after the agent exits and write JSON evidence.

Excluded from this upload copy:

- Test datasets and benchmark/run outputs, including `pymigbench-*`, `results`, `trajectories`, and `logs`.
- Papers, PDFs, PPT/PPTX, audio, generated presentation/report assets.
- Skill/plugin/runtime artifacts such as `.omx`, `.codex`, `.agents`, installer logs, and command scripts.
- External source trees and datasets such as `PyMigBench`, `official_pig_artifact`, and `official_pig_eval`.

Suggested upload root:

```bash
cd github_upload/mini-swe-agent-agent-code
git init
git add .
git status
```
