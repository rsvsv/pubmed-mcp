"""Usability/correctness regressions. All identifiers, limits and dates are 本项目自订测试口径."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import anyio

spec = importlib.util.spec_from_file_location('usability_subject', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


class SearchContext(unittest.TestCase):
    def response(self, **updates):
        result = {'_term': 'asthma', 'count': '4', 'idlist': ['22', '11'], 'retstart': '0',
                  '_retmax': 2, '_request_params': {'sort': 'relevance'}, 'querytranslation': 'asthma[All Fields]'}
        result.update(updates)
        return result

    def test_summary_order_matches_requested_ranking(self):
        payload = {'result': {'uids': ['11', '22'], '11': {'uid': '11', 'title': 'Second'}, '22': {'uid': '22', 'title': 'First'}}}
        with patch.object(m, '_get_async', AsyncMock(return_value=json.dumps(payload))):
            self.assertEqual([pid for pid, _ in run(m._esummary_dicts, ['22', '11'])], ['22', '11'])

    def test_search_exposes_sort_page_and_missing_ids_without_extra_network(self):
        summary = AsyncMock(return_value=[('22', {'title': 'First'})])
        with patch.object(m, '_esearch', AsyncMock(return_value=self.response())), patch.object(m, '_esummary_dicts', summary):
            data = json.loads(run(m.search, 'asthma', retmax=2, sort='relevance', as_json=True))
        self.assertEqual(data['sort'], 'relevance')
        self.assertEqual(data['search_pmids'], ['22', '11'])
        self.assertEqual(data['missing_pmids'], ['11'])
        self.assertEqual(data['next_retstart'], 2)
        self.assertTrue(data['has_more'])
        self.assertEqual(summary.await_count, 1)

    def test_count_only_does_not_claim_metadata_or_offer_pagination(self):
        with patch.object(m, '_esearch', AsyncMock(return_value=self.response(idlist=[], _retmax=0))):
            data = json.loads(run(m.search, 'asthma', retmax=0, as_json=True))
        self.assertFalse(data['metadata_requested'])
        self.assertIsNone(data['next_retstart'])
        self.assertTrue(data['has_more'])

    def test_last_window_does_not_offer_impossible_page(self):
        with patch.object(m, '_esearch', AsyncMock(return_value=self.response(count='10005', idlist=['22'], retstart='9998', _retmax=1))):
            data = json.loads(run(m.search, 'asthma', retstart=9998, detail='ids', as_json=True))
        self.assertIsNone(data['next_retstart'])
        self.assertTrue(data['has_more'])
        self.assertIn('9999', data['window_warning'])

    def test_complete_page_has_no_next_page(self):
        with patch.object(m, '_esearch', AsyncMock(return_value=self.response(count='2'))):
            data = json.loads(run(m.search, 'asthma', detail='ids', as_json=True))
        self.assertFalse(data['has_more'])
        self.assertIsNone(data['next_retstart'])

    def test_text_sort_context(self):
        with patch.object(m, '_esearch', AsyncMock(return_value=self.response())):
            text = run(m.search, 'asthma', detail='ids', sort='relevance')
        self.assertIn('排序：relevance', text)


class InputConvenience(unittest.TestCase):
    def test_canonical_pubmed_urls_and_mixed_ids(self):
        value = 'https://pubmed.ncbi.nlm.nih.gov/00022/?dopt=Abstract\nPMID: 11;22'
        self.assertEqual(m._parse_pmids(value), (['22', '11'], 3))

    def test_url_lookalikes_and_other_identifiers_are_rejected(self):
        for value in ('https://pubmed.ncbi.nlm.nih.gov.evil.test/22/', 'https://example.org/22/',
                      'https://pubmed.ncbi.nlm.nih.gov/?term=22', 'PMC22', '10.1000/example', '0', '000'):
            with self.subTest(value=value), self.assertRaises(m.NcbiError):
                m._parse_pmids(value)

    def test_url_list_accepts_documented_separators(self):
        for sep in (',', ';', '，', '、', '\n'):
            with self.subTest(separator=sep):
                self.assertEqual(m._parse_pmids('11' + sep + 'https://pubmed.ncbi.nlm.nih.gov/22/')[0], ['11', '22'])

    def test_url_query_or_fragment_delimiters_are_rejected_as_ambiguous(self):
        for suffix in ('?term=foo,44', '#section,44', '?term=x;44', '?term=x，44', '#x、44'):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(m.NcbiError, 'URL'):
                m._parse_pmids('https://pubmed.ncbi.nlm.nih.gov/22/' + suffix)

    def test_encoded_query_and_whitespace_separated_ids(self):
        self.assertEqual(m._parse_pmids('https://pubmed.ncbi.nlm.nih.gov/22/?term=foo%2C44 55'), (['22', '55'], 2))
        self.assertEqual(m._parse_pmids('https://pubmed.ncbi.nlm.nih.gov/22/,44'), (['22', '44'], 2))

    def test_thousands_separator_remains_ambiguous(self):
        with self.assertRaises(m.NcbiError):
            m._parse_pmids('1,234,567')


class AbstractQuality(unittest.TestCase):
    def record(self):
        return {'pmid': '22', 'source': 'article', 'title': 'Fixture', 'abstract': [{'label': '', 'text': 'Example abstract.'}],
                'pt': ['Journal Article', 'Retracted Publication'], 'comments': [{'type': 'RetractionIn', 'pmid': '33', 'ref': 'Notice'}]}

    def test_slim_projection_retains_integrity_warning(self):
        with patch.object(m, '_efetch_xml_all', AsyncMock(return_value=[self.record()])):
            data = json.loads(run(m.fetch_abstracts, '22', fields='title,abstract', as_json=True))
        self.assertNotIn('comments', data['records'][0])
        self.assertTrue(any('RetractionIn' in text and '22' in text for text in data['warnings']))
        self.assertTrue(data['abstract_requested'])

    def test_missing_abstract_status_not_claimed_when_not_requested(self):
        with patch.object(m, '_efetch_xml_all', AsyncMock(return_value=[self.record()])):
            data = json.loads(run(m.fetch_abstracts, '22', fields='title', as_json=True))
        self.assertFalse(data['abstract_requested'])

    def test_file_summary_preserves_missing_abstract_and_integrity(self):
        rec = self.record()
        rec['abstract'] = []
        with tempfile.TemporaryDirectory() as directory, patch.object(m, '_efetch_xml_all', AsyncMock(return_value=[rec])):
            data = json.loads(run(m.fetch_abstracts, '22', fields='title,abstract', as_json=True, out_path=str(Path(directory)/'abstract.json')))
            self.assertEqual(data['no_abstract'], ['22'])
            self.assertTrue(data['warnings'])
            self.assertTrue(Path(data['path']).is_file())


if __name__ == '__main__':
    unittest.main()
