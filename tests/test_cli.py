"""CLI boundary regressions. Literals/counts/paths are 本项目自订测试口径."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]  # 本项目自订：源码位置
spec = importlib.util.spec_from_file_location('cli_test_core', ROOT/'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CommandLine(unittest.TestCase):
    def test_help_has_all_modes_and_no_effects(self):
        for args in (['--help'], ['-h'], ['--review-plan', '--help'], ['--audit-report', '--help']):
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output, \
                    patch.object(m.mcp, 'run', side_effect=AssertionError('started server')), \
                    patch.object(m, '_get', side_effect=AssertionError('network')), \
                    patch.object(m, '_audit_report', side_effect=AssertionError('report')), \
                    self.assertRaises(SystemExit) as stopped:
                m.main(args)
            self.assertEqual(stopped.exception.code, 0)  # 本项目自订：帮助成功
            for flag in ('--selftest', '--probe', '--concurrent', '--check-key', '--audit-report',
                         '--verify-export', '--export-pmids', '--resume-export', '--diff-files',
                         '--snowball', '--review-plan', '--resume-review', '--verify-review'):
                self.assertIn(flag, output.getvalue())
            self.assertIn('不带参数', output.getvalue())

    def test_no_arguments_keep_stdio_without_cli_output(self):
        with patch.object(m.mcp, 'run') as serve, contextlib.redirect_stdout(io.StringIO()) as output:
            m.main([])
        serve.assert_called_once_with()
        self.assertEqual(output.getvalue(), '')

    def test_bad_or_misplaced_arguments_fail_before_execution(self):
        cases = [ ['--probeX'], ['--check'], ['--check-key', 'extra'],
                  ['--selftest', 'tooth', 'extra'], ['--probe', '--check-key'],
                  ['--out', 'unused'], ['--check-key', '--out', 'unused'],
                  ['--review-plan', 'plan.json'], ['--resume-review', 'job', '--out-dir', 'new'],
                  ['--verify-export', 'file', '--format', 'ris'],
                  ['--resume-export', 'job', '--out', 'new'],
                  ['--snowball', 'seeds', '--out', 'new'],
                  ['--diff-files', 'old', 'new', '--out', 'diff', '--baseline', 'base'] ]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(m, '_check_key', side_effect=AssertionError('executed')), \
                    patch.object(m, '_selftest', side_effect=AssertionError('executed')), \
                    self.assertRaises(SystemExit) as stopped:
                m.main(args)
            self.assertEqual(stopped.exception.code, 2)  # 本项目自订：参数错误

    def test_dispatch_uses_existing_core_instance(self):
        with patch.object(m, '_selftest') as test:
            m.main(['--selftest', 'custom term'])
        test.assert_called_once_with('custom term')
        with patch.object(m, '_check_key') as check, contextlib.redirect_stdout(io.StringIO()):
            m.main(['--check-key'])
        check.assert_called_once_with()

    def test_standalone_isolated_entry_from_other_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in m._runtime.RUNTIME_FILES:
                (root/name).write_bytes((ROOT/name).read_bytes())
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, '-I', '-B', str(root/'pubmed_mcp.py'), '--help'],
                                        cwd=cwd, capture_output=True, encoding='utf-8', timeout=30)  # 本项目自订：超时
            self.assertEqual(result.returncode, 0, result.stderr)  # 本项目自订：帮助成功
            self.assertIn('--review-plan', result.stdout)
            (root/'pubmed_cli.py').unlink()
            result = subprocess.run([sys.executable, '-I', '-B', str(root/'pubmed_mcp.py'), '--help'],
                                    cwd=cwd if Path(cwd).exists() else folder, capture_output=True,
                                    encoding='utf-8', timeout=30)  # 本项目自订：超时
            self.assertNotEqual(result.returncode, 0)  # 本项目自订：拒绝残缺安装
            self.assertIn('缺少相邻 pubmed_cli.py', result.stderr)

    def test_review_forwarding_preserves_leading_hyphen_paths(self):
        for mode in ('review-plan', 'resume-review', 'verify-review'):
            args = ['--' + mode + '=-missing-review']
            if mode == 'review-plan':
                args.append('--out-dir=-delivery')
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, '-I', '-B', str(ROOT/'pubmed_mcp.py'), *args],
                                        cwd=cwd, capture_output=True, encoding='utf-8', timeout=30)  # 本项目自订：超时
                self.assertEqual(result.returncode, 1, result.stderr)  # 本项目自订：路径进入执行后报不存在，而非参数解析错误
                self.assertNotIn('expected one argument', result.stderr)
                self.assertIn('-missing-review', result.stderr)

    def test_file_dispatch_and_repeated_audit_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new, output = root/'old.txt', root/'new.txt', root/'diff.json'
            old.write_text('1\n', encoding='utf-8')  # 本项目自订：旧名单
            new.write_text('1\n2\n', encoding='utf-8')  # 本项目自订：新名单
            with contextlib.redirect_stdout(io.StringIO()):
                m.main(['--out', str(output), '--diff-files', str(old), str(new)])
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['candidate_pmids'], ['2'])  # 本项目自订：新增 PMID
            logs = [root/'a.jsonl', root/'b.jsonl']
            for path in logs:
                path.write_text('{"event":"count"}\n', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                m.main(['--audit-report', str(logs[0]), '--audit-report', str(logs[1]), '--out', str(root/'report.csv')])  # 本项目自订：重复日志组选项
            self.assertTrue((root/'report.csv').is_file())


if __name__ == '__main__':
    unittest.main()
