"""手动联网契约探针；不由离线 runner 自动执行。
日期、种子与检索式为本项目自订测试输入；输出计数/集合为工具实测，不证明查全。
"""
import importlib.util
import json
from pathlib import Path
import anyio

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('live_subject',ROOT/'pubmed_mcp.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

async def probe():
    results={}
    with anyio.fail_after(45):  # 本项目自订：手动探针超时
        a=await m._esearch('root resorption',retmax=20,mindate='2025',maxdate='2025',datetype='edat')
        b=await m._esearch('(root resorption) AND (2025-01-01:2025-12-31[edat])',retmax=20)
        results['edat']={'parameter_query':a.get('querytranslation'),'term_query':b.get('querytranslation'),
                         'count_parameter':m._count_of(a),'count_term':m._count_of(b),
                         'ids_parameter':a.get('idlist'),'ids_term':b.get('idlist'),
                         'sample_equal':a.get('idlist')==b.get('idlist'),
                         'scope':'实测/工具输出；只比较当前首个窗口，不代表超窗拆片验证'}
        param_ids=[]
        for start in range(0,m._count_of(a),m.RETMAX_CAP):
            page=await m._esearch('root resorption',retmax=m.RETMAX_CAP,retstart=start,mindate='2025',maxdate='2025',datetype='edat')
            param_ids.extend(page.get('idlist') or [])
        full_ids,_,_,drift=await m._ids_in_window('(root resorption) AND (2025-01-01:2025-12-31[edat])')
        results['edat']['full_set_equal']=set(param_ids)==set(full_ids)
        results['edat']['full_parameter_ids']=param_ids
        results['edat']['full_term_ids']=full_ids
        results['edat']['drift']=drift
        results['edat']['scope']='实测/工具输出；本查询窗口内完整集合，不代表超窗拆片验证'
        assert results['edat']['full_set_equal']
        results['relative_frozen_query']=await m._freeze_export_query('root resorption','edat','','',7)
        for direction in ('refs','citedin'):
            result=json.loads(await m.links('29771300',to=direction,as_json=True))
            results[direction]={'sources':result['sources'],'counts':result['counts'],
                               'warning':'实测/工具输出；仅验证 PubMed 返回关系，不代表全文引文覆盖'}
    print(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__':anyio.run(probe)
