from unittest.mock import Mock, patch

import pytest
import yaml

from minisweagent.config import builtin_config_dir
from minisweagent.migration.context import build_pig_context, render_pig_context
from minisweagent.migration.verification import verify_project_migration
from minisweagent.run.migrate import (
    _discover_validation_commands,
    _render_agent_automation_context,
    build_migration_task,
    get_pymigbench_examples,
    load_pymigbench_yaml,
    main,
)
from minisweagent.run.utilities.mini_extra import get_docstring


def test_build_migration_task_includes_pymigbench_hints(tmp_path):
    task = build_migration_task(
        project=tmp_path,
        source="leveldb",
        target="plyvel",
        scopes=[],
        test_commands=["pytest tests/test_level.py"],
        notes=["Keep public class names stable."],
        pymigbench_data={
            "repo": "ethereum/py-evm",
            "commit": "5c273fff",
            "domain": "Database",
            "commit_url": "https://github.com/ethereum/py-evm/commit/5c273fff",
            "files": [
                {
                    "path": "evm/db/backends/level.py",
                    "code_changes": [
                        {
                            "line": "18:18",
                            "source_apis": ["LevelDB"],
                            "target_apis": ["DB"],
                            "properties": ["element name change"],
                        }
                    ],
                }
            ],
        },
    )

    assert "Migrate this Python project from `leveldb` to `plyvel`." in task
    assert "Do not clone, replace, reset, or re-create the project." in task
    assert "Priority scope:" in task
    assert "not a hard file limit" in task
    assert "setup.py" in task
    assert "pytest tests/test_level.py" in task
    assert "Keep public class names stable." in task
    assert "evm/db/backends/level.py" in task
    assert "LevelDB -> DB" in task
    assert "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in task


def test_load_pymigbench_yaml_rejects_non_mapping(tmp_path):
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("- not\n- a\n- mapping\n")

    with pytest.raises(ValueError, match="Expected a mapping"):
        load_pymigbench_yaml(yaml_path)


def test_get_pymigbench_examples_prefers_matching_pair(tmp_path):
    current_path = tmp_path / "current.yaml"
    current = {
        "repo": "example/current",
        "commit": "abc",
        "source": "argparse",
        "target": "click",
        "files": [{"path": "cli.py", "code_changes": [{"source_apis": ["add_argument"], "target_apis": ["option"]}]}],
    }
    current_path.write_text(yaml.safe_dump(current))
    matching_path = tmp_path / "matching.yaml"
    matching_path.write_text(
        yaml.safe_dump(
            {
                "repo": "example/matching",
                "commit": "def",
                "source": "argparse",
                "target": "click",
                "files": [
                    {
                        "path": "tool.py",
                        "code_changes": [
                            {
                                "source_apis": ["add_argument"],
                                "target_apis": ["option"],
                                "properties": ["parameter addition to decorated function"],
                            }
                        ],
                    }
                ],
            }
        )
    )
    unrelated_path = tmp_path / "unrelated.yaml"
    unrelated_path.write_text(
        yaml.safe_dump({"repo": "example/other", "commit": "ghi", "source": "requests", "target": "httpx"})
    )

    examples = get_pymigbench_examples(
        current_data=current,
        dataset_dir=tmp_path,
        current_yaml=current_path,
        count=1,
    )

    assert examples[0]["repo"] == "example/matching"
    assert examples[0]["_example_yaml"] == matching_path.name


def test_pig_context_discovers_slices_and_candidates(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    module = package / "client.py"
    module.write_text(
        "import requests\n\n"
        "def fetch(url):\n"
        "    response = requests.get(url)\n"
        "    return response.text\n"
    )
    record = {
        "source": "requests",
        "target": "aiohttp",
        "files": [
            {
                "path": "pkg/client.py",
                "code_changes": [
                    {
                        "line": "4:15",
                        "source_apis": ["get"],
                        "target_apis": ["ClientSession", "get"],
                        "properties": ["async transformation"],
                    }
                ],
            }
        ],
    }

    context = build_pig_context(
        project=tmp_path,
        source="requests",
        target="aiohttp",
        scopes=["pkg/client.py"],
        pymigbench_data=record,
        pymigbench_examples=[],
    )
    rendered = render_pig_context(context)

    assert context.occurrences
    assert context.slices
    assert any(candidate.api == "ClientSession" for candidate in context.candidates)
    assert "PIG-style migration context" in rendered
    assert "requests.get(url)" in rendered
    assert "get -> ClientSession, get" in rendered


def test_agent_automation_context_includes_edit_map_and_validation(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    module = package / "client.py"
    module.write_text(
        "import requests\n\n"
        "def fetch(url):\n"
        "    return requests.get(url).text\n"
    )
    context = build_pig_context(
        project=tmp_path,
        source="requests",
        target="httpx",
        scopes=[],
        pymigbench_data=None,
        pymigbench_examples=[],
    )

    rendered = _render_agent_automation_context(
        context=context,
        project=tmp_path,
        source="requests",
        target="httpx",
        test_commands=["python -m pytest"],
        discovered_test_commands=["python -m pytest"],
    )

    assert "Agent-owned automated migration plan" in rendered
    assert "pkg/client.py" in rendered
    assert "requests.get(url)" in rendered
    assert "python -m pytest" in rendered


def test_pig_context_discovers_source_imports_case_insensitively(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("from flask import Flask\n\napp = Flask(__name__)\n")

    context = build_pig_context(
        project=tmp_path,
        source="Flask",
        target="Quart",
        scopes=[],
        pymigbench_data=None,
        pymigbench_examples=[],
    )

    assert any(occurrence.qualified_name == "flask.Flask" for occurrence in context.occurrences)


def test_discover_validation_commands_prefers_project_tests(tmp_path):
    (tmp_path / "tests").mkdir()

    assert _discover_validation_commands(tmp_path) == ["python -m pytest"]


def test_verification_fails_when_dependency_still_mentions_source(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "client.py").write_text("import httpx\n")
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")

    report = verify_project_migration(project=tmp_path, source="requests", target="httpx")

    assert not report.passed
    assert any("still mentions 'requests'" in finding for finding in report.dependency_findings)


def test_migration_main_derives_task_and_environment_from_pymigbench_yaml(tmp_path):
    yaml_path = tmp_path / "migration.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "repo": "example/project",
                "commit": "abc123",
                "source": "oldlib",
                "target": "newlib",
                "files": [
                    {
                        "path": "pkg/module.py",
                        "code_changes": [
                            {
                                "line": "1:1",
                                "source_apis": ["oldlib.Client"],
                                "target_apis": ["newlib.Client"],
                            }
                        ],
                    }
                ],
            }
        )
    )
    mock_model = Mock()
    mock_environment = Mock()
    mock_agent = Mock()

    with (
        patch("minisweagent.run.migrate.configure_if_first_time"),
        patch("minisweagent.run.migrate.get_model", return_value=mock_model) as mock_get_model,
        patch("minisweagent.run.migrate.get_environment", return_value=mock_environment) as mock_get_environment,
        patch("minisweagent.run.migrate.get_agent", return_value=mock_agent) as mock_get_agent,
    ):
        result = main(
            project=tmp_path,
            source=None,
            target=None,
            pymigbench_yaml=yaml_path,
            pymigbench_dataset=None,
            examples=0,
            strategy="pig",
            pig_report=None,
            pig_max_candidates=8,
            pig_max_slices=80,
            pig_slice_radius=8,
            pig_introspect_target=False,
            verify_only=False,
            strict_static_check=False,
            strict_report=None,
            auto_repair_attempts=1,
            discover_tests=True,
            scope=[],
            test_command=["pytest"],
            note=[],
            model_name="test-model",
            model_class=None,
            agent_class=None,
            environment_class=None,
            yolo=True,
            cost_limit=0,
            config_spec=[str(builtin_config_dir / "mini.yaml"), str(builtin_config_dir / "migration.yaml")],
            output=None,
            exit_immediately=True,
            print_task=False,
        )

    assert result is mock_agent
    mock_get_model.assert_called_once()
    model_config = mock_get_model.call_args.kwargs["config"]
    assert model_config["model_name"] == "test-model"
    mock_get_environment.assert_called_once()
    environment_config = mock_get_environment.call_args.args[0]
    assert environment_config["cwd"] == str(tmp_path.resolve())
    mock_get_agent.assert_called_once_with(mock_model, mock_environment, mock_get_agent.call_args.args[2], default_type="interactive")
    agent_config = mock_get_agent.call_args.args[2]
    assert agent_config["cost_limit"] == 0
    assert agent_config["confirm_exit"] is False
    task = mock_agent.run.call_args.args[0]
    assert "from `oldlib` to `newlib`" in task
    assert "pkg/module.py" in task
    assert "oldlib.Client -> newlib.Client" in task
    assert "pytest" in task
    assert "PIG-style migration context" in task


def test_mini_extra_lists_migration_command():
    docstring = get_docstring()

    assert "migrate" in docstring
    assert "structured Python library migration" in docstring
