"""The branch gate must use coverage.py branch counts, never combined coverage."""

import json
import subprocess
import sys
from pathlib import Path


def test_branch_coverage_gate_rejects_high_line_coverage_with_low_branch_coverage(
    tmp_path: Path,
) -> None:
    coverage_json = tmp_path / "coverage.json"
    coverage_json.write_text(
        json.dumps(
            {
                "totals": {
                    "covered_lines": 999,
                    "num_statements": 1_000,
                    "covered_branches": 1,
                    "num_branches": 3,
                }
            }
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_branch_coverage.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--coverage-json",
            str(coverage_json),
            "--minimum",
            "65",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "33.33%" in result.stdout
    assert "65.00%" in result.stdout


def test_branch_gate_fails_when_a_required_module_is_below_its_own_threshold(
    tmp_path: Path,
) -> None:
    coverage_json = tmp_path / "coverage.json"
    coverage_json.write_text(
        json.dumps(
            {
                "files": {
                    "app/simulation/intent_ledger.py": {
                        "summary": {"covered_branches": 6, "num_branches": 10}
                    }
                },
                "totals": {"covered_branches": 90, "num_branches": 100},
            }
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_branch_coverage.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--coverage-json",
            str(coverage_json),
            "--minimum",
            "65",
            "--module",
            "app/simulation/intent_ledger.py=70",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "app/simulation/intent_ledger.py" in result.stdout
    assert "60.00%" in result.stdout
    assert "70.00%" in result.stdout
