import ast
from pathlib import Path


def test_domain_source_contains_no_float_literals() -> None:
    source_root = Path(__file__).resolve().parents[1] / "app" / "domain"
    violations: list[str] = []

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                violations.append(f"{path.name}:{node.lineno}")

    assert violations == []
