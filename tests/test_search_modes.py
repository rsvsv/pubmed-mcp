"""Output mode regressions. All fixture numbers/IDs are 本项目自订测试输入."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch, AsyncMock

import anyio

spec = importlib.util.spec_from_file_location("modes_subject", Path(__file__).resolve().parents[1] / "pubmed_mcp.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


class SearchModes(unittest.TestCase):
    def setUp(self):
        self.search = {"_term": "tooth", "count": "2", "idlist": ["22", "11"], "retstart": "0", "querytranslation": '"tooth"[All Fields]'}
        self.records = [(pid, {"title": f"Complete title {pid}", "pubdate": "2026", "source": "Journal", "volume": "9", "pages": "1-3", "authors": [{"name": "Author A"}], "articleids": [{"idtype": "doi", "value": "10.test/example"}], "pubtype": ["Journal Article", "Retracted Publication", "Published Erratum"]}) for pid in self.search["idlist"]]
        self.esearch = AsyncMock(return_value=self.search)
        self.esummary = AsyncMock(return_value=self.records)
        for name, mock in (("_esearch", self.esearch), ("_esummary_dicts", self.esummary)):
            patcher = patch.object(m, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_matches_explicit_brief(self):
        for json_mode in (False, True):
            self.assertEqual(run(m.search, "tooth", as_json=json_mode), run(m.search, "tooth", as_json=json_mode, detail="brief"))

    def test_explicit_full_preserves_original_shape(self):
        data = json.loads(run(m.search, "tooth", as_json=True, detail="full"))
        self.assertNotIn("detail", data)
        self.assertEqual(data["records"][0], m._record_parts(*self.records[0]))

    def test_full_lists_authors_without_breaking_legacy_preview(self):
        names = ['One A', 'Two B', 'Three C', 'Study Group']  # 本项目自订：合成作者名及团体作者
        self.records[0][1]['authors'] = [{'name': n} for n in names]
        records = json.loads(run(m.search, 'tooth', detail='full', as_json=True))['records']
        self.assertEqual(records[0]['author_names'], names)
        self.assertEqual(records[0]['authors'], ', '.join(names[:-1]))
        self.assertTrue(records[0]['authors_truncated'])
        self.assertFalse(records[-1]['authors_truncated'])
        self.assertEqual(m._record_parts('test', {})['author_names'], [])

    def test_non_string_author_name_is_missing_not_an_error(self):
        self.records[0][1]['authors'] = [{'name': 'One A'}, {'name': 'Two B'}, {'name': 'Three C'}, {'name': 7}]  # 本项目自订：异常作者名夹具
        parts = m._record_parts(*self.records[0])
        self.assertEqual(parts['author_names'], ['One A', 'Two B', 'Three C', ''])  # 本项目自订：异常名按缺失
        self.assertEqual(parts['authors'], 'One A, Two B, Three C')  # 本项目自订：旧预览不变
        self.assertEqual(parts['author_count'], 4)  # 本项目自订：合成题录含缺名的作者位置
        self.assertTrue(parts['authors_truncated'])
        brief = json.loads(run(m.search, 'tooth', as_json=True))
        self.assertEqual(brief['records'][0]['title'], parts['title'])  # 本项目自订：默认模式不丢首条题录
        full = json.loads(run(m.search, 'tooth', detail='full', as_json=True))
        self.assertEqual(full['records'][0]['author_names'], parts['author_names'])
        text = run(m.search, 'tooth', detail='full')
        self.assertIn('Complete title 22', text)  # 本项目自订：文本输出未整页丢失

    def test_brief_keeps_titles_and_all_types(self):
        full = json.loads(run(m.search, "tooth", as_json=True, detail="full"))
        brief = json.loads(run(m.search, "tooth", as_json=True, detail="brief"))
        for a, b in zip(full["records"], brief["records"]):
            self.assertEqual(b, {k: a[k] for k in ("pmid", "pubdate", "title", "pubtype")})
        self.assertEqual(brief["returned"], full["returned"])
        self.assertTrue(brief["metadata_requested"])
        text = run(m.search, "tooth", detail="brief")
        self.assertIn("Retracted Publication/Published Erratum", text)
        self.assertIn("Complete title 22", text)
        self.assertNotIn("Author A", text)
        self.assertIn("省略不表示来源缺失", text)

    def test_ids_skips_metadata_preserves_order_and_counts(self):
        self.esummary.side_effect = AssertionError("ids must not fetch metadata")
        data = json.loads(run(m.search, "tooth", as_json=True, detail="ids"))
        self.assertEqual(data["records"], [{"pmid": "22"}, {"pmid": "11"}])
        self.assertEqual(data["returned"], 2)
        self.assertIsNone(data["esummary_missing"])
        self.assertFalse(data["metadata_requested"])
        self.assertIn("未检查撤稿", "\n".join(data["warnings"]))
        text = run(m.search, "tooth", detail="ids")
        self.assertTrue(text.endswith("22\n11"))
        self.esummary.assert_not_awaited()

    def test_all_modes_preserve_query_and_window_warnings(self):
        self.search.update(count="10001", retstart="9998", idlist=["22"], errorlist={"phrasesnotfound": ["zzzqqx"]})
        for detail in ("full", "brief", "ids"):
            for json_mode in (False, True):
                text = run(m.search, "tooth", detail=detail, as_json=json_mode)
                self.assertIn("phrasesnotfound", text)
                self.assertIn("9999", text)
                if json_mode:
                    data = json.loads(text)
                    self.assertEqual(data["querytranslation"], self.search["querytranslation"])
                    self.assertTrue(data["window_warning"])
                else:
                    self.assertIn("NCBI 实际执行", text)

    def test_brief_missing_metadata_is_not_hidden(self):
        self.esummary.return_value = self.records[:1]
        data = json.loads(run(m.search, "tooth", detail="brief", as_json=True))
        self.assertEqual(data["returned"], 1)
        self.assertEqual(data["esummary_missing"], 1)
        self.assertTrue(any("esummary" in w for w in data["warnings"]))
        text = run(m.search, "tooth", detail="brief")
        self.assertIn('本页取得 2 个 PMID', text)  # 本项目自订：两个编号只返回一条题录
        self.assertIn('展示 1 条题录', text)  # 本项目自订：不是总命中或取号数
        self.assertEqual(data['metadata_recovery']['arguments']['pmids'], '11')  # 本项目自订：只补缺号
        self.assertEqual(data['page_pmid_count'], len(self.search['idlist']))
        self.assertEqual(data['page_citation_count'], len(self.esummary.return_value))

    def test_missing_entire_page_offers_bounded_recovery(self):
        ids = self.search['idlist']
        self.esummary.return_value = []
        with patch.object(m, 'FETCH_MAX', 1):  # 本项目自订：缺题录多于补取预算
            data = json.loads(run(m.search, 'tooth', as_json=True))
            text = run(m.search, 'tooth')
        self.assertEqual(data['missing_pmids'], ids)
        self.assertEqual(data['metadata_recovery']['arguments']['pmids'], ids[0])  # 本项目自订：仅首批作为可执行调用
        self.assertIn('展示 0 条题录', text)  # 本项目自订：确有编号但整页缺题录
        self.assertIn('分批补取', text)
        ids_only = json.loads(run(m.search, 'tooth', as_json=True, detail='ids'))
        self.assertIsNone(ids_only['page_citation_count'])
        self.assertNotIn('metadata_recovery', ids_only)

    def test_zero_count_count_only_and_out_of_range_all_modes(self):
        cases = [({"count": "0", "idlist": []}, "未匹配记录"),
                 ({"count": "2", "idlist": [], "_retmax": 0}, "只要计数"),
                 ({"count": "2", "idlist": [], "retstart": "2"}, "越过了命中总数")]
        for extra, expected in cases:
            for detail in ("full", "brief", "ids"):
                self.esearch.return_value = {**self.search, **extra}
                self.esummary.return_value = []
                for json_mode in (False, True):
                    text = run(m.search, "tooth", detail=detail, as_json=json_mode)
                    self.assertIn(expected, text)
                    if json_mode:
                        self.assertEqual(json.loads(text)["returned"], 0)

    def test_quoted_zero_warning_once_per_text_response(self):
        phrase = '"missing phrase"'
        self.esearch.return_value = {**self.search, 'count': '0', 'idlist': [],  # 本项目自订：短语零命中夹具
                                     'warninglist': {'quotedphrasesnotfound': [phrase]}}
        for tool in (m.search, m.count, m.search):
            text = run(tool, 'tooth', diagnose='auto')
            self.assertEqual(text.count('quotedphrasesnotfound'), 1)  # 本项目自订：同次去重，后续调用仍提示
            self.assertEqual(text.count(phrase), 1)  # 本项目自订：执行式不含该短语
            for needed in ('主题', '去掉引号', '诊断暂缓'):
                self.assertIn(needed, text)
        data = json.loads(run(m.search, 'tooth', as_json=True))
        self.assertTrue(any(phrase in note for note in data['warnings']))
        self.assertIn(phrase, data['zero_note'])
        self.esummary.assert_not_awaited()

    def test_brief_omission_note_requires_returned_metadata(self):
        for ids in ([], self.search['idlist']):
            self.esearch.return_value = {**self.search, 'count': str(len(ids)), 'idlist': ids}
            self.esummary.return_value = []
            for json_mode in (False, True):
                text = run(m.search, 'tooth', as_json=json_mode)
                self.assertNotIn('brief 省略', text)
                if ids:
                    self.assertIn('缺失 PMID', text)

    def test_opt_in_context_preserves_page_and_literal_source(self):
        ids = self.search['idlist']
        sentence = 'Non-extraction and extraction groups were compared.'
        xml = [{'pmid': ids[-1], 'abstract': []},
               {'pmid': ids[0], 'abstract': [{'label': 'METHODS', 'text': 'Groups were defined. ' + sentence}]}]
        with patch.object(m, '_efetch_xml_chunk', AsyncMock(return_value=xml)) as fetch:
            regular = json.loads(run(m.search, 'tooth', as_json=True))
            self.assertNotIn('keyword_preview', regular)
            fetch.assert_not_awaited()
            enriched = json.loads(run(m.search, 'tooth', as_json=True, context_keyword='extraction'))
            preview = enriched.pop('keyword_preview')['by_pmid']
            self.assertEqual(enriched, regular)
            self.assertEqual(list(preview), ids)
            self.assertEqual(preview[ids[0]], dict(status='matched', source='abstract', section='METHODS',
                                                  sentence=sentence, matches=['Non-extraction', 'extraction']))
            self.assertEqual(preview[ids[-1]]['status'], 'no_abstract')
            fetch.assert_awaited_once_with(ids)
            text = run(m.search, 'tooth', context_keyword='extraction', detail='full')
            self.assertIn('[摘要 / METHODS] **Non-extraction** and **extraction** groups were compared.', text)
            self.assertIn('此记录无摘要', text)

    def test_multiple_context_sentences_preserve_first_and_disclose_overflow(self):
        pid = self.search['idlist'][0]
        first = 'Extraction and extraction were discussed.'
        sections = [{'label': 'BACKGROUND', 'text': first},
                    {'label': 'METHODS', 'text': 'Just context. Patients received non-extraction treatment. Without extraction, controls improved.'},
                    {'label': 'RESULTS', 'text': 'Extraction was reassessed. Extraction was mentioned again.'}]
        for source in ('abstract', 'other_abstract'):
            record = {'pmid': pid, 'abstract': sections if source == 'abstract' else [],
                      'other_abstracts': [{'sections': sections}]}
            with self.subTest(source=source), patch.object(m, '_efetch_xml_chunk', AsyncMock(return_value=[record])):
                preview = json.loads(run(m.search, 'tooth', as_json=True, context_keyword='extraction'))['keyword_preview']['by_pmid'][pid]
                self.assertEqual((preview['sentence'], preview['matches'], preview['source']), (first, ['Extraction', 'extraction'], source))
                self.assertEqual([(s['section'], s['sentence'], s['matches'], s['source']) for s in preview['additional_sentences']], [
                    ('METHODS', 'Patients received non-extraction treatment.', ['non-extraction'], source),
                    ('METHODS', 'Without extraction, controls improved.', ['Without extraction'], source)])
                self.assertEqual(preview['omitted_sentence_count'], 2)  # 本项目自订：合成摘要中超过预算的句数，不是匹配词次数
                text = run(m.search, 'tooth', detail='full', context_keyword='extraction')
                self.assertIn('**non-extraction** treatment.', text)
                self.assertIn('**Without extraction**, controls improved.', text)
                self.assertIn('另有 2 条匹配原句未展示', text)  # 本项目自订：合成夹具
                self.assertNotIn('was reassessed', text)

    def test_no_match_summary_keeps_citations_and_individual_unknowns(self):
        ids = ['21', '22', '23', '24', '25', '26']  # 本项目自订：两篇未匹配、一篇匹配，其余分别无摘要、缺记录、请求失败
        self.search.update(idlist=ids, count=str(len(ids)))
        self.esummary.return_value = [(pid, {'title': 'Title for ' + pid}) for pid in ids if pid != ids[1]]
        chunks = [[{'pmid': pid, 'abstract': [{'text': 'Nothing here.'}]}] for pid in ids[:2]]
        chunks.extend([[{'pmid': ids[2], 'abstract': [{'text': 'Needle here.'}]}],
                       [{'pmid': ids[3], 'abstract': []}], [], m.NcbiError('offline')])
        with patch.object(m, 'EFETCH_CHUNK', 1), patch.object(m, '_efetch_xml_chunk', AsyncMock(side_effect=chunks * 2)):  # 本项目自订：独立批次，文本与 JSON 各跑一次
            text = run(m.search, 'tooth', context_keyword='needle')
            self.assertEqual(text.count('摘要中未找到对应原句'), 1)  # 本项目自订：只出一次汇总
            self.assertIn('本页 2 篇摘要中未找到对应原句', text)  # 本项目自订：缺题录但已取摘要的 PMID 也计入
            self.assertIn(f'PMID {ids[1]}（缺题录）', text)
            for pid in ids:
                if pid != ids[1]:
                    self.assertIn('Title for ' + pid, text)
            self.assertIn('**Needle** here.', text)
            for pid, reason in zip(ids[3:], ('此记录无摘要', '未取回此记录', '摘要请求失败')):
                after_title = text.split('Title for ' + pid, 1)[1].split('Title for ', 1)[0]
                self.assertIn(reason, after_title)
            result = json.loads(run(m.search, 'tooth', context_keyword='needle', as_json=True))
            statuses = result['keyword_preview']['by_pmid']
            self.assertEqual(result['search_pmids'], ids)
            self.assertEqual([statuses[pid]['status'] for pid in ids],
                             ['no_match', 'no_match', 'matched', 'no_abstract', 'missing_record', 'fetch_failed'])

    def test_context_failure_is_local_and_not_missing_abstract(self):
        ids = self.search['idlist']
        missing, recovered = ids
        self.esummary.return_value = self.records[:1]
        other = {'pmid': recovered, 'abstract': [], 'other_abstracts': [{'sections': [
            {'text': 'Assessed without extraction in controls.', 'label': ''}]}]}
        with patch.object(m, 'EFETCH_CHUNK', 1), patch.object(m, '_efetch_xml_chunk',  # 本项目自订：按单篇批次模拟局部故障
                AsyncMock(side_effect=[m.NcbiError('offline'), [other], [], [other]])):
            data = json.loads(run(m.search, 'tooth', as_json=True, context_keyword='extraction'))
            contexts = data['keyword_preview']['by_pmid']
            self.assertEqual(data['search_pmids'], ids)
            self.assertEqual(data['missing_pmids'], [recovered])
            self.assertEqual(contexts[missing]['status'], 'fetch_failed')
            self.assertTrue(any('offline' in w for w in data['warnings']))
            self.assertEqual(contexts[recovered]['source'], 'other_abstract')
            text = run(m.search, 'tooth', context_keyword='extraction')
            self.assertIn('未取回此记录', text)
            self.assertIn(f'PMID {recovered}（缺题录）', text)
            self.assertIn('**without extraction**', text)

    def test_keyword_literal_and_empty_pages(self):
        a, b = self.search['idlist']
        with patch.object(m, '_efetch_xml_chunk', AsyncMock(return_value=[
                {'pmid': a, 'abstract': [{'text': 'Keyword C++ is literal.'}]},
                {'pmid': b, 'abstract': [{'text': 'There is no such keyword.'}]}])) as fetch:
            data = json.loads(run(m.search, 'tooth', as_json=True, context_keyword='C++'))
            self.assertEqual(data['keyword_preview']['by_pmid'][a]['matches'], ['C++'])
            self.assertEqual(data['keyword_preview']['by_pmid'][b]['status'], 'no_match')
            self.esearch.return_value = {**self.search, 'idlist': [], 'count': '0'}  # 本项目自订：空页不读摘要
            empty = json.loads(run(m.search, 'tooth', as_json=True, context_keyword='extraction'))
            self.assertEqual(empty['keyword_preview']['by_pmid'], {})
            fetch.assert_awaited_once()

    def test_context_incompatible_modes_fail_before_network(self):
        for args in ({'detail': 'ids'}, {'out_path': 'unused-context-snapshot.json'}):
            with self.assertRaisesRegex(m.NcbiError, '原句预览限'):
                run(m.search, 'tooth', context_keyword='needle', **args)
        self.esearch.assert_not_awaited()
        tool = next(t for t in run(m.mcp.list_tools) if t.name == 'search')
        self.assertEqual(tool.inputSchema['properties']['context_keyword']['default'], '')

    def test_invalid_detail_fails_before_network_direct_and_sdk(self):
        for value in ("", "summary", None, True):
            with self.subTest(value=value), self.assertRaisesRegex(m.NcbiError, "detail 只支持"):
                run(m.search, "tooth", detail=value)
            # SDK 层由 pydantic 先拦（英文原文，含字段名 detail 与允许值）。
            with self.subTest(sdk=value), self.assertRaisesRegex(m._SdkToolError, "detail"):
                run(m.mcp.call_tool, "search", {"term": "tooth", "detail": value})
        self.esearch.assert_not_awaited()

    def test_sdk_schema_and_modes(self):
        tools = run(m.mcp.list_tools)
        tool = next(t for t in tools if t.name == "search")
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema")
        self.assertEqual(schema["properties"]["detail"]["enum"], ["full", "brief", "ids"])
        self.assertEqual(schema["properties"]["detail"]["default"], "brief")
        for detail in ("full", "brief", "ids"):
            run(m.mcp.call_tool, "search", {"term": "tooth", "detail": detail})

    def test_metadata_failure_still_raises_for_brief(self):
        self.esummary.side_effect = m.NcbiError("simulated metadata failure")
        with self.assertRaisesRegex(m.NcbiError, "metadata failure"):
            run(m.search, "tooth", detail="brief")


if __name__ == "__main__":
    unittest.main()
