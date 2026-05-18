from minisweagent.migration.checker.layers.l1_static_ast import L1StaticAstLayer
from minisweagent.migration.checker.models import CheckContext


def test_l1_flags_source_residue_in_non_py_shebang_script(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hwrt").write_text("#!/usr/bin/env python\nimport argparse\n\nargparse.ArgumentParser()\n")

    result = L1StaticAstLayer().run(CheckContext(project=tmp_path, source="argparse", target="click"))

    assert not result.passed
    assert any(failure.file == "bin/hwrt" and failure.category == "MIG" for failure in result.failures)


def test_l1_ignores_source_mentions_in_strings_and_comments(tmp_path):
    (tmp_path / "app.py").write_text('# import argparse\nvalue = "argparse.ArgumentParser"\n')

    result = L1StaticAstLayer().run(CheckContext(project=tmp_path, source="argparse", target="click"))

    assert result.passed
    assert result.extra["source_residue"] == ()


def test_l1_sem_rule_flags_await_request_args_for_quart(tmp_path):
    (tmp_path / "app.py").write_text("from quart import request\n\nasync def route():\n    return await request.args\n")

    result = L1StaticAstLayer().run(CheckContext(project=tmp_path, source="flask", target="quart"))

    assert any(failure.category == "SEM" and failure.file == "app.py" for failure in result.failures)
