from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.volunteer.verification_check_run import _verify_acceptance_evidence


@pytest.fixture
def mock_issue_pr_client():
    with patch("bernstein.core.orchestration.issue_to_pr.IssuePRClient") as mock_client_class:
        mock_instance = MagicMock()
        mock_client_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_build_report():
    with patch("importlib.util.spec_from_file_location") as mock_spec:
        mock_module = MagicMock()
        mock_spec.return_value.loader.exec_module = MagicMock()
        mock_spec.return_value.loader = MagicMock()

        with patch("importlib.util.module_from_spec", return_value=mock_module):
            mock_report = MagicMock()
            mock_report.collected = {
                "tests/unit/test_a.py": set(["test_a"]),
                "tests/unit/test_b.py": set(["test_b"]),
            }
            mock_module.build_report = MagicMock(return_value=mock_report)
            yield mock_module


@pytest.fixture
def mock_workspace(tmp_path):
    # Create dummy script so that exists() is True
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "check_test_collection.py").touch()
    return str(tmp_path)


def test_complete_mapping_passes(mock_issue_pr_client, mock_build_report, mock_workspace):
    mock_issue_pr_client.get_issue.return_value = {
        "body": "## Acceptance criteria\n- [ ] Criterion A\n- [ ] Criterion B"
    }

    pr_body = """
Closes #123
<!-- bernstein:acceptance-evidence -->
- Criterion A → tests/unit/test_a.py::test_a
- Criterion B → tests/unit/test_b.py::test_b
"""

    result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)
    assert result.status == "pass"
    assert result.mapping == {
        "Criterion A": "tests/unit/test_a.py::test_a",
        "Criterion B": "tests/unit/test_b.py::test_b",
    }
    assert result.unproven == []
    assert result.invalid_tests == []


def test_mapping_skips_criterion_fails(mock_issue_pr_client, mock_build_report, mock_workspace):
    mock_issue_pr_client.get_issue.return_value = {
        "body": "## Acceptance criteria\n- [ ] Criterion A\n- [ ] Criterion B"
    }

    pr_body = """
Closes #123
<!-- bernstein:acceptance-evidence -->
- Criterion A → tests/unit/test_a.py::test_a
"""

    result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)
    assert result.status == "fail"
    assert "Criterion B" in result.unproven
    assert "Criterion B" not in result.mapping


def test_mapping_names_nonexistent_test_fails(mock_issue_pr_client, mock_build_report, mock_workspace):
    mock_issue_pr_client.get_issue.return_value = {"body": "## Acceptance criteria\n- [ ] Criterion A"}

    pr_body = """
Closes #123
<!-- bernstein:acceptance-evidence -->
- Criterion A → tests/unit/definitely_not_a_real_test.py::test_fake
"""

    result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)
    assert result.status == "fail"
    assert "Criterion A" in result.unproven
    assert "tests/unit/definitely_not_a_real_test.py::test_fake" in result.invalid_tests


def test_issue_without_acceptance_checklist_is_neutral(mock_issue_pr_client, mock_build_report, mock_workspace):
    mock_issue_pr_client.get_issue.return_value = {"body": "## Description\nFix a typo."}

    pr_body = "Closes #123"

    result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)
    assert result.status == "neutral"


def test_no_linked_issue_is_neutral(mock_issue_pr_client, mock_workspace):
    pr_body = "Just a normal PR"
    result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)
    assert result.status == "neutral"


def test_missing_collector_script_is_neutral(mock_issue_pr_client, tmp_path):
    mock_issue_pr_client.get_issue.return_value = {"body": "## Acceptance criteria\n- [ ] Criterion A"}
    pr_body = "Closes #123\n<!-- bernstein:acceptance-evidence -->\n- Criterion A → tests/unit/test_a.py::test_a"

    result = _verify_acceptance_evidence(pr_body, "owner/repo", str(tmp_path))
    assert result.status == "neutral"
    assert "Test collection script is missing" in result.reason


def test_failing_collector_script_is_neutral(mock_issue_pr_client, mock_workspace):
    mock_issue_pr_client.get_issue.return_value = {"body": "## Acceptance criteria\n- [ ] Criterion A"}
    pr_body = "Closes #123\n<!-- bernstein:acceptance-evidence -->\n- Criterion A → tests/unit/test_a.py::test_a"

    from unittest.mock import patch

    with patch("importlib.util.spec_from_file_location") as mock_spec:
        mock_spec.return_value.loader.exec_module.side_effect = SyntaxError("bad syntax")
        result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)

    assert result.status == "neutral"
    assert "Failed to execute test collection script" in result.reason


def test_node_id_verification_fails_if_wrong_node(mock_issue_pr_client, mock_build_report, mock_workspace):
    mock_issue_pr_client.get_issue.return_value = {"body": "## Acceptance criteria\n- [ ] Criterion A"}

    pr_body = """
Closes #123
<!-- bernstein:acceptance-evidence -->
- Criterion A → tests/unit/test_a.py::test_does_not_exist
"""
    result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)
    assert result.status == "fail"
    assert "Criterion A" in result.unproven
    assert "tests/unit/test_a.py::test_does_not_exist" in result.invalid_tests


def test_criterion_normalization_passes_despite_differences(mock_issue_pr_client, mock_build_report, mock_workspace):
    mock_issue_pr_client.get_issue.return_value = {
        "body": "## Acceptance criteria\n- [ ]   Handle user login   .\n- [ ] Show   error  MESSAGE!"
    }

    pr_body = """
Closes #123
<!-- bernstein:acceptance-evidence -->
- Handle User Login → tests/unit/test_a.py::test_a
- show error message → tests/unit/test_b.py::test_b
"""

    result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)
    assert result.status == "pass"
    # mapping contains the original strings from the PR mapping block
    assert result.mapping == {
        "Handle User Login": "tests/unit/test_a.py::test_a",
        "show error message": "tests/unit/test_b.py::test_b",
    }
    assert result.unproven == []


def test_github_api_failure_is_neutral(mock_issue_pr_client, mock_workspace):
    mock_issue_pr_client.get_issue.side_effect = Exception("API rate limit exceeded")

    pr_body = "Closes #123\n<!-- bernstein:acceptance-evidence -->\n- Criterion A → tests/unit/test_a.py::test_a"

    result = _verify_acceptance_evidence(pr_body, "owner/repo", mock_workspace)
    assert result.status == "neutral"
    assert "Failed to fetch linked issue #123" in result.reason
