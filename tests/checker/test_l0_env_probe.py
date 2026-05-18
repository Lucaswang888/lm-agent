from minisweagent.migration.checker.layers.l0_env_probe import L0EnvProbeLayer
from minisweagent.migration.checker.models import CheckContext


def test_l0_includes_python_shebang_scripts(tmp_path):
    script_dir = tmp_path / "bin"
    script_dir.mkdir()
    script = script_dir / "hwrt"
    script.write_text("#!/usr/bin/env python\nimport argparse\n")

    result = L0EnvProbeLayer().run(CheckContext(project=tmp_path, source="argparse", target="click"))

    assert "bin/hwrt" in result.extra["inspected_files"]


def test_l0_includes_pyproject_console_script_module(tmp_path):
    package = tmp_path / "hwrt"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("def main():\n    pass\n")
    (tmp_path / "pyproject.toml").write_text('[project.scripts]\nhwrt = "hwrt.cli:main"\n')

    result = L0EnvProbeLayer().run(CheckContext(project=tmp_path, source="argparse", target="click"))

    assert "hwrt/cli.py" in result.extra["inspected_files"]


def test_l0_reports_dependency_source_residue_as_warning(tmp_path):
    (tmp_path / "requirements.txt").write_text("argparse\n")

    result = L0EnvProbeLayer().run(CheckContext(project=tmp_path, source="argparse", target="click"))

    assert result.passed is True
    assert result.failures
    assert result.failures[0].severity == "warning"
