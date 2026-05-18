from minisweagent.migration.checker.models import CheckContext
from minisweagent.migration.checker.pipeline import CheckerPipeline, run_default_pipeline


def test_pipeline_short_circuits_on_l1_blocker(tmp_path):
    (tmp_path / "app.py").write_text("import argparse\n")
    ctx = CheckContext(project=tmp_path, source="argparse", target="click", layers_to_run=("L0", "L1", "L3"))

    report = run_default_pipeline(ctx)

    assert report.summary["layer_failed"] == "L1"
    assert [layer.layer for layer in report.layers] == ["L0", "L1"]
    assert not report.passed


def test_pipeline_report_contains_version_and_next_action(tmp_path):
    (tmp_path / "app.py").write_text("import click\n")
    ctx = CheckContext(project=tmp_path, source="argparse", target="click", layers_to_run=("L0", "L1"))

    report = CheckerPipeline(["L0", "L1"]).run(ctx)

    assert report.verifier_version == "v2.0"
    assert report.escalation["human_review_required"] is False
