"""流式导出正确性；所有规模和题录为本项目自订夹具。"""
import importlib.util
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import anyio

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stream_subject', ROOT / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Streaming(unittest.TestCase):
    def test_bounded_parallel_order_and_missing(self):
        async def run():
            active = peak = 0
            async def writer(path, ids):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await anyio.lowlevel.checkpoint()
                Path(path).write_text('\n'.join(ids) + '\n', encoding='utf-8')
                active -= 1
                return len(ids), []
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder)/'out.ris'
                ids = [str(i) for i in range(1, 22)]
                with patch.object(m, 'EFETCH_CHUNK', 2), patch.object(m, '_export_ris_batch', writer):
                    count, missing = await m._export_ris(str(target), ids)
                self.assertEqual(target.read_text().split(), ids)
                self.assertEqual(count, len(ids))
                self.assertEqual(missing, [])
                self.assertLessEqual(peak, 3)
                self.assertEqual(list(Path(folder).iterdir()), [target])
        anyio.run(run)

    def test_empty_first_batch_has_no_leading_separator(self):
        async def run():
            async def writer(path, ids):
                if ids == ['1']:
                    Path(path).write_text('', encoding='utf-8')
                    return 0, ids
                Path(path).write_text('TY  - JOUR\nAN  - 2\nER  - \n', encoding='utf-8')
                return len(ids), []
            with tempfile.TemporaryDirectory() as folder:
                target=Path(folder)/'out.ris'
                with patch.object(m,'EFETCH_CHUNK',1),patch.object(m,'_export_ris_batch',writer):
                    count,missing=await m._export_ris(str(target),['1','2'])
                self.assertTrue(target.read_bytes().startswith(b'TY'))
                self.assertEqual(missing,['1'])
        anyio.run(run)

    def test_csv_header_written_once(self):
        async def run():
            async def writer(path, ids):
                with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                    w=csv.writer(f); w.writerow(['pmid','title'])
                    w.writerows((pid, 'fixture') for pid in ids)
                return len(ids), []
            with tempfile.TemporaryDirectory() as folder:
                target=Path(folder)/'out.csv'
                with patch.object(m,'ESUMMARY_CHUNK',2), patch.object(m,'_export_csv_batch',writer):
                    await m._export_csv(str(target),['1','2','3','4','5'])
                with target.open(encoding='utf-8-sig',newline='') as f:
                    rows=list(csv.DictReader(f))
                self.assertEqual([row['pmid'] for row in rows],['1','2','3','4','5'])
                self.assertEqual(target.read_bytes().count(b'\xef\xbb\xbf'),1)
        anyio.run(run)

    def test_medline_reorders_source_records(self):
        async def run():
            with tempfile.TemporaryDirectory() as folder:
                target=Path(folder)/'out.nbib'
                with patch.object(m,'_get_async',AsyncMock(return_value='PMID- 2\nTI  - second\n\nPMID- 1\nTI  - first\n')):
                    count, missing=await m._export_medline(str(target),['1','2'])
                self.assertTrue(target.read_text().startswith('PMID- 1'))
                self.assertEqual(count,2)
                self.assertEqual(missing,[])
        anyio.run(run)

if __name__=='__main__':
    unittest.main()
