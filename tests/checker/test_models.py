from minisweagent.migration.checker.models import CheckerReport, CoverageInfo, Failure, LayerResult, PassRecord, SuggestedFix


def test_checker_report_serializes_passed_flag():
    report = CheckerReport(
        verifier_version="v2.0",
        summary={"passed": True},
        layers=(LayerResult(layer="L0", passed=True),),
        failures=(),
        passes=(PassRecord(id="L0.scan", note="ok"),),
        coverage=CoverageInfo(),
        escalation={"human_review_required": False},
    )

    payload = report.to_dict()

    assert payload["passed"] is True
    assert payload["verifier_version"] == "v2.0"
    assert payload["passes"][0]["id"] == "L0.scan"


def test_failure_suggested_fix_round_trip():
    failure = Failure(
        id="L1.MIG.001",
        layer="L1",
        category="MIG",
        severity="blocker",
        file="bin/hwrt",
        line=14,
        suggested_fix=SuggestedFix(kind="replace_source_call", edit_targets=("bin/hwrt:14",)),
    )

    assert failure.suggested_fix is not None
    assert failure.suggested_fix.edit_targets == ("bin/hwrt:14",)
