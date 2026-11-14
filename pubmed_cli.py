"""Command-line parsing and dispatch, independent of MCP service initialization.

The core passes an explicit operations mapping to run(); this module never imports
pubmed_mcp, so file entrypoints and tests use the same running core instance.
CLI flags, defaults and exit statuses are project-defined interfaces.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

import anyio


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pubmed-mcp",
        description="PubMed 检索服务与文件工作流；不带参数启动 MCP stdio 服务。",
        epilog=("MCP 直接提供 search/count/fetch_abstracts/links/check_recall/mesh/diff/export。\n"
                "综述交付、离线审计报告、名单差集和完整单轮追引用流程通过命令行执行。\n"
                "links 支持 refs/citedin；设置 PUBMED_AUDIT_DIR 后 MCP 调用也会留痕。\n"
                "帮助不会联网或创建任务。命令及文件边界见 docs/review-delivery.md、docs/retrieval-files.md。"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--selftest", nargs="?", const="tooth resorption", metavar="TERM",
                       help="联网自检，可选英文检索词")
    modes.add_argument("--probe", action="store_true", help="联网边界探测；发现失败时非零退出")
    modes.add_argument("--concurrent", nargs="?", const="tooth resorption", metavar="TERM",
                       help="联网并发检查，可选英文检索词")
    modes.add_argument("--check-key", action="store_true", help="本地检查密钥配置形状和依赖；不打印完整密钥")
    modes.add_argument("--audit-report", nargs="+", action="append", metavar="LOG",
                       help="离线生成审计报告；可重复该选项读取多组日志")
    modes.add_argument("--verify-export", metavar="FILE", help="离线核验导出文件与伴随说明")
    modes.add_argument("--export-pmids", metavar="FILE", help="按已有完整 PMID/候选名单导出；题录格式需联网")
    modes.add_argument("--resume-export", metavar="DIR", help="恢复题录导出作业，复用已核验批次")
    modes.add_argument("--diff-files", nargs=2, metavar=("OLD", "NEW"),  # 本项目自订：差集输入顺序
                       help="离线比较旧、新 PMID 名单")
    modes.add_argument("--snowball", metavar="SEEDS", help="联网追查参考文献和施引，扣除已有名单")
    modes.add_argument("--review-plan", metavar="JSON", help="按方案联网生成综述检索交付目录")
    modes.add_argument("--resume-review", metavar="DIR", help="续跑交付或补取缺失题录，可能联网")
    modes.add_argument("--verify-review", metavar="DIR", help="离线核验交付文件、阶段与审计一致性")
    outputs = parser.add_argument_group("输出及命令专用参数")
    outputs.add_argument("--out", metavar="FILE", help="审计报告、名单导出、差集或追引用的输出；拒绝覆盖")
    outputs.add_argument("--format", choices=("pmids", "csv", "ris", "medline"),
                         help="仅用于 --export-pmids；默认 pmids")
    outputs.add_argument("--checkpoint-dir", metavar="DIR", help="仅用于 --export-pmids；新建可恢复作业目录")
    outputs.add_argument("--baseline", metavar="FILE", help="--snowball 必需的已有完整 PMID 名单")
    outputs.add_argument("--meta", metavar="FILE", help="--verify-export 的伴随说明路径；默认自动定位")
    outputs.add_argument("--skip-invalid", action="store_true", help="仅审计报告：保留无效行提示并继续，输入仍标不完整")
    outputs.add_argument("--verify-files", action="store_true", help="仅审计报告：同时核验日志指向的当前文件")
    outputs.add_argument("--out-dir", metavar="DIR", help="--review-plan 必需的新交付目录")
    return parser


# Each option has one owner (or a small explicit owner set). Validation happens
# before dispatch, so misplaced arguments never disappear into an unrelated mode.
OPTION_OWNERS = {
    "out": {"audit_report", "export_pmids", "diff_files", "snowball"},
    "format": {"export_pmids"},
    "checkpoint_dir": {"export_pmids"},
    "baseline": {"snowball"},
    "meta": {"verify_export"},
    "skip_invalid": {"audit_report"},
    "verify_files": {"audit_report"},
    "out_dir": {"review_plan"},
}


def parse_args(argv):
    parser = build_parser()
    # Help remains usable while a command is incomplete; respect the option terminator.
    option_tokens = argv[:argv.index("--")] if "--" in argv else argv
    if any(token in {"--help", "-h"} for token in option_tokens):
        parser.parse_args(["--help"])
    options = parser.parse_args(argv)
    mode = next((name for name, value in vars(options).items()
                 if name not in OPTION_OWNERS and value is not None and value is not False), None)
    if mode is None:
        parser.error("请指定命令；不带任何参数才会启动 MCP 服务")
    for option, owners in OPTION_OWNERS.items():
        value = getattr(options, option)
        if value is not None and value is not False and mode not in owners:
            parser.error("--" + option.replace("_", "-") + " 不适用于当前命令")
    if mode in OPTION_OWNERS["out"] and not options.out:
        parser.error("当前命令需要 --out")
    if mode == "snowball" and not options.baseline:
        parser.error("--snowball 必须配 --baseline")
    if mode == "review_plan" and not options.out_dir:
        parser.error("--review-plan 必须配 --out-dir")
    return mode, options


def run(argv, operations, review_path):
    """Dispatch using explicit core callbacks; no core import or process globals mutation."""
    mode, options = parse_args(argv)
    if mode in {"review_plan", "resume_review", "verify_review"}:
        path = Path(review_path)
        spec = importlib.util.spec_from_file_location("pubmed_review", path)
        review = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(review)
        forwarded = ["--" + mode.replace("_", "-") + "=" + getattr(options, mode)]
        if options.out_dir is not None:
            forwarded.append("--out-dir=" + options.out_dir)
        raise SystemExit(review.main(forwarded))

    async def file_command():
        if mode == "export_pmids":
            return await operations[mode](options.export_pmids, options.out, options.format or "pmids", options.checkpoint_dir or "")
        if mode == "resume_export":
            return await operations[mode](options.resume_export)
        if mode == "snowball":
            return await operations[mode](options.snowball, options.baseline, options.out)
        return await anyio.to_thread.run_sync(operations[mode], *options.diff_files, options.out)

    try:
        if mode in {"export_pmids", "resume_export", "snowball", "diff_files"}:
            result = anyio.run(file_command)
        elif mode == "audit_report":
            result = operations[mode]([p for group in options.audit_report for p in group], options.out,
                                      skip_invalid=options.skip_invalid, verify_files=options.verify_files)
        elif mode == "verify_export":
            result = operations[mode](options.verify_export, options.meta or "")
        elif mode == "selftest":
            operations[mode](options.selftest)
            return
        elif mode == "probe":
            raise SystemExit(1 if anyio.run(operations[mode]) else 0)  # 本项目自订：探测退出状态
        elif mode == "concurrent":
            anyio.run(operations[mode], options.concurrent)
            return
        else:
            print("pubmed-mcp:", operations["version"]())
            operations["check_key"]()
            return
    except (operations["error_type"], OSError, ValueError) as exc:
        print(operations["mask"](str(exc)), file=sys.stderr)
        raise SystemExit(1) from exc  # 本项目自订：执行失败退出状态
    print(json.dumps(result, ensure_ascii=False))
    if mode == "verify_export" and result["status"] != "verified":
        raise SystemExit(1)  # 本项目自订：文件核验不通过退出状态
