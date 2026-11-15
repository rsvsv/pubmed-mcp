"""Build a curated runtime/skill bundle; filenames and schema are project policy."""
from pathlib import Path
import argparse
import ast
import hashlib
import json
import shutil
import zipfile

DOCS = ("systematic-review-workflow.md", "retrieval-files.md", "review-delivery.md",
        "review-plan.template.json", "audit-report.md")
SKILL_FILES = ("literature-search/SKILL.md", "literature-search/references/operations.md",
               "literature-search/references/reading.md", "literature-search/references/retrieval.md",
               "systematic-review/SKILL.md", "systematic-review/references/operations.md")


def build(repo, output):
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "pubmed-mcp-portable"
    bundle.mkdir()
    tree = ast.parse((repo / "pubmed_runtime.py").read_text(encoding="utf-8"))
    runtime = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RUNTIME_FILES" for t in node.targets))
    mapping = {"runtime/" + name: repo / name for name in (*runtime, "requirements.txt")}
    mapping.update({"skills/" + name: repo / ".claude/skills" / name for name in SKILL_FILES})
    mapping.update({"skills/systematic-review/references/" + name: repo / "docs" / name for name in DOCS})
    mapping.update({"install.py": repo / "scripts/install_portable.py", "LICENSE": repo / "LICENSE",
                    "安装说明.md": repo / "docs/portable-install.md"})
    for relative, source in mapping.items():
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (bundle / "install-windows.cmd").write_bytes(
        b'@echo off\r\npython -I "%~dp0install.py" --configure both\r\n'
        b'if errorlevel 1 (\r\n  echo Installation failed. Keep this window output for diagnosis.\r\n'
        b'  pause\r\n  exit /b 1\r\n)\r\necho Installation completed. Restart Claude.\r\npause\r\n')
    manifest = {"evidence": "Build record: hashes measured from curated bundle files; runtime_files from pubmed_runtime.py RUNTIME_FILES. Integrity only, not a signature.",
                "runtime_files": list(runtime), "files": {}}
    for item in sorted(bundle.rglob("*")):
        if item.is_file():
            manifest["files"][item.relative_to(bundle).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = output / "pubmed-mcp-portable.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as stream:
        for item in sorted(bundle.rglob("*")):
            if item.is_file():
                stream.write(item, item.relative_to(output))
    with zipfile.ZipFile(archive) as stream:
        if stream.testzip() is not None:
            raise RuntimeError("ZIP verification failed")
    if not archive.is_file() or archive.stat().st_size == 0:
        raise RuntimeError("Missing/empty archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256.txt").write_text(digest + "  " + archive.name + "  [measured: build tool output]\n", encoding="utf-8")
    print(json.dumps({"archive": str(archive), "bundle": str(bundle), "sha256": digest}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.repo.resolve(), args.output.resolve())
