from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from helpers import LiveServer, wait_for

from database import TaskStatus
from deploy import build_worker_package as bwp

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "T0ken_for-the-tests_0123456789abcdefghijklmnopqrstu"
PACKAGE_FILES = {
    "worker_agent.py", "worker_config.json", "install.ps1", "install.bat", "uninstall.ps1", "uninstall.bat",
    "run.bat", "README.txt",
}
POWERSHELL = shutil.which("powershell") if os.name == "nt" else None
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is needed")


@pytest.fixture
def pc1_config(tmp_path) -> Path:
    """A PC1 settings file with an API token (never the project's real one)."""
    path = tmp_path / "pc1" / "config.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"server": {"host": "0.0.0.0", "port": 8123}, "ui": {"product": "HST"}, "tasks": {"api_token": TOKEN}}),
        encoding="utf-8",
    )
    return path


def build(pc1_config: Path, tmp_path: Path, *extra: str, server: str = "http://pc1.example.com:8123") -> tuple[int, Path]:
    out = tmp_path / "dist"
    code = bwp.main(["--config", str(pc1_config), "--out", str(out), "--server", server, *extra])
    return code, out / bwp.PACKAGE


def package_settings(folder: Path) -> dict:
    return json.loads((folder / "worker_config.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------ the package
def test_the_package_has_everything_pc2_needs_and_nothing_of_pc1(pc1_config, tmp_path):
    code, folder = build(pc1_config, tmp_path, "--allowed-root", r"M:\pcs", "--allowed-root", r"\\fileserver\recipes")
    assert code == 0
    assert {path.name for path in folder.iterdir()} == PACKAGE_FILES

    settings = package_settings(folder)
    assert settings["server_url"] == "http://pc1.example.com:8123"
    assert settings["api_token"] == TOKEN
    assert settings["allowed_roots"] == [r"M:\pcs", r"\\fileserver\recipes"]
    template = json.loads((ROOT / "worker" / "worker_config.json").read_text(encoding="utf-8"))
    for key in template.keys() - {"server_url", "api_token", "allowed_roots"}:
        assert settings[key] == template[key]  # the timings and the rest are the shipped ones

    assert (folder / "worker_agent.py").read_bytes() == (ROOT / "worker" / "worker_agent.py").read_bytes()
    for path in folder.iterdir():  # the token is in the settings file and nowhere else
        if path.name != "worker_config.json":
            assert TOKEN.encode() not in path.read_bytes(), path.name
    assert not (folder / "config.json").exists()  # PC1's own settings never travel to PC2


def test_the_scripts_are_plain_ascii_with_windows_line_endings(pc1_config, tmp_path):
    _, folder = build(pc1_config, tmp_path)
    for name in PACKAGE_FILES - {"worker_agent.py"}:
        data = (folder / name).read_bytes()
        data.decode("ascii")  # (Windows PowerShell 5.1 reads a script without a BOM as ANSI)
        assert b"\n" in data and data.count(b"\n") == data.count(b"\r\n"), f"{name} has a bare LF"


def test_the_readme_names_pc1(pc1_config, tmp_path):
    _, folder = build(pc1_config, tmp_path)
    readme = (folder / "README.txt").read_text(encoding="ascii")
    assert "http://pc1.example.com:8123/workers" in readme and "@SERVER_URL@" not in readme
    for path in folder.iterdir():
        assert b"@SERVER_URL@" not in path.read_bytes(), path.name


def test_the_zip_is_the_folder(pc1_config, tmp_path):
    _, folder = build(pc1_config, tmp_path)
    with zipfile.ZipFile(tmp_path / "dist" / "PCS1Stop_worker.zip") as bundle:
        assert bundle.testzip() is None
        names = bundle.namelist()
        assert {name.split("/", 1)[0] for name in names} == {"PCS1Stop_worker"}  # unzips into one folder
        assert {name.split("/", 1)[1] for name in names} == PACKAGE_FILES
        for name in names:
            assert bundle.read(name) == (folder / name.split("/", 1)[1]).read_bytes()


def test_a_rebuild_replaces_the_old_package(pc1_config, tmp_path):
    _, folder = build(pc1_config, tmp_path)
    (folder / "old_leftover.txt").write_text("from an earlier build")
    code, folder = build(pc1_config, tmp_path, server="http://other-pc1:9000")
    assert code == 0 and {path.name for path in folder.iterdir()} == PACKAGE_FILES
    assert package_settings(folder)["server_url"] == "http://other-pc1:9000"
    with zipfile.ZipFile(tmp_path / "dist" / "PCS1Stop_worker.zip") as bundle:
        assert len(bundle.namelist()) == len(PACKAGE_FILES)


def test_building_does_not_touch_pc1s_settings(pc1_config, tmp_path):
    before = pc1_config.read_bytes()
    build(pc1_config, tmp_path)
    assert pc1_config.read_bytes() == before


# ------------------------------------------------------------- a rebuild never leaves a half-made package behind
def snapshot(dist: Path) -> dict[str, bytes]:
    """Every file below ``dist`` with its content."""
    return {str(path.relative_to(dist)): path.read_bytes() for path in sorted(dist.rglob("*")) if path.is_file()}


def test_nothing_but_the_package_is_left_in_the_output_folder(pc1_config, tmp_path):
    build(pc1_config, tmp_path)
    build(pc1_config, tmp_path)
    assert sorted(path.name for path in (tmp_path / "dist").iterdir()) == ["PCS1Stop_worker", "PCS1Stop_worker.zip"]


def test_a_build_that_fails_halfway_keeps_the_old_package(pc1_config, tmp_path, monkeypatch, capsys):
    build(pc1_config, tmp_path)
    dist = tmp_path / "dist"
    before = snapshot(dist)

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(bwp.zipfile, "ZipFile", disk_full)
        code, _ = build(pc1_config, tmp_path, server="http://other-pc1:9000")

    assert code == 2 and "No space left on device" in capsys.readouterr().err
    assert snapshot(dist) == before  # the old folder and zip as they were, and no staging folder left over
    assert {path.name for path in dist.iterdir()} == {"PCS1Stop_worker", "PCS1Stop_worker.zip"}


def test_a_failure_while_the_new_package_is_moved_in_puts_the_old_one_back(pc1_config, tmp_path, monkeypatch, capsys):
    build(pc1_config, tmp_path)
    dist = tmp_path / "dist"
    before = snapshot(dist)
    real_replace, moves = os.replace, []

    def last_move_fails(source, target):
        moves.append(source)
        if len(moves) == 4:  # old folder away, old zip away, new folder in, NEW ZIP IN <- fails
            raise OSError(28, "No space left on device")
        return real_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(bwp.os, "replace", last_move_fails)
        code, _ = build(pc1_config, tmp_path, server="http://other-pc1:9000")

    assert code == 2 and "The old package is untouched" in capsys.readouterr().err
    assert len(moves) >= 4 + 3  # (and the three moves before it were taken back)
    assert snapshot(dist) == before
    assert {path.name for path in dist.iterdir()} == {"PCS1Stop_worker", "PCS1Stop_worker.zip"}


def test_a_package_folder_that_is_in_use_is_reported_and_left_alone(pc1_config, tmp_path, monkeypatch, capsys):
    build(pc1_config, tmp_path)
    dist = tmp_path / "dist"
    before = snapshot(dist)

    def in_use(source, target):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    with monkeypatch.context() as patch:
        patch.setattr(bwp.os, "replace", in_use)
        patch.setattr(bwp.time, "sleep", lambda _seconds: None)
        code, _ = build(pc1_config, tmp_path, "--rotate-token", server="http://other-pc1:9000")

    err = capsys.readouterr().err
    assert code == 2 and "still using" in err and "untouched" in err
    assert snapshot(dist) == before
    assert json.loads(pc1_config.read_text(encoding="utf-8"))["tasks"]["api_token"] == TOKEN  # not rotated either


def test_a_folder_that_this_script_did_not_make_is_never_replaced(pc1_config, tmp_path, capsys):
    foreign = tmp_path / "dist" / bwp.PACKAGE
    foreign.mkdir(parents=True)
    (foreign / "precious.txt").write_text("mine")

    code, _ = build(pc1_config, tmp_path)

    assert code == 2 and "was not made by this script" in capsys.readouterr().err
    assert (foreign / "precious.txt").read_text() == "mine"
    assert [path.name for path in (tmp_path / "dist").iterdir()] == [bwp.PACKAGE]  # no zip, no staging folder


def test_an_empty_folder_of_that_name_is_just_replaced(pc1_config, tmp_path):
    (tmp_path / "dist" / bwp.PACKAGE).mkdir(parents=True)
    code, folder = build(pc1_config, tmp_path)
    assert code == 0 and {path.name for path in folder.iterdir()} == PACKAGE_FILES


def test_a_token_that_cannot_be_saved_is_said_so_after_the_build(pc1_config, tmp_path, monkeypatch, capsys):
    def cannot_save(self, **values):
        raise OSError(13, "config.json is read-only")

    monkeypatch.setattr(bwp.Settings, "update", cannot_save)
    code, folder = build(pc1_config, tmp_path, "--rotate-token")
    err = capsys.readouterr().err
    assert code == 2 and "NEW token" in err and "--rotate-token" in err and "read-only" in err
    assert json.loads(pc1_config.read_text(encoding="utf-8"))["tasks"]["api_token"] == TOKEN


# -------------------------------------------------------------------------------------------- the token
def test_rotate_token_makes_a_new_token_for_pc1_and_the_package(pc1_config, tmp_path):
    code, folder = build(pc1_config, tmp_path, "--rotate-token")
    assert code == 0
    first = package_settings(folder)["api_token"]
    saved = json.loads(pc1_config.read_text(encoding="utf-8"))
    assert first != TOKEN and len(first) == 43 and first.isascii()
    assert saved["tasks"]["api_token"] == first  # PC1 and PC2 hold the same one
    assert saved["server"]["host"] == "0.0.0.0" and saved["server"]["port"] == 8123  # nothing else moved
    assert saved["ui"]["product"] == "HST"

    build(pc1_config, tmp_path, "--rotate-token")
    second = package_settings(folder)["api_token"]
    assert second not in (first, TOKEN)
    assert json.loads(pc1_config.read_text(encoding="utf-8"))["tasks"]["api_token"] == second


def test_the_token_is_not_printed(pc1_config, tmp_path, capsys):
    build(pc1_config, tmp_path)
    out = capsys.readouterr().out
    assert TOKEN not in out and bwp.fingerprint(TOKEN) in out and f"{len(TOKEN)} characters" in out


def test_the_fingerprint_tells_tokens_apart_without_showing_them():
    assert bwp.fingerprint("a") != bwp.fingerprint("b") and len(bwp.fingerprint("a")) == 8
    assert bwp.fingerprint(TOKEN) == hashlib.sha256(TOKEN.encode()).hexdigest()[:8]


def test_without_a_token_nothing_is_built(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"tasks": {"api_token": ""}}))
    code, folder = build(config, tmp_path)
    assert code == 2 and "--rotate-token" in capsys.readouterr().err
    assert not (tmp_path / "dist").exists()


def test_a_failed_build_does_not_lock_the_old_workers_out(pc1_config, tmp_path, capsys):
    code, _ = build(pc1_config, tmp_path, "--rotate-token", "--allowed-root", "  ")
    assert code == 2 and "--allowed-root" in capsys.readouterr().err
    assert json.loads(pc1_config.read_text(encoding="utf-8"))["tasks"]["api_token"] == TOKEN  # not rotated


# ---------------------------------------------------------------------------------- the server address
@pytest.mark.parametrize("server", ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"])
def test_localhost_is_no_address_for_another_pc(pc1_config, tmp_path, capsys, server):
    code, _ = build(pc1_config, tmp_path, server=server)
    assert code == 2 and "cannot reach" in capsys.readouterr().err
    assert build(pc1_config, tmp_path, "--allow-local", server=server)[0] == 0  # (for a test on one PC)


@pytest.mark.parametrize("server", ["pc1:8080", "ftp://pc1", "", "http://"])
def test_a_server_address_must_be_a_url(pc1_config, tmp_path, capsys, server):
    code, _ = build(pc1_config, tmp_path, server=server)
    assert code == 2 and "--server" in capsys.readouterr().err


def test_the_default_address_is_this_pc_and_the_port_of_the_config(pc1_config, tmp_path, monkeypatch):
    """An app that listens on the network and has no public address is reached at its own port."""
    monkeypatch.setattr(bwp.socket, "getfqdn", lambda: "PC1.Corp.Example.COM")
    assert bwp.default_server_url(8123) == "http://pc1.corp.example.com:8123"
    out = tmp_path / "dist"
    assert bwp.main(["--config", str(pc1_config), "--out", str(out)]) == 0
    assert package_settings(out / bwp.PACKAGE)["server_url"] == "http://pc1.corp.example.com:8123"


# ------------------------------------------- an app on this PC only, published by a reverse proxy (IIS)
SITE = "http://ppv-pcs1stop-ss.intel.com"


def proxied_config(tmp_path: Path, **server) -> Path:
    """A PC1 settings file whose app listens on this PC only (port 8007), plus whatever ``server`` adds."""
    path = tmp_path / "pc1" / "config.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"server": {"host": "127.0.0.1", "port": 8007, **server}, "tasks": {"api_token": TOKEN}}),
                    encoding="utf-8")
    return path


def test_the_default_address_is_the_public_url_of_the_config(tmp_path, capsys):
    out = tmp_path / "dist"
    assert bwp.main(["--config", str(proxied_config(tmp_path, public_url=SITE + "/")), "--out", str(out)]) == 0
    folder = out / bwp.PACKAGE
    assert package_settings(folder)["server_url"] == SITE  # the site, not host:8007; no trailing slash
    assert f"{SITE}/workers" in (folder / "README.txt").read_text(encoding="ascii")
    said = capsys.readouterr().out
    assert SITE in said and "reverse proxy" in said
    assert "firewall" not in said.lower()  # nothing to open: the app is reached through the web site


def test_an_app_on_this_pc_only_with_no_public_url_gives_pc2_nothing_to_connect_to(tmp_path, capsys):
    out = tmp_path / "dist"
    assert bwp.main(["--config", str(proxied_config(tmp_path)), "--out", str(out)]) == 2
    assert "server.public_url" in capsys.readouterr().err
    assert not out.exists()  # nothing was built


def test_the_server_option_beats_the_public_url(tmp_path):
    out = tmp_path / "dist"
    config = proxied_config(tmp_path, public_url=SITE)
    assert bwp.main(["--config", str(config), "--out", str(out), "--server", "http://other.example.com:9000"]) == 0
    assert package_settings(out / bwp.PACKAGE)["server_url"] == "http://other.example.com:9000"


def test_a_public_url_that_is_localhost_is_refused_like_any_other_localhost_address(tmp_path, capsys):
    config = proxied_config(tmp_path, public_url="http://localhost")
    assert bwp.main(["--config", str(config), "--out", str(tmp_path / "dist")]) == 2
    assert "cannot reach" in capsys.readouterr().err


def test_the_shipped_config_builds_a_package_that_pc2_can_use(tmp_path):
    """``uv run python deploy/build_worker_package.py`` with the project's own config.json, as it is (only the output
    goes somewhere else)."""
    from config import DEFAULT_CONFIG_PATH, Settings

    shipped = Settings(DEFAULT_CONFIG_PATH)
    out = tmp_path / "dist"
    assert bwp.main(["--config", str(DEFAULT_CONFIG_PATH), "--out", str(out)]) == 0
    settings = package_settings(out / bwp.PACKAGE)
    assert settings["api_token"] == shipped.api_token  # the same token on both sides
    assert settings["server_url"] == (shipped.public_url or bwp.default_server_url(shipped.port))
    assert not settings["server_url"].startswith(("http://127.0.0.1", "http://localhost"))


def test_a_trailing_slash_is_dropped():
    assert bwp.check_server_url(" http://pc1:8080/ ") == "http://pc1:8080"


def test_a_missing_config_is_reported_and_not_created(tmp_path, capsys):
    missing = tmp_path / "config.json"
    assert bwp.main(["--config", str(missing), "--out", str(tmp_path / "dist")]) == 2
    assert "does not exist" in capsys.readouterr().err and not missing.exists()


# -------------------------------------------------------- the packaged agent, as PC2 would run it
@pytest.fixture
def unzipped(pc1_config, tmp_path) -> Path:
    """The zip as PC2 gets it, unzipped somewhere else; PC1 is the test server (see ``pc1``)."""
    code, _ = build(pc1_config, tmp_path, "--allow-local", server="http://127.0.0.1:9")
    assert code == 0
    target = tmp_path / "pc2"
    with zipfile.ZipFile(tmp_path / "dist" / "PCS1Stop_worker.zip") as bundle:
        bundle.extractall(target)
    return target / "PCS1Stop_worker"


def point_at(folder: Path, url: str, **settings) -> None:
    path = folder / "worker_config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(server_url=url, **settings)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def pc1(services):
    services.api_token = TOKEN
    server = LiveServer(services)
    server.start()
    yield server
    server.stop()


def agent_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")  # the Python of the tests
    env["PCS_NOPAUSE"] = "1"
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def test_the_packaged_agent_passes_its_check_with_the_token_of_the_package(unzipped, pc1, services):
    point_at(unzipped, pc1.url)
    run = subprocess.run([sys.executable, "worker_agent.py", "--check"], cwd=unzipped, capture_output=True, text=True,
                         timeout=60, env=agent_env())
    assert run.returncode == 0, run.stdout + run.stderr
    assert "[ ok ] the API token is accepted" in run.stdout and TOKEN not in run.stdout
    assert [worker.hostname for worker in services.workers.list_workers()] == [socket.gethostname()]


def test_the_packaged_agent_is_refused_when_pc1s_token_changed(unzipped, pc1, services):
    point_at(unzipped, pc1.url)
    services.api_token = "another-token-on-pc1"
    run = subprocess.run([sys.executable, "worker_agent.py", "--check"], cwd=unzipped, capture_output=True, text=True,
                         timeout=60, env=agent_env())
    assert run.returncode == 1 and "refused the API token" in run.stdout


def test_the_packaged_agent_does_the_checks(unzipped, pc1, services, tmp_path):
    point_at(unzipped, pc1.url, hostname="PC2-package", poll_interval_s=1)
    target = tmp_path / "SST_PowerCycle.xml"
    target.write_text("<Workflow/>")
    found = services.tasks.create_task("check_path", {"path": str(target), "product": "HST"})
    missing = services.tasks.create_task("check_path", {"path": str(tmp_path / "nope.xml"), "product": "HST"})

    agent = subprocess.Popen([sys.executable, "worker_agent.py"], cwd=unzipped, env=agent_env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: all(services.tasks.get_task(i).status is TaskStatus.COMPLETED for i in (found, missing)),
                 timeout=30, what="the packaged agent to answer")
    finally:
        agent.terminate()
        agent.wait(15)
    assert services.tasks.get_task(found).result["exists"] is True
    assert services.tasks.get_task(missing).result["exists"] is False
    assert {services.tasks.get_task(i).assigned_worker for i in (found, missing)} == {"PC2-package"}
    assert (unzipped / "worker.log").exists()  # it logs next to itself


# ------------------------------------------------------------------------------- the install scripts
def powershell(folder: Path, script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(folder / script), *args],
        cwd=folder, capture_output=True, text=True, timeout=180, env=env or agent_env(), stdin=subprocess.DEVNULL,
    )


def task_exists(name: str) -> bool:
    return subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True).returncode == 0


@needs_powershell
def test_every_powershell_script_parses(unzipped):
    scripts = [unzipped / "install.ps1", unzipped / "uninstall.ps1"]
    command = (
        "$bad = 0; foreach ($f in @(" + ",".join(f"'{s}'" for s in scripts) + ")) { $t = $null; $e = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e); "
        "foreach ($x in $e) { Write-Output (\"$f : $($x.Message)\"); $bad++ } }; exit $bad"
    )
    run = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout


@needs_powershell
def test_install_dry_run_tests_the_connection_and_changes_nothing(unzipped, pc1):
    point_at(unzipped, pc1.url)
    name = f"pcs-test-{os.getpid()}-{int(time.time())}"
    run = powershell(unzipped, "install.ps1", "-DryRun", "-TaskName", name)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "[ ok ] the API token is accepted" in run.stdout
    assert "DRY RUN" in run.stdout and f"Scheduled task '{name}'" in run.stdout and "pythonw.exe" in run.stdout
    assert "at every logon" in run.stdout and "replaces:" not in run.stdout
    assert not task_exists(name)


@needs_powershell
def test_install_stops_when_the_connection_test_fails(unzipped, pc1, services):
    point_at(unzipped, pc1.url)
    services.api_token = "changed-on-pc1"
    name = f"pcs-test-{os.getpid()}-{int(time.time())}-b"
    run = powershell(unzipped, "install.ps1", "-DryRun", "-TaskName", name)
    assert run.returncode == 1
    assert "refused the API token" in run.stdout and "ERROR: The connection test failed" in run.stdout
    assert "DRY RUN" not in run.stdout and not task_exists(name)


@needs_powershell
def test_install_can_skip_the_connection_test(unzipped):
    point_at(unzipped, "http://127.0.0.1:9")  # nobody there
    run = powershell(unzipped, "install.ps1", "-DryRun", "-SkipCheck", "-TaskName", "pcs-test-skip")
    assert run.returncode == 0 and "Skipping the connection test" in run.stdout and "DRY RUN" in run.stdout


@needs_powershell
def test_install_says_when_python_is_missing(unzipped):
    env = agent_env()
    env["PATH"] = os.pathsep.join([r"C:\Windows\System32", r"C:\Windows\System32\WindowsPowerShell\v1.0"])
    run = powershell(unzipped, "install.ps1", "-DryRun", "-SkipCheck", "-TaskName", "pcs-test-nopython", env=env)
    assert run.returncode == 1 and "Python 3.10 or newer was not found" in run.stdout


@needs_powershell
def test_install_refuses_to_run_outside_the_package(unzipped, tmp_path):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    shutil.copy(unzipped / "install.ps1", lonely / "install.ps1")
    run = powershell(lonely, "install.ps1", "-DryRun", "-SkipCheck")
    assert run.returncode == 1 and "worker_agent.py is not in" in run.stdout


@needs_powershell
def test_uninstall_without_an_installation_is_harmless(unzipped):
    run = powershell(unzipped, "uninstall.ps1", "-TaskName", "pcs-test-never-installed")
    assert run.returncode == 0 and "nothing to remove" in run.stdout


# (a throw-away task that never runs: its trigger is ten years away, and it is deleted again at the end of the test)
def register_task(name: str, agent_path: str) -> None:
    script = (
        "$who = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name; "
        f"$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '\"{agent_path}\"'; "
        "$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(10); "
        "$principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited; "
        f"Register-ScheduledTask -TaskName '{name}' -Action $action -Trigger $trigger -Principal $principal "
        "-Description 'pcs test - safe to delete' -Force | Out-Null"
    )
    run = subprocess.run([POWERSHELL, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr


def delete_task(name: str) -> None:
    subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)


def throwaway_name(kind: str) -> str:
    return f"pcs-test-{os.getpid()}-{int(time.time())}-{kind}"


@needs_powershell
def test_uninstall_removes_the_worker_that_was_installed_from_this_folder(unzipped):
    name = throwaway_name("own")
    register_task(name, str(unzipped / "worker_agent.py"))
    try:
        run = powershell(unzipped, "uninstall.ps1", "-TaskName", name)
        assert run.returncode == 0 and "Removed the scheduled task" in run.stdout
        assert not task_exists(name)
    finally:
        delete_task(name)


@needs_powershell
def test_uninstall_leaves_a_worker_that_was_installed_from_another_folder(unzipped):
    """An old copy of the package must not be able to remove the worker of a newer copy."""
    name, elsewhere = throwaway_name("other"), r"C:\somewhere\else\worker_agent.py"
    register_task(name, elsewhere)
    try:
        run = powershell(unzipped, "uninstall.ps1", "-TaskName", name)
        assert run.returncode == 1 and "not installed from this folder" in run.stdout and elsewhere in run.stdout
        assert "-Force" in run.stdout and task_exists(name)

        forced = powershell(unzipped, "uninstall.ps1", "-TaskName", name, "-Force")
        assert forced.returncode == 0 and "Removed the scheduled task" in forced.stdout
        assert not task_exists(name)
    finally:
        delete_task(name)


@needs_powershell
def test_install_says_which_worker_it_replaces(unzipped):
    name, old = throwaway_name("replace"), r"C:\old\package\worker_agent.py"
    register_task(name, old)
    try:
        run = powershell(unzipped, "install.ps1", "-DryRun", "-SkipCheck", "-TaskName", name)
        assert run.returncode == 0 and "replaces:" in run.stdout and old in run.stdout
        assert task_exists(name)  # (a dry run changes nothing)
    finally:
        delete_task(name)


@pytest.mark.skipif(os.name != "nt", reason="batch files are Windows only")
def test_the_batch_launchers(unzipped, pc1):
    point_at(unzipped, pc1.url)
    env = agent_env()
    check = subprocess.run(["cmd", "/c", str(unzipped / "run.bat"), "--check"], cwd=unzipped, capture_output=True,
                           text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "[ ok ] the API token is accepted" in check.stdout

    if POWERSHELL:
        dry = subprocess.run(["cmd", "/c", str(unzipped / "install.bat"), "-DryRun", "-SkipCheck", "-TaskName", "pcs-test-bat"],
                             cwd=unzipped, capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
        assert dry.returncode == 0 and "DRY RUN" in dry.stdout


@pytest.mark.skipif(os.name != "nt", reason="batch files are Windows only")
def test_run_bat_says_when_python_is_missing(unzipped):
    env = agent_env()
    env["PATH"] = os.pathsep.join([r"C:\Windows\System32"])
    run = subprocess.run(["cmd", "/c", str(unzipped / "run.bat")], cwd=unzipped, capture_output=True, text=True, timeout=60,
                         env=env, stdin=subprocess.DEVNULL)
    assert run.returncode == 1 and "Python 3.10 or newer was not found" in run.stdout


def fake_python(folder: Path, *, new_enough: bool) -> None:
    """A ``python.bat`` (the kind a version manager such as pyenv-win puts on the PATH): answers the version probe
    like Python 3.11 or 3.9 would, and says what it was asked to run otherwise."""
    folder.mkdir()
    (folder / "python.bat").write_text(
        "@echo off\r\n"
        f'if "%1"=="-c" exit /b {0 if new_enough else 1}\r\n'
        "echo fake python runs %*\r\n"
        "exit /b 7\r\n"
    )


@pytest.mark.skipif(os.name != "nt", reason="batch files are Windows only")
def test_run_bat_refuses_a_python_that_is_too_old(unzipped, tmp_path):
    fake_python(tmp_path / "py39", new_enough=False)
    env = agent_env()
    env["PATH"] = os.pathsep.join([str(tmp_path / "py39"), r"C:\Windows\System32"])
    run = subprocess.run(["cmd", "/c", str(unzipped / "run.bat")], cwd=unzipped, capture_output=True, text=True, timeout=60,
                         env=env, stdin=subprocess.DEVNULL)
    assert run.returncode == 1 and "Python 3.10 or newer was not found" in run.stdout
    assert "fake python runs" not in run.stdout  # the worker was not started with it


@pytest.mark.skipif(os.name != "nt", reason="batch files are Windows only")
def test_run_bat_uses_a_python_that_is_new_enough_even_when_it_is_a_bat_shim(unzipped, tmp_path):
    fake_python(tmp_path / "py311", new_enough=True)
    env = agent_env()
    env["PATH"] = os.pathsep.join([str(tmp_path / "py311"), r"C:\Windows\System32"])
    run = subprocess.run(["cmd", "/c", str(unzipped / "run.bat"), "--check"], cwd=unzipped, capture_output=True, text=True,
                         timeout=60, env=env, stdin=subprocess.DEVNULL)
    assert "fake python runs worker_agent.py --check" in run.stdout
    assert run.returncode == 7  # the exit code of Python is the exit code of run.bat (control came back to it)
