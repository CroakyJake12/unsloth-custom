"""Transactional per-user replacement for the existing Unsloth desktop/runtime."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
import zipfile
from contextlib import closing


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot_db(source, target):
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(target))) as dest:
        src.backup(dest)


def replace_file(source, target):
    # Installed wheel files can be hard-linked to uv's cache. Never overwrite a linked inode.
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".custom-new")
    shutil.copy2(source, temporary)
    os.replace(temporary, target)


def stop_exact_processes(paths):
    import psutil
    wanted = {os.path.normcase(os.path.realpath(str(p))) for p in paths}
    processes = []
    for process in psutil.process_iter(["exe", "cmdline"]):
        try:
            if os.path.normcase(os.path.realpath(process.info["exe"] or "")) in wanted:
                processes.append(process)
                # Unsloth's venv launcher delegates to the base Python executable.
                processes.extend(process.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for process in reversed(processes):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, active = psutil.wait_procs(processes, timeout=15)
    if active:
        raise RuntimeError("Unsloth did not close. No install files were replaced.")


def desktop_session(studio_home):
    secret = (studio_home / "auth" / ".desktop_secret").read_text().strip()
    request = urllib.request.Request("http://127.0.0.1:8888/api/auth/desktop-login",
        data=json.dumps({"secret": secret}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["access_token"]


def wait_for_idle(studio_home, timeout=900):
    deadline = time.monotonic()+timeout
    try:
        token = desktop_session(studio_home)
    except urllib.error.URLError:
        return  # App is already closed; validation above still checks its exact install files.
    while time.monotonic() < deadline:
        request = urllib.request.Request("http://127.0.0.1:8888/api/inference/active-generations", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=10) as response:
            count = json.load(response)["count"]
        with closing(sqlite3.connect(studio_home / "studio.db")) as conn:
            research_count = conn.execute("SELECT COUNT(*) FROM research_runs WHERE status IN ('planning','running','queued')").fetchone()[0]
        if count == 0 and research_count == 0:
            return
        print(f"Waiting for current work to finish: {count} generation(s), {research_count} research run(s)", flush=True)
        time.sleep(10)
    raise RuntimeError("Current work is still running. Install files were left unchanged; run this installer after it finishes.")


def check_health(port, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(1)
    raise RuntimeError("Custom backend failed to become healthy")


def check_custom_build(studio_home, version):
    token = desktop_session(studio_home)
    request = urllib.request.Request("http://127.0.0.1:8888/api/project-tools/build", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        if json.load(response).get("version") != version:
            raise RuntimeError("The custom backend version could not be verified")


def cleanup_build_source(manifest):
    root_value = manifest.get("cleanupRoot")
    if not root_value:
        return []
    root = Path(root_value).resolve()
    allowed = (Path(os.environ["USERPROFILE"]) / "Documents" / "Codex").resolve()
    if root == allowed or not root.is_relative_to(allowed):
        raise RuntimeError("Build cleanup root is outside the authorized Codex workspace")
    marker = root / "work" / ".custom-build-cleanup"
    if not marker.exists() or marker.read_text().strip() != manifest.get("cleanupMarker"):
        return []  # Already cleaned or this installer is being used on another machine.
    removed = []
    for name in ["npm-cache", "work"]:
        target = root / name
        resolved = target.resolve()
        if resolved.parent != root or resolved != target or target.is_symlink():
            raise RuntimeError("Build cleanup target failed the resolved-path check")
        if target.exists():
            shutil.rmtree(target)
            removed.append(str(target))
    return removed


def table_counts(path):
    if not path.exists():
        return {}
    with closing(sqlite3.connect(str(path))) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: conn.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
                for name in ["chat_projects", "chat_threads", "chat_messages", "app_settings", "chat_settings"] if name in names}


def run_installer(payload, studio_home, desktop, *, test=False):
    manifest = json.loads((payload / "manifest.json").read_text(encoding="utf-8"))
    runtime = studio_home / "unsloth_studio" / "Lib" / "site-packages" / "studio"
    python = studio_home / "unsloth_studio" / "Scripts" / "python.exe"
    executable = desktop / "unsloth-studio.exe"
    if not (runtime / "backend" / "main.py").exists() or not executable.exists():
        raise RuntimeError("The existing Unsloth desktop/runtime could not be found")
    for file in manifest["files"]:
        source, target = payload / "backend" / file["path"], runtime / "backend" / file["path"]
        if digest(source) != file["after"]:
            raise RuntimeError("Installer payload checksum failed: " + file["path"])
        if file["before"] is None:
            if target.exists() and digest(target) != file["after"]:
                raise RuntimeError("Custom file already exists with different contents: " + file["path"])
        elif digest(target) != file["before"]:
            if digest(target) == file["after"]:
                raise RuntimeError("This custom build is already installed")
            raise RuntimeError("Installed source changed since validation: " + file["path"])
    if digest(payload / "unsloth-studio.exe") != manifest["desktopSha256"]:
        raise RuntimeError("Desktop payload checksum failed")
    if not test:
        wait_for_idle(studio_home)
        stop_exact_processes([executable])
    before = table_counts(studio_home / "studio.db")
    backup = studio_home / "custom-backups" / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=False)
    snapshot_db(studio_home / "studio.db", backup / "studio.db")
    snapshot_db(studio_home / "auth" / "auth.db", backup / "auth" / "auth.db")
    originals = []
    for file in manifest["files"]:
        target = runtime / "backend" / file["path"]
        saved = backup / "backend" / file["path"]
        if target.exists():
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        originals.append((target, saved if target.exists() else None))
    shutil.copy2(executable, backup / "unsloth-studio.exe")
    # Keep the entire WebView profile in its original location. Only the executable and modules change.
    frontend_backup = backup / "frontend-dist"
    if (runtime / "frontend" / "dist").exists():
        shutil.copytree(runtime / "frontend" / "dist", frontend_backup)
    log = backup / "install-result.json"
    try:
        for file in manifest["files"]:
            replace_file(payload / "backend" / file["path"], runtime / "backend" / file["path"])
        replace_file(payload / "unsloth-studio.exe", executable)
        dist = runtime / "frontend" / "dist"
        # Static browser assets are separate from settings. Old hashed assets may remain safely.
        with zipfile.ZipFile(payload / "frontend.zip") as archive:
            for item in archive.infolist():
                target = (dist / item.filename).resolve()
                if not target.is_relative_to(dist.resolve()):
                    raise RuntimeError("Invalid frontend archive path")
            for item in archive.infolist():
                if item.is_dir():
                    continue
                target = dist / item.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".custom-new")
                temporary.write_bytes(archive.read(item))
                os.replace(temporary, target)
        if table_counts(studio_home / "studio.db") != before:
            raise RuntimeError("Existing project/chat/settings counts changed during replacement")
        # Compile installed files in memory, without altering the shared uv cache.
        for file in manifest["files"]:
            source = runtime / "backend" / file["path"]
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
        if not test:
            subprocess.Popen([str(executable)], cwd=str(desktop))
            check_health(8888)
            check_custom_build(studio_home, manifest["version"])
        result = {"status": "installed", "version": manifest["version"], "backup": str(backup), "preservedCounts": before}
        if not test:
            try:
                result["removedBuildSource"] = cleanup_build_source(manifest)
            except Exception as cleanup_error:
                result["cleanupWarning"] = str(cleanup_error)
        log.write_text(json.dumps(result, indent=2), encoding="utf-8")
        (studio_home / "custom-build.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return result
    except BaseException as exc:
        if not test:
            stop_exact_processes([executable])
        for target, saved in originals:
            if saved is None:
                if target.exists():
                    target.unlink()
            else:
                replace_file(saved, target)
        replace_file(backup / "unsloth-studio.exe", executable)
        # The database is additive and was never rewritten; retain any newly saved messages.
        if frontend_backup.exists():
            shutil.copytree(frontend_backup, runtime / "frontend" / "dist", dirs_exist_ok=True)
        log.write_text(json.dumps({"status": "rolled-back", "error": str(exc), "backup": str(backup)}, indent=2), encoding="utf-8")
        if not test:
            subprocess.Popen([str(executable)], cwd=str(desktop))
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--studio-home", type=Path, default=Path(os.environ["USERPROFILE"]) / ".unsloth" / "studio")
    parser.add_argument("--desktop", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "Unsloth Studio (Desktop)")
    args = parser.parse_args()
    run_installer(args.payload, args.studio_home, args.desktop)
