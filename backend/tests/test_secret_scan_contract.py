"""Exercise the local Gitleaks history mode with a non-secret synthetic canary."""

import shutil
import subprocess
from pathlib import Path


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def test_gitleaks_git_mode_detects_a_synthetic_history_canary(tmp_path: Path) -> None:
    gitleaks = shutil.which("gitleaks")
    assert gitleaks is not None, "gitleaks must be installed for the secret-scan gate"
    repository = tmp_path / "history-canary"
    repository.mkdir()
    config = repository / ".gitleaks.toml"
    history_file = repository / "historical.txt"

    config.write_text(
        """title = \"synthetic history scan\"

[[rules]]
id = \"synthetic-history-canary\"
description = \"non-secret regression marker\"
regex = '''CANARY_HISTORY_SCAN_MARKER_[A-Z0-9]+'''
secretGroup = 0
""",
        encoding="utf-8",
    )
    assert _run(["git", "init"], cwd=repository).returncode == 0
    assert (
        _run(["git", "config", "user.email", "test@local.invalid"], cwd=repository).returncode == 0
    )
    assert _run(["git", "config", "user.name", "Secret Scan Test"], cwd=repository).returncode == 0
    history_file.write_text("CANARY_HISTORY_SCAN_MARKER_A1\n", encoding="ascii")
    assert _run(["git", "add", "."], cwd=repository).returncode == 0
    assert _run(["git", "commit", "-m", "add canary"], cwd=repository).returncode == 0
    history_file.write_text("removed from working tree\n", encoding="ascii")
    assert _run(["git", "add", "historical.txt"], cwd=repository).returncode == 0
    assert _run(["git", "commit", "-m", "remove canary"], cwd=repository).returncode == 0

    result = _run(
        [
            gitleaks,
            "git",
            "--no-banner",
            "--redact",
            "--exit-code",
            "1",
            "--config",
            str(config),
            "--log-opts=--all",
            str(repository),
        ],
        cwd=repository,
    )

    assert result.returncode == 1
