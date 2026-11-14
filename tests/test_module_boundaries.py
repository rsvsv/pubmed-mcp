"""Extraction boundaries. All test identifiers/values are 本项目自订测试口径."""
import ast
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]  # 本项目自订：测试源码目录


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Boundaries(unittest.TestCase):
    def test_pure_modules_load_without_sdk_or_core(self):
        # -S excludes site-packages, so pure modules cannot accidentally require MCP.
        with tempfile.TemporaryDirectory() as folder:
            for name in ('pubmed_query.py', 'pubmed_records.py', 'pubmed_report.py', 'pubmed_services.py'):
                target = Path(folder) / name
                shutil.copyfile(ROOT / name, target)
                result = subprocess.run([sys.executable, '-I', '-S', '-B', str(target)],
                                        cwd=folder, capture_output=True, timeout=30)  # 本项目自订：超时
                self.assertEqual(result.returncode, 0, result.stderr)  # 本项目自订：独立加载成功
                tree = ast.parse(target.read_text(encoding='utf-8'))
                imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
                imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
                self.assertFalse(any(n and (n.startswith('mcp') or n.startswith('pubmed_mcp')) for n in imported))

    def test_fingerprint_covers_each_runtime_file_and_missing_files_fail(self):
        runtime = load(ROOT / 'pubmed_runtime.py')
        with tempfile.TemporaryDirectory() as folder:
            for name in runtime.RUNTIME_FILES:
                shutil.copyfile(ROOT / name, Path(folder) / name)
            copied = load(Path(folder) / 'pubmed_runtime.py')
            baseline = copied.implementation_fingerprint()
            self.assertEqual(set(baseline), set(runtime.RUNTIME_FILES))
            for name in runtime.RUNTIME_FILES:
                target = Path(folder) / name
                original = target.read_bytes()
                target.write_bytes(original + b'\n# fixture change\n')
                changed = copied.implementation_fingerprint()
                self.assertNotEqual(changed[name], baseline[name])
                self.assertEqual({k: v for k, v in changed.items() if k != name},
                                 {k: v for k, v in baseline.items() if k != name})
                target.write_bytes(original)
            (Path(folder) / 'pubmed_records.py').unlink()
            with self.assertRaises(FileNotFoundError):
                copied.implementation_fingerprint()
            with self.assertRaisesRegex(ImportError, 'pubmed_records.py'):
                copied.load_sibling('pubmed_records')

    def test_report_uses_current_core_callbacks(self):
        from unittest.mock import Mock, patch
        core = load(ROOT / 'pubmed_mcp.py')
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'audit.jsonl'
            source.write_text('{"event":"fixture","path":"fixture"}\n', encoding='utf-8')
            writer = Mock(return_value=('published', 0, 'fixture-digest'))  # 本项目自订：写入替身结果
            verifier = Mock(return_value={'status': 'fixture-verified'})
            with patch.object(core, '_write_new_file', writer), patch.object(core, '_verify_audit_artifact', verifier):
                result = core._audit_report([str(source)], str(Path(folder) / 'report.md'), verify_files=True)
            self.assertEqual(result['path'], 'published')
            verifier.assert_called_once_with({'event': 'fixture', 'path': 'fixture'})
            self.assertIn('fixture-verified', writer.call_args.args[1])  # 本项目自订：报告正文参数位置
            self.assertFalse((Path(folder) / 'report.md').exists())

    def test_audit_contexts_are_isolated_between_loaded_cores(self):
        from unittest.mock import patch
        import anyio
        import os
        first = load(ROOT / 'pubmed_mcp.py')
        second = load(ROOT / 'pubmed_mcp.py')
        self.assertIsNot(first._AUDIT_CONTEXT, second._AUDIT_CONTEXT)
        self.assertIsNot(first._REQ_CTR, second._REQ_CTR)
        self.assertIsNot(first._AUDIT_LOCK, second._AUDIT_LOCK)
        events = []
        def writer(owner):
            def record(event, fields, **kwargs):
                events.append((owner, fields))
                return {'status': 'disabled'}
            return record
        @second._audited
        async def child():
            self.assertIsNotNone(first._AUDIT_CONTEXT.get())
            self.assertIsNotNone(second._AUDIT_CONTEXT.get())
            self.assertIsNone(second._REQ_CTR.get())
            return {'ok': True}
        @first._audited
        async def parent():
            token = first._REQ_CTR.set([])
            try:
                await child()
                self.assertIsNone(second._AUDIT_CONTEXT.get())
            finally:
                first._REQ_CTR.reset(token)
            return {'ok': True}
        async def run():
            with patch.dict(os.environ, {'PUBMED_AUDIT_DIR': ''}), \
                    patch.object(first, '_write_audit', writer('first')), \
                    patch.object(second, '_write_audit', writer('second')):
                await parent()
            self.assertIsNone(first._AUDIT_CONTEXT.get())
            self.assertIsNone(second._AUDIT_CONTEXT.get())
            self.assertIsNone(first._REQ_CTR.get())
        anyio.run(run)
        self.assertEqual([owner for owner, _ in events], ['second', 'first'])
        self.assertNotEqual(events[0][1]['run_id'], events[1][1]['run_id'])  # 本项目自订：不同实例的事件位置

    def test_audit_session_and_mask_are_read_at_call_time(self):
        from unittest.mock import patch
        import anyio
        import json
        import os
        core = load(ROOT / 'pubmed_mcp.py')
        @core._audited
        async def operation():
            core._audit('operation', note='fixture-sensitive')
            return {'ok': True}
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {'PUBMED_AUDIT_DIR': folder}), \
                    patch.object(core, '_AUDIT_SESSION', 'fixture-new-session'), \
                    patch.object(core, '_mask', lambda text: text.replace('fixture-sensitive', '[masked]')):
                result = anyio.run(operation)
                self.assertEqual(Path(result['audit_path']).name, core._audit_filename())
                self.assertIn('fixture-new-session', result['audit_path'])
                record = json.loads(Path(result['audit_path']).read_text(encoding='utf-8'))
            self.assertEqual(record['note'], '[masked]')
            self.assertIsNone(core._AUDIT_CONTEXT.get())

    def test_audit_restores_parent_when_finalization_fails(self):
        from unittest.mock import patch
        import anyio
        core = load(ROOT / 'pubmed_mcp.py')
        parent = {'run_id': 'fixture-parent'}
        @core._audited
        async def operation():
            return {'ok': True}
        async def run():
            token = core._AUDIT_CONTEXT.set(parent)
            try:
                with patch.object(core, '_utc_now', side_effect=['fixture-start', RuntimeError('clock failed')]):
                    with self.assertRaisesRegex(RuntimeError, 'clock failed'):
                        await operation()
                self.assertIs(core._AUDIT_CONTEXT.get(), parent)
                with patch.object(core, '_write_audit', side_effect=RuntimeError('writer failed')):
                    with self.assertRaisesRegex(RuntimeError, 'writer failed'):
                        await operation()
                self.assertIs(core._AUDIT_CONTEXT.get(), parent)
                with patch.object(core, '_write_audit', return_value={'status': 'disabled'}):
                    self.assertEqual((await operation())['audit_status'], 'disabled')
                self.assertIs(core._AUDIT_CONTEXT.get(), parent)
            finally:
                core._AUDIT_CONTEXT.reset(token)
        anyio.run(run)

    def test_audit_module_loads_without_importing_sdk_or_core(self):
        script = (
            'import importlib.abc, importlib.util, sys\n'
            'class Block(importlib.abc.MetaPathFinder):\n'
            ' def find_spec(self, fullname, path=None, target=None):\n'
            '  if fullname == "mcp" or fullname.startswith("mcp.") or fullname == "pubmed_mcp":\n'
            '   raise RuntimeError("forbidden import: " + fullname)\n'
            'sys.meta_path.insert(0, Block())\n'
            'spec = importlib.util.spec_from_file_location("subject", sys.argv[1])\n'
            'module = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(module)\n'
            'assert module.RuntimeContext().audit.get() is None\n'
        )
        result = subprocess.run([sys.executable, '-I', '-c', script, str(ROOT / 'pubmed_audit.py')],
                                capture_output=True, timeout=30)  # 本项目自订：测试超时
        self.assertEqual(result.returncode, 0, result.stderr)  # 本项目自订：加载成功

    def test_dependencies_reject_unknown_contracts_and_resolve_late(self):
        runtime = load(ROOT / 'pubmed_runtime.py')
        state = {'value': 'before'}
        deps = runtime.Dependencies(('value',), {'value': lambda: state['value']},
                                    {'value': lambda value: state.update(value=value)})
        self.assertEqual(deps.value, 'before')
        state['value'] = 'after'
        self.assertEqual(deps.value, 'after')
        deps.value = 'written'
        self.assertEqual(state['value'], 'written')
        with self.assertRaises(AttributeError):
            deps.unknown = 'invalid'
        with self.assertRaises(AttributeError):
            _ = deps.unknown
        with self.assertRaises(ValueError):
            runtime.Dependencies(('missing',), {})
        with self.assertRaises(ValueError):
            runtime.Dependencies((), {}, {'unknown': lambda value: None})

    def test_transport_state_is_instance_local_and_callbacks_stay_live(self):
        from unittest.mock import patch
        import anyio
        import os
        first, second = load(ROOT / 'pubmed_mcp.py'), load(ROOT / 'pubmed_mcp.py')
        with patch.dict(os.environ, {'NCBI_API_KEY': 'fixture-key-before'}):
            self.assertEqual(first.api_key(), 'fixture-key-before')
        with patch.dict(os.environ, {'NCBI_API_KEY': 'fixture-key-after'}):
            self.assertEqual(second.api_key(), 'fixture-key-after')
        self.assertEqual(first._KEY, 'fixture-key-before')
        first._transport_impl.dependencies._NREQ += 1  # 本项目自订：模拟请求计数
        self.assertEqual(second._NREQ, 0)  # 本项目自订：初始请求计数
        async def run():
            await first._pace()
            await second._pace()
            self.assertIsNot(first._PACER, second._PACER)
            self.assertIsNot(first._PACER_INIT, second._PACER_INIT)
            def replacement(*args, **kwargs):
                return 'fixture-response'
            with patch.object(first, '_get', replacement):
                self.assertEqual(await first._get_async('fixture', {}, 1), 'fixture-response')  # 本项目自订：假超时值
        anyio.run(run)

    def test_review_services_are_late_bound_and_restore_audit_session(self):
        from unittest.mock import patch
        import os
        import anyio
        core = load(ROOT / 'pubmed_mcp.py')
        services = core.review_services()
        with patch.object(core, '_mask', lambda text: 'masked'), patch.object(core, 'GOLD_MAX', 7):  # 本项目自订：接口替身
            self.assertEqual(services.mask('fixture'), 'masked')
            self.assertEqual(services.recall_limit, 7)  # 本项目自订：替身值
        with self.assertRaises(AttributeError):
            _ = services._KEY
        for previous in (None, '', 'fixture-old-directory'):
            with patch.dict(os.environ):
                if previous is None:
                    os.environ.pop('PUBMED_AUDIT_DIR', None)
                else:
                    os.environ['PUBMED_AUDIT_DIR'] = previous
                session = core._AUDIT_SESSION
                with self.assertRaisesRegex(RuntimeError, 'fixture-failure'):
                    with services.audit_session('fixture-new-directory'):
                        outer_session = core._AUDIT_SESSION
                        self.assertNotEqual(outer_session, session)
                        with services.audit_session('fixture-nested-directory'):
                            self.assertNotEqual(core._AUDIT_SESSION, outer_session)
                        self.assertEqual(core._AUDIT_SESSION, outer_session)
                        self.assertEqual(os.environ['PUBMED_AUDIT_DIR'], 'fixture-new-directory')
                        raise RuntimeError('fixture-failure')
                self.assertEqual(os.environ.get('PUBMED_AUDIT_DIR'), previous)
                self.assertEqual(core._AUDIT_SESSION, session)
                async def cancel():
                    with anyio.CancelScope() as scope:
                        with services.audit_session('fixture-cancelled'):
                            scope.cancel()
                            await anyio.sleep(0)  # 本项目自订：取消检查点
                anyio.run(cancel)
                self.assertEqual(os.environ.get('PUBMED_AUDIT_DIR'), previous)
                self.assertEqual(core._AUDIT_SESSION, session)

    def test_probe_offline_fault_cases_patch_and_restore_current_core(self):
        import anyio
        import contextlib
        import io
        from unittest.mock import patch
        core = load(ROOT / 'pubmed_mcp.py')
        tree = ast.parse((ROOT / 'pubmed_diagnostics.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Implementation')
        probe = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_probe')
        start = next(i for i, n in enumerate(probe.body) if isinstance(n, ast.Assign)
                     and isinstance(n.targets[0], ast.Tuple)
                     and any(isinstance(t, ast.Name) and t.id == '_old_exist' for t in n.targets[0].elts))
        stop = next(i for i, n in enumerate(probe.body) if isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Call) and n.value.args
                    and isinstance(n.value.args[0], ast.Constant)
                    and str(n.value.args[0].value).startswith('计数器活着'))
        # Run the actual offline tail of the probe, excluding its earlier live queries.
        probe.body = probe.body[start:stop]
        probe.args.args.append(ast.arg(arg='check'))
        subject = ast.Module(body=[ast.Import(names=[ast.alias(name='json')]), probe], type_ignores=[])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'probe_cases.py'
            path.write_text(ast.unparse(ast.fix_missing_locations(subject)), encoding='utf-8')
            cases = load(path)
            observations = []
            def check(label, passed, detail):
                observations.append(label)
                self.assertTrue(passed, (label, detail))
            async def blocked(*args, **kwargs):
                raise AssertionError('offline probe reached original transport')
            original_exist = core._pmids_exist
            with patch.object(core, '_get_async', blocked), contextlib.redirect_stderr(io.StringIO()):
                anyio.run(cases._probe, core._diagnostics_impl, check)
                self.assertIs(core._get_async, blocked)
                self.assertIs(core._pmids_exist, original_exist)
            self.assertTrue(observations)

    def test_review_uses_only_public_service_factory_from_core(self):
        tree = ast.parse((ROOT / 'pubmed_review.py').read_text(encoding='utf-8'))
        refs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == 'm'}
        self.assertEqual(refs, {'review_services'})

    def test_implementation_dependencies_match_actual_accesses(self):
        for name in ('transport', 'files', 'export', 'diagnostics', 'tools'):
            tree = ast.parse((ROOT / ('pubmed_' + name + '.py')).read_text(encoding='utf-8'))
            self.assertFalse(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                 and n.func.id in ('globals', 'locals', 'eval', 'exec') for n in ast.walk(tree)), name)
            declared = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == 'DEPENDENCIES' for t in n.targets))
            used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Attribute) and n.value.attr == 'dependencies'
                    and isinstance(n.value.value, ast.Name) and n.value.value.id == 'self'}
            self.assertEqual(set(declared), used, name)
            imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            self.assertFalse(any(n and (n == 'mcp' or n.startswith('mcp.') or n == 'pubmed_mcp') for n in imported))

    def test_sibling_load_does_not_share_mutable_module_state(self):
        runtime = load(ROOT / 'pubmed_runtime.py')
        first = runtime.load_sibling('pubmed_query')
        second = runtime.load_sibling('pubmed_query')
        first._TAG_BAD['fixture'] = ('fixture', True)
        self.assertNotIn('fixture', second._TAG_BAD)


if __name__ == '__main__':
    unittest.main()
