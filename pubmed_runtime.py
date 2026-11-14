"""Adjacent-module loading and implementation identity for file and wheel installs.

Each load returns an isolated module instance. Business modules never import the
MCP entrypoint; no sys.path changes or process-wide module cache are required.
"""
import hashlib
import importlib.util
from pathlib import Path


RUNTIME_FILES = (
    "pubmed_mcp.py", "pubmed_cli.py", "pubmed_review.py", "pubmed_runtime.py",
    "pubmed_query.py", "pubmed_records.py", "pubmed_report.py", "pubmed_audit.py",
    "pubmed_transport.py", "pubmed_files.py", "pubmed_export.py",
    "pubmed_diagnostics.py", "pubmed_tools.py", "pubmed_services.py",
)


class Dependencies:
    """A declared contract of late-bound operations, never a module namespace."""
    def __init__(self, required, getters, setters=None):
        if set(required) != set(getters):
            raise ValueError("运行依赖与声明不一致")
        if not set(setters or {}).issubset(getters):
            raise ValueError("可写状态未声明")
        object.__setattr__(self, "_getters", getters)
        object.__setattr__(self, "_setters", setters or {})

    def __getattr__(self, name):
        try:
            getter = self._getters[name]
        except KeyError:
            raise AttributeError(name) from None
        return getter()

    def __setattr__(self, name, value):
        try:
            setter = self._setters[name]
        except KeyError:
            raise AttributeError("只读或未声明的运行状态：" + name) from None
        setter(value)


def load_sibling(name):
    """Load this installation's module, independent of cwd and installed copies."""
    if name + ".py" not in RUNTIME_FILES:
        raise ValueError("未知运行模块：" + name)
    path = Path(__file__).with_name(name + ".py")
    if not path.is_file():
        raise ImportError("缺少相邻运行模块 " + path.name + "；请完整安装或复制全部运行模块。")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def implementation_fingerprint():
    """Hash all runtime sources; a version string alone does not identify a build."""
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in RUNTIME_FILES}
