"""离线合成基准；不代表 NCBI 网络速度。所有规模/延迟为本项目自订夹具。
运行：python -I tests/benchmark_export.py；输出为实测/工具输出。
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from unittest.mock import patch
import anyio

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bench_subject',ROOT/'pubmed_mcp.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

async def benchmark():
    ids=[str(i) for i in range(1,1001)]  # 本项目自订：基准规模
    def record(pid):
        return {'pmid':pid,'title':'fixture','authors':[], 'journal':{},'pubdate':'2020',
                'lang':[],'ids':{},'abstract':[{'label':'','text':'x'*5000}],  # 本项目自订：摘要长度
                'mesh':[],'pt':[],'comments':[]}
    async def fetch(batch):
        await anyio.lowlevel.checkpoint()
        return [record(pid) for pid in batch]
    results={}
    with tempfile.TemporaryDirectory() as folder, patch.object(m,'_efetch_xml_all',fetch):
        for label,writer in [('all_in_memory',m._export_ris_batch),('bounded_batches',m._export_ris)]:
            tracemalloc.start(); start=time.perf_counter()
            path=Path(folder)/(label+'.ris')
            count,missing=await writer(str(path),ids)
            elapsed=time.perf_counter()-start
            _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
            results[label]={'seconds':elapsed,'peak_python_bytes':peak,'written':count,
                            'missing':missing,'sha256':m._sha256_file(str(path)),
                            'evidence':'实测/工具输出；合成题录，非联网性能'}
        # 按记录内容比较；批边界允许额外空白，集合相同不能代替逐条字段检查。
        a=(Path(folder)/'all_in_memory.ris').read_text().split('ER  - ')
        b=(Path(folder)/'bounded_batches.ris').read_text().split('ER  - ')
        assert [v.strip() for v in a]==[v.strip() for v in b]
    print(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__': anyio.run(benchmark)
