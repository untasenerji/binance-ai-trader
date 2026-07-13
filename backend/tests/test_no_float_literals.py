import ast
from pathlib import Path

FINANCIAL_PACKAGES = (
    "domain",
    "planning",
    "strategy",
    "simulation",
    "exchange",
    "persistence",
)


def test_financial_source_contains_no_float_literals() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    violations: list[str] = []

    for package in FINANCIAL_PACKAGES:
        for path in (app_root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    violations.append(f"{path.relative_to(app_root)}:{node.lineno}")

    assert violations == []
