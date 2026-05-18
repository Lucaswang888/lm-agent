from minisweagent.migration.verification import verify_project_migration


def test_legacy_verifier_fails_on_non_py_shebang_source_residue(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hwrt").write_text("#!/usr/bin/env python\nimport argparse\n")

    report = verify_project_migration(project=tmp_path, source="argparse", target="click")

    assert not report.passed
    assert "bin/hwrt" in report.source_residue
    assert report.checker_report is not None


def test_legacy_verifier_runs_l2_for_web_framework_migrations(tmp_path):
    app_dir = tmp_path / "sample_api"
    views_dir = app_dir / "views"
    views_dir.mkdir(parents=True)
    (app_dir / "app.py").write_text(
        "import quart\n"
        "from views import home\n"
        "app = quart.Quart(__name__)\n"
        "app.register_blueprint(home.blueprint)\n"
    )
    (views_dir / "home.py").write_text(
        "import quart\n"
        "blueprint = quart.blueprints.Blueprint(__name__, __name__)\n"
    )

    report = verify_project_migration(project=tmp_path, source="flask", target="quart")

    assert not report.passed
    assert report.checker_report is not None
    assert report.checker_report["summary"]["layer_failed"] == "L2"
