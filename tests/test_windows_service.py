"""Tests for the Windows service wrapper's startup behavior."""

import ast
from pathlib import Path

SERVICE_FILE = (
    Path(__file__).parents[1] / "src" / "wisp" / "daemon" / "windows_service.py"
)


def test_daemon_imports_are_deferred_until_after_service_running() -> None:
    """The SCM must receive SERVICE_RUNNING before daemon imports execute."""
    tree = ast.parse(SERVICE_FILE.read_text(encoding="utf-8"))
    service_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WispService"
    )
    svc_run = next(
        node
        for node in service_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "SvcDoRun"
    )
    import_indexes = [
        index
        for index, node in enumerate(svc_run.body)
        if isinstance(node, ast.ImportFrom)
        and node.module in {"wisp.daemon.server", "wisp.daemon.transport"}
    ]

    assert import_indexes
    assert not any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "ReportServiceStatus"
        for node in svc_run.body
    )


def test_service_module_compiles_with_deferred_transport_type() -> None:
    """The service host must load before the transport module is imported."""
    compile(SERVICE_FILE.read_text(encoding="utf-8"), str(SERVICE_FILE), "exec")
