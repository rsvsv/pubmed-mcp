"""Check installed distribution, CLI and stdio without querying NCBI.
All timeout/version/test argument literals below are 本项目自订测试口径.
"""
from importlib.metadata import version
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import anyio
import pubmed_mcp
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
                assert schema["properties"]["detail"]["default"] == "full"
                assert schema["properties"]["detail"]["enum"] == ["full", "brief", "ids"]
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
    print("Installed module:", pubmed_mcp.__file__)
    print("Installed versions:", {p: version(p) for p in ("pubmed-mcp", "mcp", "anyio", "pydantic")})
    with tempfile.TemporaryDirectory() as workdir:
        subprocess.run([cli, "--check-key"], cwd=workdir, check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        anyio.run(smoke, cli, workdir)
