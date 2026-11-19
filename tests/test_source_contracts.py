"""本项目自订：所有 PMID、计数、上游回包都是离线夹具，不是文献证据。"""
import importlib.util
import json
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio

spec = importlib.util.spec_from_file_location('contract_subject', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SOURCE, BAD, TARGET, NEXT = '11', '22', '101', '202'  # 本项目自订：来源与目标分开


def run(fn, *args, **kwargs):
    return anyio.run(partial(fn, *args, **kwargs))


def groups(name, ids):
    return [{'linkname': name, 'links': ids}]


class SourceContracts(unittest.TestCase):
    def test_bad_source_cannot_add_edges_or_erase_other_source(self):
        cases = [
            ('pubmed', {'linksetdbs': groups('pubmed_pubmed', [NEXT])}),
            ('refs', {'ids': [BAD], 'linksetdbs': [{'links': [NEXT]}]}),
            ('refs', {'ids': [BAD], 'linksetdbs': groups('pubmed_pubmed_citedin', [NEXT])}),
            ('refs', {'ids': [BAD], 'linksetdbs': {}}),
            ('refs', {'ids': [BAD], 'linksetdbs': 7}),  # 本项目自订：不是关系数组
            ('refs', {'id': BAD, 'ids': [SOURCE], 'linksetdbs': groups('pubmed_pubmed_refs', [NEXT])}),
        ]
        for direction, bad in cases:
            async def reply(endpoint, params, timeout):
                good = {'id': SOURCE, 'linksetdbs': groups(params.get('linkname', 'pubmed_pubmed'), [TARGET])}
                return json.dumps({'linksets': [good if params['id'] == SOURCE else bad]})
            with self.subTest(direction=direction, bad=bad), patch.object(m, '_get_async', reply), \
                    patch.object(m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present', BAD: 'present'})):
                data = json.loads(run(m.links, f'{SOURCE} {BAD}', to=direction, as_json=True))
                self.assertEqual(data['edges'], [{'source': SOURCE, 'relation': direction, 'target': TARGET}])
                self.assertEqual(data['sources'][-1]['relation_status'], 'error')
                self.assertNotIn('未录入该方向', '\n'.join(data['warnings']))

    def test_text_unique_citations_and_languages(self):
        body = {'linksets': [{'ids': [SOURCE], 'linksetdbs': groups('pubmed_pubmed_refs', [TARGET, TARGET, NEXT])}]}
        titles = [(pid, {'title': 'fixture', 'lang': ['eng', 'fre']}) for pid in (TARGET, NEXT)]
        with patch.object(m, '_get_async', AsyncMock(return_value=json.dumps(body))), \
                patch.object(m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present'})), \
                patch.object(m, '_esummary_dicts', AsyncMock(return_value=titles)):
            text = run(m.links, SOURCE, to='refs', with_titles=True)
        self.assertIn(f'目标 PMID：{TARGET},{NEXT}', text)
        self.assertIn('语言：eng, fre', text)

    def test_receipt_and_audit_bind_every_failed_source(self):
        failed = ['22', '33', '44', '55', '66', '77']  # 本项目自订：超过预览截断长度
        states = dict.fromkeys([SOURCE, *failed], 'present') | {BAD: 'unknown'}
        async def reply(endpoint, params, timeout):
            pid = params['id']
            if pid in failed:
                raise m.NcbiError('fixture timeout')
            return json.dumps({'linksets': [{'ids': [pid]}]})
        with tempfile.TemporaryDirectory() as folder, patch.object(m, '_get_async', reply), \
                patch.object(m, '_pmids_exist', AsyncMock(return_value=states)), patch.object(m, '_audit') as audit:
            text = run(m.links, ' '.join(states), to='refs')
            receipt = json.loads(run(m.links, ' '.join(states), to='refs', out_path=str(Path(folder) / 'result.json')))
        row = next(line for line in text.splitlines() if line.startswith(f'PMID {BAD} '))
        self.assertIn('请求失败', row)
        self.assertIn('存在性', row)
        self.assertEqual([r['source'] for r in receipt['source_issues']], failed)
        self.assertEqual(receipt['source_issues'][0]['source_status'], 'unknown')  # 本项目自订：题录与关联独立
        fields = next(c.kwargs for c in audit.call_args_list if 'source_states' in c.kwargs)
        self.assertEqual(fields['source_states'][SOURCE]['relation_status'], 'ok')
        self.assertEqual(fields['source_states'][BAD]['source_status'], 'unknown')
        self.assertEqual(fields['source_states'][BAD]['relation_status'], 'error')

    def test_no_linkset_differs_from_zero_in_audit(self):
        async def reply(endpoint, params, timeout):
            return json.dumps({'linksets': [{'ids': [SOURCE]}] if params['id'] == SOURCE else []})
        with patch.object(m, '_get_async', reply), patch.object(m, '_audit') as audit, \
                patch.object(m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present', BAD: 'unknown'})):
            run(m.links, f'{SOURCE} {BAD}', to='pmc')
        fields = audit.call_args.kwargs
        self.assertEqual(fields['source_states'][BAD]['relation_status'], 'no_linkset')
        self.assertEqual(fields['source_states'][SOURCE]['relation_status'], 'ok')
        self.assertEqual(fields['errors'], 1)  # 本项目自订：一条未核实关联，不是两条零关联

    def test_recall_failed_batch_is_unknown_in_input_order(self):
        async def reply(q, **kw):
            if BAD in q:
                raise m.NcbiError('fixture timeout')
            return {'_term': q, 'count': '1', 'idlist': [SOURCE], 'querytranslation': f'tooth[All Fields] AND {SOURCE}[UID]'}  # 本项目自订：一个确认命中
        for pids in ([BAD, SOURCE], [SOURCE, BAD]):
            with self.subTest(order=pids), patch.object(m, 'GOLD_CHUNK', 1), patch.object(m, '_esearch', reply):  # 本项目自订：每号一批
                data = json.loads(run(m.check_recall, 'tooth', ' '.join(pids), as_json=True))
                text = run(m.check_recall, 'tooth', ' '.join(pids))
                self.assertEqual(data['found'], [SOURCE])
                self.assertEqual(data['retrieved'], [SOURCE])
                self.assertEqual(data['missing'], [])
                self.assertEqual(data['unknown'], [BAD])
                self.assertEqual([c['pmids'] for c in data['checks']], [[p] for p in pids])
                self.assertIn('fixture timeout', text)
                self.assertIn(BAD, text)

    def test_all_recall_requests_failed_is_not_empty_success(self):
        with patch.object(m, 'GOLD_CHUNK', 1), patch.object(m, '_esearch', AsyncMock(side_effect=m.NcbiError('offline'))):  # 本项目自订：两个请求都失败
            with self.assertRaisesRegex(m.NcbiError, '全部批次失败.*offline'):
                run(m.check_recall, 'tooth', f'{SOURCE} {BAD}', as_json=True)

    def test_incomplete_or_contradictory_links_do_not_become_evidence(self):
        name = 'pubmed_pubmed_refs'
        for ls in ({'linksetdbs': [{'linkname': name}]},
                   {'linksetdbs': [{'linkname': name, 'links': None}]},
                   {'dbfrom': 'gene', 'linksetdbs': groups(name, [TARGET])},
                   {'linksetdbs': [{'linkname': name, 'dbto': 'gene', 'links': [TARGET]}]},
                   {'linksetdbs': groups(name, ['000'])}):  # 本项目自订：无效目标
            async def wire(endpoint, params, timeout):
                row = ls if params['id'] == BAD else {'linksetdbs': groups(name, [])}
                return json.dumps({'linksets': [{'ids': [params['id']], **row}]})
            with self.subTest(ls=ls), patch.object(m, '_get_async', wire), patch.object(
                    m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present', BAD: 'present'})):
                page = json.loads(run(m.links, f'{SOURCE} {BAD}', to='refs', compact=True))
                self.assertEqual([s['relation_status'] for s in page['sources']], ['ok', 'error'])
                self.assertFalse(page['targets'])
                self.assertNotIn(BAD, next(w for w in page['warnings'] if '未录入该方向' in w))

    def test_unknown_all_sources_has_same_outcome_and_audit(self):
        async def wire(endpoint, params, timeout):
            if params['id'] == BAD:
                raise m.NcbiError('fixture unavailable')
            return json.dumps({'linksets': []})
        with tempfile.TemporaryDirectory() as folder, patch.object(m, '_get_async', wire), patch.object(
                m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present', BAD: 'unknown'})):
            path = Path(folder) / 'not-written.json'
            for options in ({}, {'compact': True}, {'as_json': True}, {'out_path': str(path)}):
                with self.subTest(options=options), patch.object(m, '_audit') as audit:
                    with self.assertRaises(m.NcbiError):
                        run(m.links, f'{SOURCE} {BAD}', **options)
                    states = audit.call_args.kwargs['source_states']
                    self.assertEqual(states[SOURCE]['relation_status'], 'no_linkset')
                    self.assertIn('fixture unavailable', states[BAD]['error'])
                    self.assertFalse(path.exists())

    def test_missing_language_element_preserves_title(self):
        body = {'linksets': [{'ids': [SOURCE], 'linksetdbs': groups('pubmed_pubmed_refs', [TARGET])}]}
        with patch.object(m, '_get_async', AsyncMock(return_value=json.dumps(body))), patch.object(
                m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present'})), patch.object(
                m, '_esummary_dicts', AsyncMock(return_value=[(TARGET, {'title': 'intact', 'lang': ['eng', None]})])):
            text = run(m.links, SOURCE, to='refs', with_titles=True)
            self.assertIn('语言：eng｜intact', text)
            data = json.loads(run(m.links, SOURCE, to='refs', with_titles=True, compact=True))
            self.assertEqual(data['targets'][0]['languages'], ['eng'])  # 本项目自订：单目标

    def test_full_file_keeps_other_relation_without_creating_direct_edge(self):
        group = groups('pubmed_pmc_refs', [TARGET])
        body = {'linksets': [{'ids': [SOURCE], 'linksetdbs': group}]}
        with tempfile.TemporaryDirectory() as folder, patch.object(m, '_get_async', AsyncMock(return_value=json.dumps(body))), patch.object(
                m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present'})):
            path = Path(folder) / 'links.json'
            run(m.links, SOURCE, to='pmc', out_path=str(path))
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(data['edges'])
            self.assertEqual(data['sources'][0]['other_relations'][0]['relation'], 'pubmed_pmc_refs')  # 本项目自订：独立于直接关系

    def test_projection_sorts_details_once(self):
        body = {'linksets': [{'ids': [SOURCE], 'linksetdbs': groups('pubmed_pubmed_refs', [TARGET, NEXT])}]}
        titles = [(p, {'title': p}) for p in (TARGET, NEXT)]
        with patch.object(m, '_get_async', AsyncMock(return_value=json.dumps(body))), patch.object(
                m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present'})), patch.object(
                m, '_esummary_dicts', AsyncMock(return_value=titles)), patch.object(
                m._tools_module, 'sorted', wraps=sorted, create=True) as sorts:
            page = json.loads(run(m.links, SOURCE, to='refs', with_titles=True, as_json=True))
        sorts.assert_called_once()
        self.assertEqual([r['pmid'] for r in page['candidate_details']], [TARGET, NEXT])
        self.assertFalse(page['outside_term_pmids'])

    def test_unreliable_received_list_preserves_observation(self):
        async def wire(term, **kwargs):
            pid = BAD if BAD in term else SOURCE
            return {'count': '2' if pid == BAD else '1', 'idlist': [NEXT if pid == BAD else SOURCE],  # 本项目自订：回包名单不属本批
                    '_term': term, 'querytranslation': term}
        with patch.object(m, 'GOLD_CHUNK', 1), patch.object(m, '_esearch', wire):  # 本项目自订：每源单批
            data = json.loads(run(m.check_recall, 'tooth', f'{SOURCE} {BAD}', as_json=True))
            check = data['checks'][-1]
            self.assertEqual(check['observed_pmids'], [NEXT])
            self.assertTrue(check['querytranslation'])
            self.assertEqual(data['unknown'], [BAD])
            self.assertEqual(data['found'], [SOURCE])
            self.assertNotIn(NEXT, data['retrieved'])

    def test_rejected_wire_page_retains_observed_ids_not_found(self):
        def payload(endpoint, params, timeout):
            bad = BAD + '[uid]' in params['term']
            ids = [BAD, BAD] if bad else [SOURCE]
            return json.dumps({'esearchresult': {'count': str(len(ids)), 'idlist': ids, 'querytranslation': params['term']}})
        with patch.object(m, 'GOLD_CHUNK', 1), patch.object(m, '_get_async', AsyncMock(side_effect=payload)):  # 本项目自订：每号一批
            data = json.loads(run(m.check_recall, 'tooth', f'{SOURCE} {BAD}', as_json=True))
        bad = data['checks'][-1]
        self.assertEqual(bad['error_kind'], 'invalid_intersection')
        self.assertEqual((bad['observed_pmids'], bad['returned']), ([BAD, BAD], [BAD]))
        self.assertIn(BAD + '[uid]', bad['querytranslation'])
        self.assertEqual((data['found'], data['unknown']), ([SOURCE], [BAD]))

    def test_audited_direct_total_deduplicates_overlapping_linknames(self):
        group = groups('pubmed_nuccore', [TARGET]) + groups('pubmed_nuccore_refseq', [TARGET])
        with patch.object(m, '_get_async', AsyncMock(return_value=json.dumps({'linksets': [{'ids': [SOURCE], 'linksetdbs': group}]}))), patch.object(
                m, '_pmids_exist', AsyncMock(return_value={SOURCE: 'present'})), patch.object(m, '_audit') as audit:
            data = json.loads(run(m.links, SOURCE, to='nucleotide', as_json=True))
        self.assertEqual(audit.call_args.kwargs['direct_total'], len(data['edges']))
        self.assertEqual(data['counts']['targets'], len({TARGET}))

    def test_snowball_preserves_individual_failures(self):
        async def wire(endpoint, params, timeout):
            pid = params['id']
            if params['linkname'].endswith('_refs'):
                raise m.NcbiError('fixture error ' + pid)
            return json.dumps({'linksets': [{'ids': [pid]}]})
        states = {SOURCE: 'present', BAD: 'unknown'}
        with tempfile.TemporaryDirectory() as folder, patch.object(m, '_get_async', wire), patch.object(
                m, '_pmids_exist', AsyncMock(return_value=states)):
            root = Path(folder)
            (root / 'seeds').write_text('\n'.join(states))
            (root / 'baseline').write_text('')
            run(m._snowball, str(root / 'seeds'), str(root / 'baseline'), str(root / 'result.json'))
            data = json.loads((root / 'result.json').read_text(encoding='utf-8'))
        failed = [s for s in data['sources'] if s['relation'] == 'refs']
        self.assertEqual({s['source']: s['source_status'] for s in failed}, states)
        self.assertEqual([s['error'] for s in failed], ['fixture error ' + p for p in states])
        self.assertEqual([e['seeds'] for e in data['source_errors']], [[p] for p in states])

    def test_retry_preserves_only_reused_metadata_failure(self):
        async def search(term, **kwargs):
            if BAD + '[uid]' in term:
                raise m.NcbiError('old intersection timeout')
            return {'count': '0', 'idlist': [], 'querytranslation': term, '_term': term}  # 本项目自订：可判定未检回
        with patch.object(m, 'GOLD_CHUNK', 1), patch.object(m, '_esearch', search), patch.object(  # 本项目自订：每号一批
                m, '_esummary_dicts', AsyncMock(side_effect=m.NcbiError('old title timeout'))):
            previous = json.loads(run(m.check_recall, 'tooth', f'{SOURCE} {BAD}', as_json=True))
        async def retry(term, **kwargs):
            self.assertIn(BAD + '[uid]', term)
            return {'count': '1', 'idlist': [BAD], 'querytranslation': term, '_term': term}  # 本项目自订：恢复一条
        with tempfile.TemporaryDirectory() as folder, patch.object(m, '_esearch', retry):
            path = Path(folder) / 'receipt.json'
            path.write_text(json.dumps(previous), encoding='utf-8')
            result = json.loads(run(m._tools_impl.retry_recall, 'tooth', f'{SOURCE} {BAD}', str(path)))
        self.assertEqual(result['missing'], previous['missing'])
        self.assertIn('old title timeout', '\n'.join(result['warnings']))
        self.assertNotIn('old intersection timeout', '\n'.join(result['warnings']))

    def test_recall_missing_uid_is_not_missing_subject(self):
        term = f'tooth AND {TARGET}[uid]'
        d = {'count': '1', 'idlist': [SOURCE], 'querytranslation': f'tooth[All Fields] AND {SOURCE}[UID]'}  # 本项目自订：仅用户 UID 消失
        with patch.object(m, '_esearch', AsyncMock(return_value=d)):
            text = run(m.check_recall, term, SOURCE)
        self.assertIn('无法判定', text)
        self.assertNotIn('原主题未参与', text)
