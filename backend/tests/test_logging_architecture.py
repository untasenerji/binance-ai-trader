"""Keep production logs behind the redacting structured logging boundary."""

import ast
from pathlib import Path

APP_ROOT = Path(__file__).parents[1] / "app"
STRUCTURED_LOG_MODULE = APP_ROOT / "observability" / "logging.py"
LOG_METHODS = frozenset({"critical", "debug", "error", "exception", "info", "log", "warning"})


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_production_code_has_no_unredacted_logging_bypass() -> None:
    violations: list[str] = []

    for path in APP_ROOT.rglob("*.py"):
        tree = _tree(path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                violations.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}:print")
            if path == STRUCTURED_LOG_MODULE:
                continue
            if isinstance(node, ast.Import):
                if any(alias.name == "logging" for alias in node.names):
                    violations.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}:logging-import")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in LOG_METHODS:
                    violations.append(
                        f"{path.relative_to(APP_ROOT)}:{node.lineno}:{node.func.attr}"
                    )

    assert violations == []
