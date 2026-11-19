"""Opt-in network acceptance: python tests/accept_public_handoff.py /path/to/pubmed-mcp.

Uses the supplied installed console, an anonymous home and an ephemeral handoff. The
PMIDs and scope below are project software-test inputs, not evidence of recall in general.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PID, CONTROL = '19912982', '33555773'  # 来源：已核对历史工程测试锚点；非临床金标准
SCOPE = dict(term='Portuguese[Title] AND KOOS-PS[Title]', mindate='2009', maxdate='2010', datetype='pdat')  # 本项目自订：测试窗口与检索式


def check(ok, message):
    if not ok:
        raise RuntimeError(message)


async def via_stdio(cli, root, env):
    params = StdioServerParameters(command=str(cli), cwd=str(root), env=env)
    with anyio.fail_after(90):  # 本项目自订：验收超时，不代表服务 SLA
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, args):
                reply = await session.call_tool(name, args)
                check(not getattr(reply, 'isError', getattr(reply, 'is_error', False)), f'{name}: {reply.content}')
                return json.loads(reply.content[0].text)  # 来源：工具 stdio 文本回执

            page = await call('search', dict(**SCOPE, as_json=True, diagnose='off'))
            check(PID in page['search_pmids'] and any(r['pmid'] == PID for r in page['records']), 'known citation missing from search')
            result = await call('check_recall', dict(**SCOPE, pmids=f'{PID} {CONTROL}', as_json=True))
            check(result['found'] == [PID] and [r['pmid'] for r in result['missing']] == [CONTROL]
                  and not result['unknown'], f"unexpected check_recall result: {result['checks']}")
            filtered = await call('check_recall', dict(term='knee NOT review[pt]', pmids=PID, as_json=True))
            check(filtered['found'] == [PID] and not filtered['unknown'],
                  f"mapped topic with publication type exclusion not confirmed: {filtered['checks']}")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('console', type=Path, help='Installed pubmed-mcp executable')
    cli = parser.parse_args().console.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='pubmed-public-handoff-') as folder:
        root = Path(folder)
        home = root/'anonymous'
        home.mkdir()
        # Pass only transport/runtime prerequisites; never load a user's key or audit directory.
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
                                          'SSL_CERT_FILE', 'SSL_CERT_DIR') if k in os.environ}
        env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8='1', NCBI_API_KEY='', NCBI_EMAIL='', PUBMED_AUDIT_DIR='')
        anyio.run(via_stdio, cli, root, env)
        plan = dict(**SCOPE, question='工程测试：真实工具入口至筛选表，不作临床判断', eligibility='本项目自订测试条件',
                    strategy_status='approved', strategy_review_note='工程测试夹具的预期查询；不是实际研究策略批准',
                    known_relevant=[dict(pmid=PID, source='历史软件测试锚点，不是从本轮搜索反造')],
                    other_databases='工程测试仅覆盖 PubMed', citation_tracking='工程测试不评估追引或查全')
        plan_path, out = root/'plan.json', root/'delivery'
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')

        def command(*args):
            p = subprocess.run([str(cli), *map(str, args)], cwd=root, env=env, capture_output=True,
                               encoding='utf-8', timeout=120)  # 本项目自订：联网超时
            check(p.returncode == 0, p.stderr + p.stdout)
            return json.loads(p.stdout)

        checked = command('--review-plan', plan_path, '--out-dir', out)
        verified = command('--verify-review', out)
        check(checked['status'] == verified['status'] == 'verified', 'offline verification failed')
        check(checked['handoff_status'] == verified['handoff_status'] == 'ready_for_screening',
              'handoff blocked: ' + str(json.loads((out/'delivery.json').read_text('utf-8'))['issues']))
        ids = (out/'all-pmids.txt').read_text('utf-8').split()
        with (out/'screening.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        check([r['pmid'] for r in rows] == ids and PID in ids, 'screening CSV differs from PMID list')
        row = next(r for r in rows if r['pmid'] == PID)
        check(row['reviewer_decision'] == '待筛选' and row['abstract_status'] == 'available'
              and bool(row['abstract'].strip()) and 'KOOS' in row['title'], 'known article not readable in screening CSV')
        check(f'AN  - {PID}\n' in (out/'references.ris').read_text('utf-8'), 'known article absent from RIS')
        print('PASS: anonymous installed stdio search/recall, review CLI export, screening CSV, RIS and offline verify')


if __name__ == '__main__':
    main()
