"""Check installed distribution, CLI and stdio without querying NCBI.
All timeout/version/test argument literals below are 本项目自订测试口径.
"""
from importlib.metadata import version
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile

import anyio
import pubmed_mcp
import pubmed_cli
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke(cli, workdir):
    with anyio.fail_after(30):
        async with stdio_client(StdioServerParameters(command=cli, cwd=workdir)) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = [t.name for t in tools]
                assert set(names) == {"search", "count", "fetch_abstracts", "links",
                                      "check_recall", "mesh", "diff", "export"}, names
                assert len(names) == len(set(names)), names
                assert all(t.description for t in tools)
                search = next(t for t in tools if t.name == "search")
                schema = getattr(search, "inputSchema", None) or getattr(search, "input_schema")
                assert schema["properties"]["detail"]["default"] == "brief"
                assert schema["properties"]["detail"]["enum"] == ["full", "brief", "ids"]
                assert schema["properties"]["datetype"]["enum"] == ["pdat", "edat"]
                link_tool = next(t for t in tools if t.name == "links")
                link_schema = getattr(link_tool, "inputSchema", None) or getattr(link_tool, "input_schema")
                assert {"out_path", "as_json", "offset", "limit"}.issubset(link_schema["properties"])
                export = next(t for t in tools if t.name == "export")
                eschema = getattr(export, "inputSchema", None) or getattr(export, "input_schema")
                assert eschema["properties"]["format"]["default"] == "pmids"
                assert eschema["properties"]["format"]["enum"] == ["pmids", "csv", "medline", "ris"]
                # Invalid mode/format must fail before any NCBI request, through the wire protocol.
                for tool, args, word in (("search", {"term": "tooth", "detail": "invalid"}, "detail"),
                                         ("export", {"term": "tooth", "path": "x.txt", "format": "invalid"}, "format")):
                    result = await session.call_tool(tool, args)
                    error = getattr(result, "isError", None)
                    if error is None:
                        error = getattr(result, "is_error", False)
                    assert error, result
                    assert word in "\n".join(getattr(c, "text", "") for c in result.content)
                print("Installed stdio tools/list and invalid-mode/format tools/call: PASS")


if __name__ == "__main__":
    cli = shutil.which("pubmed-mcp", path=str(Path(sys.executable).parent))
    if not cli:
        cli = shutil.which("pubmed-mcp")
    assert cli, "Installed console script missing"
    source = Path(__file__).resolve().parents[1] / "pubmed_mcp.py"
    assert Path(pubmed_mcp.__file__).resolve() != source, "Smoke must import installed package, not checkout"
    assert Path(pubmed_cli.__file__).resolve().parent == Path(pubmed_mcp.__file__).resolve().parent
    fingerprint = pubmed_mcp._implementation_fingerprint()
    assert set(fingerprint) == set(pubmed_mcp._runtime.RUNTIME_FILES)
    for module in (pubmed_mcp._query, pubmed_mcp._records, pubmed_mcp._report, pubmed_mcp._audit_module,
                   pubmed_mcp._transport_module, pubmed_mcp._files_module, pubmed_mcp._export_module,
                   pubmed_mcp._diagnostics_module, pubmed_mcp._tools_module,
                   pubmed_mcp._runtime.load_sibling('pubmed_services')):
        assert Path(module.__file__).parent == Path(pubmed_mcp.__file__).parent
    print("Installed runtime modules and implementation fingerprint: PASS")
    print("Installed module:", pubmed_mcp.__file__)
    print("Installed versions:", {p: version(p) for p in ("pubmed-mcp", "mcp", "anyio", "pydantic")})
    with tempfile.TemporaryDirectory() as workdir:
        for entry in ([cli], [sys.executable, "-I", "-m", "pubmed_mcp"]):
            helped = subprocess.run([*entry, "--help"], cwd=workdir, check=True, timeout=30,  # 本项目自订：帮助超时
                                    capture_output=True, encoding="utf-8")
            assert "--review-plan" in helped.stdout and "--snowball" in helped.stdout
        print("Installed console/module help: PASS")
        subprocess.run([cli, "--check-key"], cwd=workdir, check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log = Path(workdir) / "legacy.jsonl"
        log.write_text(json.dumps({"event": "count", "term": "fixture", "count": 0}) + "\n", encoding="utf-8")
        report = Path(workdir) / "report.md"
        result = subprocess.run([cli, "--audit-report", str(log), "--out", str(report)], cwd=workdir,
                                check=True, timeout=30, capture_output=True, encoding="utf-8")
        assert json.loads(result.stdout)["records"] == 1
        assert "fixture" in report.read_text(encoding="utf-8")
        print("Installed audit-report CLI: PASS")
        old, new = Path(workdir)/"old.txt", Path(workdir)/"new.txt"
        old.write_text("1\n2\n", encoding="utf-8")
        new.write_text("2\n3\n", encoding="utf-8")
        diff = Path(workdir)/"diff.json"
        def command(*args):
            result = subprocess.run([cli, *args], cwd=workdir, check=True, timeout=30,
                                    capture_output=True, encoding="utf-8")
            return json.loads(result.stdout)
        command("--diff-files", str(old), str(new), "--out", str(diff))
        assert json.loads(diff.read_text(encoding="utf-8"))["candidate_pmids"] == ["3"]
        exported = Path(workdir)/"exported.txt"
        command("--export-pmids", str(diff), "--out", str(exported), "--checkpoint-dir", str(Path(workdir)/"job"))
        assert exported.read_text(encoding="utf-8").split() == ["3"]
        meta = {"schema":"pubmed.export.v1", "sha256":pubmed_mcp._sha256_file(str(exported))}
        Path(str(exported)+".meta.json").write_text(json.dumps(meta), encoding="utf-8")
        assert command("--verify-export", str(exported))["status"] == "verified"
        print("Installed file diff/export/checkpoint/verify CLI: PASS")
        anyio.run(smoke, cli, workdir)
