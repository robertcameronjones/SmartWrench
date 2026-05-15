"""Tests for the dedicated variable-audit module.

Per ADR 0004 there is no separate "dashboard defaults" set; the Case
fixture is the single source of truth for runtime variable values, so
the audit takes only ``case_keys``.
"""

from __future__ import annotations

from pathlib import Path

from guidepoint.agent import (
    AuditIssue,
    AuditReport,
    ConfigPaths,
    audit_files,
    audit_prompt_variables,
)
from guidepoint.case import Case
from tests.agent._helpers import minimal_agent_config, seed


class TestAuditPromptVariables:
    def test_clean_run_returns_no_issues(self) -> None:
        report = audit_prompt_variables(
            prompt_body="Hi {{a}}",
            case_keys=frozenset({"a"}),
        )
        assert report.ok
        assert report.issues == ()

    def test_unresolved_placeholder_is_an_error(self) -> None:
        report = audit_prompt_variables(
            prompt_body="Hi {{ghost}}",
            case_keys=frozenset(),
        )
        assert not report.ok
        assert len(report.errors) == 1
        assert "ghost" in report.errors[0].message
        assert report.errors[0].level == "error"

    def test_unused_case_key_is_a_warning(self) -> None:
        report = audit_prompt_variables(
            prompt_body="Hi {{a}}",
            case_keys=frozenset({"a", "unused_field"}),
        )
        assert report.ok
        assert len(report.warnings) == 1
        assert "unused_field" in report.warnings[0].message
        assert report.warnings[0].level == "warning"

    def test_case_key_satisfies_placeholder(self) -> None:
        report = audit_prompt_variables(
            prompt_body="Hi {{customer_first_name}}",
            case_keys=frozenset({"customer_first_name"}),
        )
        assert report.ok

    def test_issues_are_sorted_deterministically(self) -> None:
        report = audit_prompt_variables(
            prompt_body="{{z}} {{a}}",
            case_keys=frozenset(),
        )
        messages = [i.message for i in report.errors]
        assert messages == sorted(messages)

    def test_str_repr_includes_level(self) -> None:
        issue = AuditIssue(level="error", message="boom")
        assert str(issue) == "[error] boom"


class TestAuditReport:
    def test_errors_and_warnings_split(self) -> None:
        report = AuditReport(
            issues=(
                AuditIssue(level="error", message="e1"),
                AuditIssue(level="warning", message="w1"),
                AuditIssue(level="error", message="e2"),
            )
        )
        assert len(report.errors) == 2
        assert len(report.warnings) == 1
        assert not report.ok

    def test_warnings_only_is_still_ok(self) -> None:
        report = AuditReport(
            issues=(AuditIssue(level="warning", message="w1"),),
        )
        assert report.ok


class TestCaseVariableKeys:
    def test_includes_expected_user_facing_keys(self) -> None:
        keys = Case.variable_keys()
        for expected in (
            "case_id",
            "customer_first_name",
            "vehicle_year",
            "slot_options",
        ):
            assert expected in keys

    def test_key_set_is_stable(self) -> None:
        assert Case.variable_keys() == Case.variable_keys()


class TestAuditFiles:
    def test_clean_config_has_no_errors(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        seed(
            paths=paths,
            config=minimal_agent_config(),
            prompt="Hi {{customer_first_name}}.",
            tools=(),
        )
        report = audit_files(paths=paths)
        assert report.ok
        assert report.errors == ()

    def test_unresolved_variable_surfaces_as_error(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        seed(
            paths=paths,
            config=minimal_agent_config(),
            prompt="Hi {{nope}}.",
            tools=(),
        )
        report = audit_files(paths=paths)
        assert not report.ok
        assert any("nope" in e.message for e in report.errors)

    def test_unused_case_keys_surface_as_warnings(self, tmp_path: Path) -> None:
        paths = ConfigPaths.for_root(tmp_path)
        seed(
            paths=paths,
            config=minimal_agent_config(),
            prompt="Hi {{customer_first_name}}.",
            tools=(),
        )
        report = audit_files(paths=paths)
        assert report.ok
        assert any("vehicle_make" in w.message for w in report.warnings)

    def test_case_keys_come_from_schema_not_fixture(self, tmp_path: Path) -> None:
        """``audit_files`` should not depend on any fixture file existing."""
        paths = ConfigPaths.for_root(tmp_path)
        seed(paths=paths, config=minimal_agent_config(), prompt="ok", tools=())
        audit_files(paths=paths)
