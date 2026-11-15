"""Portable install failure regressions; fixture names and values are 本项目自订测试口径."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('portable_install_subject', Path(__file__).resolve().parents[1] / 'scripts/install_portable.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class PortableInstall(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.bundle = self.root / 'bundle'
        self.home = self.root / 'home'
        sources = {'runtime/pubmed_mcp.py': b'# fixture', 'runtime/requirements.txt': b'',
                   'skills/literature-search/SKILL.md': b'new daily',
                   'skills/systematic-review/SKILL.md': b'new review'}
        manifest = {'runtime_files': ['pubmed_mcp.py'], 'files': {}}
        for rel, data in sources.items():
            p = self.bundle / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            manifest['files'][rel] = hashlib.sha256(data).hexdigest()
        (self.bundle / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.daily = self.home / '.claude/skills/literature-search/SKILL.md'
        self.review = self.home / '.claude/skills/systematic-review/SKILL.md'
        self.config = self.home / '.claude.json'
        self.originals = {self.daily: b'old daily', self.review: b'old review',
                          self.config: b'{"mcpServers":{"other":{"command":"keep"}},"fixture":true}'}
        for p, data in self.originals.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        for hook in (patch.object(m.venv, 'EnvBuilder'),
                     patch.object(m.subprocess, 'run', return_value=SimpleNamespace(stdout='--review-plan'))):
            hook.start()
            self.addCleanup(hook.stop)

    def install(self):
        return m.install(self.bundle, self.home, configure='code')

    def test_locked_unmodified_config_does_not_block_skill_restore(self):
        atomic = m.atomic_write
        def fail_config(path, data):
            if path == self.config:
                raise PermissionError('fixture locked config')
            atomic(path, data)
        with patch.object(m, 'atomic_write', side_effect=fail_config):
            with self.assertRaises(PermissionError):
                self.install()
        for p, data in self.originals.items():
            self.assertEqual(p.read_bytes(), data)
        self.assertFalse((self.daily.parent / 'references/installation.json').exists())
        self.assertFalse((self.review.parent / 'references/installation.json').exists())
        journals = list((self.home / '.claude/pubmed-mcp/backups').glob('*/restore.json'))
        self.assertEqual(len(journals), 1)
        for entry in json.loads(journals[0].read_text(encoding='utf-8'))['files']:
            original = self.originals.get(Path(entry['path']))
            if original is not None:
                self.assertEqual(Path(entry['backup']).read_bytes(), original)

    def test_one_failed_restore_does_not_prevent_remaining_restore(self):
        atomic = m.atomic_write
        def fail_write_and_restore(path, data):
            if path == self.config:
                raise PermissionError('fixture original failure')
            if path == self.review and data == self.originals[self.review]:
                raise PermissionError('fixture restore failure')
            atomic(path, data)
        with patch.object(m, 'atomic_write', side_effect=fail_write_and_restore):
            with self.assertRaisesRegex(RuntimeError, 'rollback incomplete') as caught:
                self.install()
        self.assertIsInstance(caught.exception.__cause__, PermissionError)
        self.assertIn('fixture original failure', str(caught.exception.__cause__))
        self.assertIn(str(self.review), str(caught.exception))
        self.assertIn('restore.json', str(caught.exception))
        self.assertEqual(self.daily.read_bytes(), self.originals[self.daily])
        self.assertEqual(self.config.read_bytes(), self.originals[self.config])
        self.assertEqual(self.review.read_bytes(), b'new review')
        self.assertFalse((self.daily.parent / 'references/installation.json').exists())

    def test_backup_failure_does_not_attempt_rollback_writes(self):
        write = Path.write_bytes
        def fail_journal(path, data):
            if path.name == 'restore.json':
                raise PermissionError('fixture journal failure')
            return write(path, data)
        with patch.object(Path, 'write_bytes', fail_journal), patch.object(m, 'atomic_write') as atomic:
            with self.assertRaisesRegex(PermissionError, 'journal failure'):
                self.install()
        atomic.assert_not_called()
        for p, data in self.originals.items():
            self.assertEqual(p.read_bytes(), data)

    def test_postwrite_verification_failure_restores_modified_target(self):
        atomic = m.atomic_write
        def corrupt(path, data):
            atomic(path, b'corrupted fixture' if path == self.daily and data == b'new daily' else data)
        with patch.object(m, 'atomic_write', side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                self.install()
        for p, data in self.originals.items():
            self.assertEqual(p.read_bytes(), data)

    def test_success_preserves_other_configuration(self):
        self.install()
        config = json.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(config['mcpServers']['other'], {'command': 'keep'})
        self.assertTrue(config['fixture'])
        self.assertEqual(self.daily.read_bytes(), b'new daily')
        self.assertTrue((self.home / '.claude/pubmed-mcp/installation.json').exists())


if __name__ == '__main__':
    unittest.main()
