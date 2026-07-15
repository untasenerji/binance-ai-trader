import re
from pathlib import Path


def test_backend_ci_installs_gitleaks_before_secret_scan_contract() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    workflow = (repository_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    backend_section = workflow.split("  frontend:", maxsplit=1)[0]

    install_step = "Install Gitleaks CLI for backend secret-scan contract"
    backend_test_step = "Test backend, including PostgreSQL acceptance coverage"

    assert install_step in backend_section
    assert '"${GITLEAKS_DIRECTORY}/gitleaks" version' in backend_section
    assert "sha256sum --check --status" in backend_section
    assert '>> "${GITHUB_PATH}"' in backend_section
    assert backend_section.index(install_step) < backend_section.index(backend_test_step)


def test_all_remote_github_actions_are_pinned_to_full_commit_shas() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    workflow = (repository_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    action_references = [
        line.split("uses:", maxsplit=1)[1].strip().split(maxsplit=1)[0]
        for line in workflow.splitlines()
        if "uses:" in line
    ]

    assert action_references
    assert all(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", reference)
        for reference in action_references
    )
