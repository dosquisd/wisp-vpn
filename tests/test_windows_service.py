"""Tests for the Windows service wrapper's startup behavior."""

import ast
import builtins
import json
import os
import runpy
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

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
        for index, node in enumerate(ast.walk(svc_run))
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows service executable")
@pytest.mark.parametrize("location", ["root", "Scripts"])
def test_native_service_host_resolves_virtual_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    """Reproduce the old prefix error and verify the native host's new layout."""
    import pywintypes
    import win32service

    from wisp.daemon import install as daemon_install

    venv_dir = tmp_path / "service venv"
    venv.EnvBuilder(with_pip=False).create(venv_dir)
    packages = venv_dir / "Lib" / "site-packages"
    host_dir = packages / "win32"
    host_dir.mkdir()
    installed_venv = Path(win32service.__file__).parents[3]
    # pywin32's installer may have moved its host out of site-packages.
    host_source = next(
        candidate
        for candidate in (
            Path(win32service.__file__).with_name("pythonservice.exe"),
            installed_venv / "Scripts" / "pythonservice.exe",
            installed_venv / "pythonservice.exe",
        )
        if candidate.is_file()
    )
    shutil.copy2(host_source, host_dir)
    shutil.copytree(Path(pywintypes.__file__).parent, packages / "pywin32_system32")
    (packages / "wisp_venv_probe.py").write_text("", encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(
            daemon_install.sys, "executable", str(venv_dir / "Scripts" / "python.exe")
        )
        assert daemon_install._prepare_windows_service_runtime()

    host = venv_dir / "Scripts" / "pythonservice.exe"
    if location == "root":
        for source in (venv_dir / "Scripts").iterdir():
            if source.suffix == ".dll" or source.name == "pythonservice.exe":
                shutil.copy2(source, venv_dir / source.name)
        host = venv_dir / "pythonservice.exe"

    # Exit during site initialization, before pywin32 touches the SCM or logs.
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    report = tmp_path / "bootstrap.json"
    (probe_dir / "sitecustomize.py").write_text(
        "import importlib.util, json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['WISP_BOOTSTRAP_REPORT']).write_text(json.dumps({\n"
        "    'prefix': sys.prefix,\n"
        "    'package_found': importlib.util.find_spec('wisp_venv_probe') is not None\n"
        "}), encoding='utf-8')\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    environment = {**os.environ, "PYTHONPATH": str(probe_dir), "PYTHONNOUSERSITE": "1"}
    environment.pop("PYTHONHOME", None)
    environment["WISP_BOOTSTRAP_REPORT"] = str(report)
    result = subprocess.run(
        [str(host)], env=environment, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(report.read_text(encoding="utf-8"))
    if location == "Scripts":
        assert Path(data["prefix"]) == venv_dir
        assert data["package_found"]
    else:
        assert Path(data["prefix"]) != venv_dir
        assert not data["package_found"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows service registration")
def test_service_registration_uses_importable_class_and_scripts_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register a package-qualified class against the prepared service host."""
    import win32serviceutil

    registrations = []
    monkeypatch.setenv("WISP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Scripts" / "python.exe"))
    monkeypatch.setattr(
        win32serviceutil,
        "HandleCommandLine",
        lambda cls, serviceClassString: registrations.append((cls, serviceClassString)),
    )
    runpy.run_path(str(SERVICE_FILE), run_name="__main__")

    service_class, class_string = registrations[0]
    assert class_string == "wisp.daemon.windows_service.WispService"
    assert Path(service_class._exe_name_) == tmp_path / "Scripts" / "pythonservice.exe"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows service wrapper")
@pytest.mark.parametrize("failure_at", ["import", "transport", "serve"])
def test_service_failure_preserves_traceback_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    """Preserve failures before and after event-loop creation without using SCM."""
    import servicemanager

    from wisp.daemon import server, transport

    events = []
    monkeypatch.setenv("WISP_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(servicemanager, "LogInfoMsg", lambda message: None)
    monkeypatch.setattr(servicemanager, "LogErrorMsg", events.append)
    module = runpy.run_path(str(SERVICE_FILE))
    cls = module["WispService"]
    service = cls.__new__(cls)
    service._loop = service._main_task = service._transport = None

    if failure_at == "import":
        original_import = builtins.__import__

        def fail_import(name, *args, **kwargs):
            if name == "wisp.daemon.server":
                raise ImportError("injected startup failure")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_import)
    elif failure_at == "transport":

        def fail_transport():
            raise RuntimeError("injected startup failure")

        monkeypatch.setattr(transport, "WindowsNamedPipeTransport", fail_transport)
    else:

        async def fail_main(transport):
            raise RuntimeError("injected startup failure")

        monkeypatch.setattr(server, "main", fail_main)

    with pytest.raises((ImportError, RuntimeError), match="injected startup failure"):
        service.SvcDoRun()

    assert service._loop is None
    assert service._transport is None
    assert service._main_task is None
    log = (tmp_path / "service-startup.log").read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in log
    assert "injected startup failure" in log
    assert "injected startup failure" in events[0]
