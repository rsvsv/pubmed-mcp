"""单轮滚雪球回归；数字/PMID/日期均为本项目自订夹具。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import anyio

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('snow_subject',ROOT/'pubmed_mcp.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class Snowball(unittest.TestCase):
    def test_recomputable_candidates_and_missing_titles(self):
        async def run():
            async def links(pmids,to,as_json):
                targets=['1','2','3'] if to=='refs' else ['3','4']
                return json.dumps({'sources':[{'source':'1','source_status':'present','relation':to,'target_ids':targets,'error':None}],
                                   'edges':[{'source':'1','relation':to,'target':pid} for pid in targets]})
            async def summary(ids):
                return [('3',{'title':'three'})]
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder); seeds=root/'seeds.txt'; baseline=root/'old.txt'; out=root/'out.json'
                seeds.write_text('1\n'); baseline.write_text('2\n')
                with patch.object(m,'links',links), patch.object(m,'_esummary_dicts',summary):
                    result=await m._snowball(str(seeds),str(baseline),str(out))
                rec=json.loads(out.read_text(encoding='utf-8'))
                self.assertEqual(rec['candidate_pmids'],['3','4'])
                self.assertEqual(rec['title_missing'],['4'])
                expected=[pid for pid in rec['before_exclusion'] if pid not in set(rec['seeds']['ids']+rec['baseline']['ids'])]
                self.assertEqual(expected,rec['candidate_pmids'])
                self.assertEqual(len(rec['edges']),5)
                self.assertEqual(result['sha256'],m._sha256_file(str(out)))
        anyio.run(run)

    def test_failed_direction_preserved(self):
        async def run():
            async def links(pmids,to,as_json):
                if to=='refs': raise m.NcbiError('fixture failure')
                return json.dumps({'sources':[{'source':'1','source_status':'present','relation':to,'target_ids':[],'error':None}], 'edges':[]})
            with tempfile.TemporaryDirectory() as folder:
                root=Path(folder); seeds=root/'seed'; baseline=root/'old'; out=root/'result.json'
                seeds.write_text('1'); baseline.write_text('')
                with patch.object(m,'links',links):
                    result=await m._snowball(str(seeds),str(baseline),str(out))
                self.assertTrue(result['source_errors'])
                rec=json.loads(out.read_text(encoding='utf-8'))
                self.assertEqual(len(rec['sources']),2)
                self.assertEqual(rec['candidate_pmids'],[])
        anyio.run(run)

if __name__=='__main__': unittest.main()
