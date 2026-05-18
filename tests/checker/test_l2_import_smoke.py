from minisweagent.migration.checker.layers.l2_import_smoke import L2ImportSmokeLayer
from minisweagent.migration.checker.models import CheckContext


def test_l2_imports_script_style_quart_app_entrypoint(tmp_path):
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

    result = L2ImportSmokeLayer().run(CheckContext(project=tmp_path, source="flask", target="quart"))

    assert not result.passed
    assert any(failure.category == "MIN" for failure in result.failures)
    assert any("sample_api/app.py" in str(failure.evidence) for failure in result.failures)
