"""Install a curated bundle outside the checkout; numeric constants are project policy."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import venv

SKILLS = ("literature-search", "systematic-review")


def verify_bundle(bundle):
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    actual_files = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    if actual_files != set(manifest["files"]) | {"manifest.json"}:
        raise ValueError("Bundle has missing or unexpected files; extract a fresh copy")
    for name, expected in manifest["files"].items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle.resolve()):
            raise ValueError("Unsafe bundle path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Bundle checksum mismatch: " + name)
    return manifest


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pubmed-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def desktop_path(home):
    if sys.platform == "win32":
        # Respect redirected AppData for the real user; test homes stay isolated.
        base = Path(os.environ.get("APPDATA", home / "AppData/Roaming")) if home == Path.home() else home / "AppData/Roaming"
        return base / "Claude/claude_desktop_config.json"
    if sys.platform == "darwin":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    raise ValueError("Desktop configuration is supported only on Windows/macOS; use --configure code or none")


def config_update(path, command, args):
    raw = path.read_bytes() if path.exists() else None
    data = json.loads(raw.decode("utf-8-sig")) if raw is not None else {}
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError("Unexpected configuration shape: " + str(path))
    servers = data.setdefault("mcpServers", {})
    entry = servers.get("pubmed", {})
    if not isinstance(entry, dict):
        raise ValueError("Unexpected PubMed configuration shape: " + str(path))
    # Preserve user environment/key settings, replace incompatible transport fields.
    updated = {key: value for key, value in entry.items()
               if key not in ("type", "url", "headers", "command", "args", "cwd")}
    updated.update({"command": command, "args": args})
    servers["pubmed"] = updated
    return raw, json_bytes(data)


def install(bundle, home, configure="none", skills_only=False, runtime=None, python=None):
    manifest = verify_bundle(bundle)
    home = home.resolve()
    if skills_only and configure != "none":
        raise ValueError("--skills-only cannot change client configuration")
    targets = []
    if configure in ("code", "both"):
        targets.append(home / ".claude.json")
    if configure in ("desktop", "both"):
        targets.append(desktop_path(home))
    # Validate all configurations before preparing an installation.
    for path in targets:
        config_update(path, "validation-only", [])
    root = home / ".claude/pubmed-mcp"
    root.mkdir(parents=True, exist_ok=True)
    install_id = uuid.uuid4().hex
    release = root / "releases" / install_id
    backup = root / "backups" / install_id
    backup.mkdir(parents=True, exist_ok=False)
    changed = []
    try:
        if skills_only:
            if not runtime or not python:
                raise ValueError("--skills-only requires --runtime and --python")
            runtime = Path(runtime).resolve()
            executable = str(Path(python).resolve())
            for name in manifest["runtime_files"]:
                actual = hashlib.sha256((runtime / name).read_bytes()).hexdigest()
                if actual != manifest["files"]["runtime/" + name]:
                    raise ValueError("Existing runtime differs from bundle: " + name)
        else:
            release.mkdir(parents=True, exist_ok=False)
            runtime = release / "runtime"
            shutil.copytree(bundle / "runtime", runtime)
            environment = release / "venv"
            venv.EnvBuilder(with_pip=True).create(environment)
            executable = str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            subprocess.run([executable, "-I", "-m", "pip", "install", "-r", str(runtime / "requirements.txt")], check=True)
        args = ["-I", str(runtime / "pubmed_mcp.py")]
        # Verify actual startup from outside both source and extraction directories.
        with tempfile.TemporaryDirectory(prefix="pubmed-start-") as work:
            helped = subprocess.run([executable, *args, "--help"], cwd=work, check=True,
                                    capture_output=True, encoding="utf-8", timeout=60)
            if "--review-plan" not in helped.stdout:
                raise RuntimeError("Installed CLI verification failed")
        record = {"command": executable, "args": args,
                  "evidence": "Installation record: resolved paths; manifest hashes verified. No live NCBI test implied."}
        writes = []
        for name in SKILLS:
            source = bundle / "skills" / name
            destination = home / ".claude/skills" / name
            for item in sorted(source.rglob("*")):
                if item.is_file():
                    writes.append((destination / item.relative_to(source), item.read_bytes()))
            writes.append((destination / "references/installation.json", json_bytes(record)))
        for target in targets:
            _, content = config_update(target, executable, args)
            writes.append((target, content))
        writes.append((root / "installation.json", json_bytes(record)))
        # Snapshot originals before replacing any global files. Private backups are never bundled.
        journal = []
        originals = {}
        for index, (path, content) in enumerate(writes):
            old = path.read_bytes() if path.exists() else None
            saved = backup / str(index)
            if old is not None:
                saved.write_bytes(old)
            journal.append({"path": str(path), "backup": str(saved) if old is not None else None})
            originals[path] = old
        (backup / "restore.json").write_bytes(json_bytes({"files": journal}))
        for path, content in writes:
            changed.append((path, originals[path]))
            atomic_write(path, content)
            if path.read_bytes() != content:
                raise RuntimeError("Installed file verification failed: " + str(path))
        print("Installed MCP command:", executable)
        print("Installed MCP arguments:", json.dumps(args))
        print("Global skills:", home / ".claude/skills")
        print("Private backup:", backup)
        print("Client configuration:", configure)
        print("Restart the client / open a new Code session to discover skills.")
        return record
    except BaseException as original_error:
        restore_errors = []
        for path, previous in reversed(changed):
            try:
                if previous is None:
                    path.unlink(missing_ok=True)
                elif not path.exists() or path.read_bytes() != previous:
                    atomic_write(path, previous)
            except BaseException as restore_error:
                restore_errors.append(f"{path} ({type(restore_error).__name__})")
        # Retain failed staging and backup; one locked file must not block other restores.
        if restore_errors:
            raise RuntimeError(
                "Installation failed; rollback incomplete for: " + "; ".join(restore_errors)
                + ". Original files are retained in " + str(backup / "restore.json")
            ) from original_error
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--home", type=Path, default=Path.home(), help="Alternate user root for isolated installation tests")
    parser.add_argument("--configure", choices=("none", "code", "desktop", "both"), default="none")
    parser.add_argument("--skills-only", action="store_true")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--python")
    args = parser.parse_args()
    install(args.bundle.resolve(), args.home, args.configure, args.skills_only, args.runtime, args.python)


if __name__ == "__main__":
    main()
