"""Static regression checks for the Phase 14 fail-closed architecture boundary."""

import ast
from pathlib import Path

APP_ROOT = Path(__file__).parents[1] / "app"
ROUTES_ROOT = APP_ROOT / "api" / "routes"
EXCHANGE_ROOT = APP_ROOT / "exchange"
FORBIDDEN_FIELD_TOKENS = frozenset(
    {
        "apikey",
        "apisecret",
        "authorization",
        "credential",
        "password",
        "passphrase",
        "secret",
        "token",
    }
)
FORBIDDEN_ROUTE_TOKENS = ("auth", "order", "trade")
FORBIDDEN_EXCHANGE_IMPORTS = frozenset(
    {"aiohttp", "http", "httpcore", "httpx", "requests", "socket", "urllib", "websockets"}
)
TRANSPORT_PROTOCOL_NAMES = frozenset({"ExchangeTransport", "RequestSigner"})


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _field_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id.replace("_", "").lower())
    return names


def _exchange_architecture_violations(root: Path) -> tuple[list[str], list[str]]:
    implementations: list[str] = []
    network_imports: list[str] = []

    for path in sorted(root.rglob("*.py")):
        tree = _module_tree(path)
        relative_path = path.relative_to(root).as_posix()
        transport_aliases = set(TRANSPORT_PROTOCOL_NAMES)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module is not None
                    and node.module.split(".")[0] in FORBIDDEN_EXCHANGE_IMPORTS
                ):
                    network_imports.append(f"{relative_path}:{node.module}")
                for alias in node.names:
                    if alias.name in TRANSPORT_PROTOCOL_NAMES:
                        transport_aliases.add(alias.asname or alias.name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in FORBIDDEN_EXCHANGE_IMPORTS:
                        network_imports.append(f"{relative_path}:{alias.name}")
            if isinstance(node, ast.ClassDef):
                base_names = {_base_name(base) for base in node.bases}
                if transport_aliases & base_names:
                    implementations.append(f"{relative_path}:{node.name}")

    return implementations, network_imports


def test_api_routes_have_no_auth_or_order_mutation_surface_or_credential_fields() -> None:
    route_paths: list[str] = []
    field_names: set[str] = set()
    non_read_decorators: list[str] = []

    for path in ROUTES_ROOT.glob("*.py"):
        tree = _module_tree(path)
        field_names.update(_field_names(tree))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"get", "post", "put", "patch", "delete", "websocket"}:
                continue
            if node.func.attr not in {"get", "websocket"}:
                non_read_decorators.append(f"{path.name}:{node.func.attr}")
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                route_paths.append(node.args[0].value.lower())

    assert non_read_decorators == []
    assert all(token not in path for path in route_paths for token in FORBIDDEN_ROUTE_TOKENS)
    assert FORBIDDEN_FIELD_TOKENS.isdisjoint(field_names)


def test_exchange_package_has_only_protocol_boundaries_and_no_network_transport() -> None:
    implementations, network_imports = _exchange_architecture_violations(EXCHANGE_ROOT)

    assert implementations == []
    assert network_imports == []


def test_exchange_architecture_scan_rejects_nested_transport_aliases(tmp_path: Path) -> None:
    nested_package = tmp_path / "exchange" / "nested"
    nested_package.mkdir(parents=True)
    (nested_package / "bypass.py").write_text(
        "from app.exchange.contracts import ExchangeTransport as Transport\n"
        "import socket\n"
        "class Bypass(Transport):\n"
        "    pass\n",
        encoding="utf-8",
    )

    implementations, network_imports = _exchange_architecture_violations(tmp_path / "exchange")

    assert implementations == ["nested/bypass.py:Bypass"]
    assert network_imports == ["nested/bypass.py:socket"]
