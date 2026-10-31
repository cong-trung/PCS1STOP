#!/usr/bin/env python3
"""Builds the package that is copied to PC2: the worker agent, its settings (PC1's address and the API token) and the
scripts that install it.

    uv run python deploy/build_worker_package.py                        # PC1 = this PC, token from config.json
    uv run python deploy/build_worker_package.py --server http://pc1:8080
    uv run python deploy/build_worker_package.py --rotate-token         # new token: saved in config.json, used here
    uv run python deploy/build_worker_package.py --allowed-root "M:\\pcs" --allowed-root "\\\\fileserver\\recipes"

Result: ``dist/PCS1Stop_worker/`` and ``dist/PCS1Stop_worker.zip``.  Copy the zip to PC2, unzip it somewhere permanent
and double-click ``install.bat``.  The package holds PC1's API token - treat it like a password.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from config import ConfigError, Settings  # noqa: E402  (the project's own settings reader)

PACKAGE = "PCS1Stop_worker"
TEMPLATES = HERE / "pc2"
AGENT = ROOT / "worker" / "worker_agent.py"
AGENT_SETTINGS = ROOT / "worker" / "worker_config.json"
#: what the scripts of the package are
SCRIPTS = ("install.ps1", "install.bat", "uninstall.ps1", "uninstall.bat", "run.bat", "README.txt")
#: replaced in the files of the package
PLACEHOLDER_SERVER = "@SERVER_URL@"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class BuildError(Exception):
    """The package cannot be built (the message says why)."""


def default_server_url(port: int) -> str:
    """PC1 as the other PCs know it: the full name of this machine."""
    host = socket.getfqdn() or socket.gethostname()
    return f"http://{host.lower()}:{port}"


def check_server_url(url: str, *, allow_local: bool = False) -> str:
    """``http://host:port`` without a trailing slash; PC2 cannot reach "localhost", so that is refused."""
    text = url.strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BuildError(f"--server must look like http://host:port, got {url!r}")
    if parts.hostname.lower() in LOCAL_HOSTS and not allow_local:
        raise BuildError(
            "PC2 cannot reach 'localhost' - give the name or address of PC1 with --server, e.g. --server http://pc1:8080"
        )
    return text.rstrip("/")


def new_token() -> str:
    """A fresh API token (43 plain characters, safe in an HTTP header)."""
    return secrets.token_urlsafe(32)


def fingerprint(token: str) -> str:
    """Eight characters that tell two tokens apart without showing either."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _crlf(data: bytes) -> bytes:
    """Windows line endings (a .bat file with bare LF can confuse cmd.exe)."""
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _template(name: str, server_url: str) -> bytes:
    path = TEMPLATES / name
    if not path.is_file():
        raise BuildError(f"the template {path} is missing")
    data = path.read_bytes()
    try:
        text = data.decode("ascii")  # Windows PowerShell 5.1 reads a file without BOM as ANSI: keep them plain
    except UnicodeDecodeError as exc:
        raise BuildError(f"{path} must be plain ASCII (position {exc.start})") from exc
    return _crlf(text.replace(PLACEHOLDER_SERVER, server_url).encode("ascii"))


def worker_settings(server_url: str, token: str, allowed_roots: Sequence[str]) -> dict:
    """worker/worker_config.json with PC1's address, the token and the allowed folders filled in."""
    settings = json.loads(AGENT_SETTINGS.read_text(encoding="utf-8-sig"))
    settings.update(server_url=server_url, api_token=token, allowed_roots=list(allowed_roots))
    return settings


def _is_package(folder: Path) -> bool:
    """True for a folder this script made (it holds worker_agent.py) or an empty one: only those may be replaced."""
    return folder.is_dir() and (not any(folder.iterdir()) or (folder / "worker_agent.py").is_file())


def _verify(folder: Path, archive: Path, files: dict[str, bytes]) -> None:
    """What was written is what was meant (a full disk or a virus scanner can leave a damaged file)."""
    for name, data in files.items():
        if (folder / name).read_bytes() != data:
            raise BuildError(f"{folder / name} was not written correctly - is the disk full?")
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None or sorted(bundle.namelist()) != sorted(f"{PACKAGE}/{name}" for name in files):
            raise BuildError(f"{archive} was not written correctly - is the disk full?")


def _swap(staged: tuple[Path, Path], final: tuple[Path, Path], parking: Path) -> None:
    """Move the finished (folder, zip) over the old pair.  Whatever fails, the old pair is back in place afterwards."""
    done: list[tuple[Path, Path]] = []  # the renames made so far (source, destination)

    def move(source: Path, target: Path) -> None:
        for attempt in range(1, 6):  # (a virus scanner can hold a just-written folder for a moment)
            try:
                os.replace(source, target)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.2 * attempt)
        done.append((source, target))

    try:
        for index, old in enumerate(final):
            if old.exists():
                move(old, parking / f"old{index}")
        for new, target in zip(staged, final):
            move(new, target)
    except OSError as exc:
        for source, target in reversed(done):
            try:
                os.replace(target, source)
            except OSError:
                pass  # (nothing more can be done about it)
        raise BuildError(
            f"cannot replace {final[0]}: {exc.strerror or exc} - is a window, Explorer or the worker still using the "
            "old package folder or zip?  The old package is untouched."
        ) from exc


def build_package(
    *, server_url: str, token: str, out_dir: Path, allowed_roots: Sequence[str] = ()
) -> tuple[Path, Path]:
    """Write ``out_dir/PCS1Stop_worker`` and ``out_dir/PCS1Stop_worker.zip``; returns (folder, zip).

    Everything is made in a staging folder first and moved into place only when it is complete and checked, so a
    failure (disk full, a file in use) leaves the previous package as it was.  A ``PCS1Stop_worker`` folder that this
    script did not make is never replaced."""
    if not token or not (token.isascii() and token.isprintable()):
        raise BuildError("the API token is empty or not plain ASCII")
    roots = [root.strip() for root in allowed_roots]
    if any(not root for root in roots):
        raise BuildError("--allowed-root must not be empty")

    files: dict[str, bytes] = {"worker_agent.py": AGENT.read_bytes()}
    files["worker_config.json"] = _crlf(
        (json.dumps(worker_settings(server_url, token, roots), indent=2) + "\n").encode("ascii")
    )
    for name in SCRIPTS:
        files[name] = _template(name, server_url)

    folder, archive = out_dir / PACKAGE, out_dir / f"{PACKAGE}.zip"
    if folder.exists() and not _is_package(folder):
        raise BuildError(
            f"{folder} exists but was not made by this script (it has no worker_agent.py) - "
            "move it away or choose another --out"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".build-", dir=out_dir))  # (same drive as the result: renames, no copying)
    try:
        staged_folder, staged_archive = stage / PACKAGE, stage / archive.name
        staged_folder.mkdir()
        for name, data in files.items():
            (staged_folder / name).write_bytes(data)
        with zipfile.ZipFile(staged_archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, data in files.items():
                bundle.writestr(f"{PACKAGE}/{name}", data)
        _verify(staged_folder, staged_archive, files)
        _swap((staged_folder, staged_archive), (folder, archive), stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return folder, archive


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build the worker package for PC2")
    ap.add_argument("--config", default=str(ROOT / "config.json"), help="PC1's settings file (default: ../config.json)")
    ap.add_argument("--server", metavar="URL", help="PC1 as PC2 reaches it, e.g. http://pc1:8080 (default: this PC)")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="where the package is written (default: ../dist)")
    ap.add_argument("--rotate-token", action="store_true", help="make a new API token and save it in PC1's config.json")
    ap.add_argument("--allowed-root", action="append", default=[], metavar="FOLDER",
                    help="a folder / share the worker may look at (repeat); default: anywhere")
    ap.add_argument("--allow-local", action="store_true", help="accept localhost as PC1 (for a test on this PC only)")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config)
    try:
        if not config_path.is_file():
            raise BuildError(f"{config_path} does not exist")
        settings = Settings(config_path)
        server_url = check_server_url(
            args.server if args.server is not None else default_server_url(settings.port), allow_local=args.allow_local
        )
        token = new_token() if args.rotate_token else settings.api_token
        if not token:
            raise BuildError(
                "tasks.api_token in config.json is empty - set one there or run again with --rotate-token"
            )
        folder, archive = build_package(
            server_url=server_url, token=token, out_dir=Path(args.out), allowed_roots=args.allowed_root
        )
        if args.rotate_token:
            try:
                settings.update(api_token=token)  # (after the build: a failed build must not lock the old workers out)
            except (ConfigError, OSError) as exc:
                raise BuildError(
                    f"the package was built with the NEW token, but it could not be saved in {config_path} ({exc}); "
                    "PC1 still has the old one - fix that and run again with --rotate-token"
                ) from exc
    except (BuildError, ConfigError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print("Worker package for PC2 built:")
    print(f"  folder:  {folder}")
    print(f"  zip:     {archive}")
    print(f"  PC1:     {server_url}")
    print(f"  token:   {len(token)} characters, fingerprint {fingerprint(token)}"
          f"{' (new - saved in ' + str(config_path) + ')' if args.rotate_token else ' (the one in ' + config_path.name + ')'}")
    roots = args.allowed_root
    print(f"  folders: {', '.join(roots) if roots else 'any (set --allowed-root to limit what a recipe can make PC2 look at)'}")
    print()
    print("Next: copy the zip to PC2, unzip it somewhere permanent and double-click install.bat.")
    if settings.host in {"127.0.0.1", "localhost", "::1"}:
        print(f"NOTE: PC1 listens on {settings.host} only - set server.host to 0.0.0.0 in {config_path.name} and restart it.")
    print("PC1's firewall must let the port in: deploy\\open_firewall_pc1.ps1 (run as administrator).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
