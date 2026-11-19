"""check_recall / links 共用的成员判定回归。

所有 PMID、日期、计数与执行式均为本项目自订测试夹具，不是真实检索结果；
来源标记沿用项目口径（本项目自订／本项目口径）。
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio

spec = importlib.util.spec_from_file_location(
    'rc_membership_subject', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def run(fn, *args, **kwargs):
    async def call():
        return await fn(*args, **kwargs)
    return anyio.run(call)


def page(qt, ids=('11',)):
    """本项目自订：单批 esearch 夹具；count 与 idlist 一致。"""
    return {'count': str(len(ids)), 'idlist': list(ids), 'querytranslation': qt, 'retstart': '0'}


def recall(term, pmids, qt, **scope):
    with patch.object(m, '_esearch', AsyncMock(return_value=page(qt))), \
            patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])):
        return json.loads(run(m.check_recall, term, pmids, True, **scope))


class TestPerformedDateUidMembership:
    def test_performed_date_and_uid_in_any_order_grouping_or_pmid_spelling(self):
        # 本项目自订：同一批 11[UID]/11[PMID] 与 2024 全年窗口，顺序/括号/拼写不同。
        executed = [
            '11[UID] AND 2024/01/01:2024/12/31[Date - Publication]',
            '2024/01/01:2024/12/31[Date - Publication] AND 11[UID]',
            '("11"[PMID]) AND (2024/01/01:2024/12/31[Date - Publication])',
        ]
        for qt in executed:
            data = recall('tooth', '11', qt, mindate='2024', maxdate='2024')
            assert data['term_ignored'] is True
            assert data['retrieved'] == ['11']
            assert data['found'] == []
            assert data['unknown'] == ['11']

    def test_true_user_uid_or_date_only_queries_are_preserved(self):
        cases = [
            ('11[uid]', '11', '11[UID] AND 11[PMID]', {}),  # 本项目自订：纯用户 UID
            ('2024[dp]', '11', '11[uid] AND 2024/01/01:2024/12/31[Date - Publication]',
             {'mindate': '2024', 'maxdate': '2024'}),  # 本项目自订：纯用户日期
            ('11', '11', '11[uid] AND 11[uid]', {'field': 'PMID'}),  # 本项目自订：PMID 字段裸号
            ('"tooth"[tiab]', '11', '"tooth"[tiab] AND 11[uid]', {}),  # 本项目自订：ASCII 引号变体
        ]
        for term, pmids, qt, scope in cases:
            data = recall(term, pmids, qt, **scope)
            assert data['term_ignored'] is False, (term, qt)
            assert data['found'] == ['11'], (term, qt)
            assert data['unknown'] == [], (term, qt)

    def test_user_date_only_term_needs_its_date_not_just_performed_uid(self):
        # 本项目自订：原式只有日期、执行式只剩本批 UID；日期条件消失不能当成「没丢主题词」。
        data = recall('2024[dp]', '11', '11[uid]')
        assert data['term_ignored'] is True
        assert data['retrieved'] == ['11']
        assert data['found'] == []
        assert data['unknown'] == ['11']

    def test_real_user_uid_inside_compound_term_is_not_a_subject_atom(self):
        # 本项目自订：用户原式自带 11[uid]，执行式里同一个号出现两次（用户条件＋本批交集）。
        data = recall('tooth[Title] AND 11[uid]', '11',
                      'tooth[Title] AND 11[uid] AND 11[UID]', field='Title')
        assert data['term_ignored'] is False
        assert data['found'] == ['11']
        assert data['unknown'] == []


class TestTaggedSubjectMembership:
    VALID = [
        ('bone[Title] AND root[Title]',
         'bone[Title] AND root[Title] AND 11[uid]'),  # 本项目自订：明确双概念
        ('bone[Title] OR root[Title]',
         '(bone[Title] OR root[Title]) AND 11[uid]'),  # 本项目自订：布尔变体 OR
        ('(bone[Title] OR root[Title]) AND (implant[Title] OR screw[Title])',
         '(bone[Title] OR root[Title]) AND (implant[Title] OR screw[Title]) AND 11[uid]'),  # 本项目自订：复合式
        ('"bone"[ti] AND "root"[Title]',
         '"bone"[Title] AND "root"[Title] AND 11[uid]'),  # 本项目自订：别名＋引号拼写
    ]

    def test_explicit_tagged_concepts_keep_membership_and_role(self):
        for term, qt in self.VALID:
            data = recall(term, '11', qt)
            assert data['found'] == ['11'], (term, qt, data['warnings'])
            assert data['unknown'] == [], (term, qt, data['warnings'])

    def test_missing_regrouped_or_negated_concept_is_unsafe_and_kept(self):
        executed = [
            'bone[Title] AND 11[uid]',  # 本项目自订：root 缺失
            '(bone[Title] OR root[Title]) AND 11[uid]',  # 本项目自订：AND 被重组为 OR
            '(bone[Title] NOT root[Title]) AND 11[uid]',  # 本项目自订：root 被否定
        ]
        for qt in executed:
            data = recall('bone[Title] AND root[Title]', '11', qt)
            assert data['retrieved'] == ['11']
            assert data['found'] == [], qt
            assert data['unknown'] == ['11'], qt
            assert any('主题概念' in warning or '布尔关系' in warning
                       for warning in data['warnings']), data['warnings']

    def test_repaired_membership_preserves_each_field_and_each_group(self):
        # 本项目自订：同一批只返回首号，用整批断言区分「实取」与「确认命中/未命中」。
        batch = '11 22'  # 本项目自订：已知集夹具
        uid = '(11[UID] OR 22[UID])'  # 本项目自订：工具附加条件
        four = '(a[ti] AND b[ti]) OR (c[ti] AND d[ti])'
        for term, echo, scope, valid in (
                (four, '(a[ti] AND c[ti]) OR (b[ti] AND d[ti])', {}, False),
                ('knee', 'hip[All Fields]', {}, False),
                ('knee', '"knee"[All Fields]', {}, True),
                (four, '(d[Title] AND c[ti]) OR (b[ti] AND a[ti])', {}, True),
                ('x[ti] NOT x[tiab]', 'x[tiab] NOT x[ti]', {}, False),
                ('x[ti] NOT x[tiab]', '"x"[Title] NOT "x"[Title/Abstract]', {}, True),
                ('a[tiab]', 'a[Title]', {}, False),
                ('a[ti]', 'a[Title/Abstract]', {'field': 'Title/Abstract'}, False),
                ('a AND b', 'a[Title] OR b[Title]', {'field': 'Title'}, False),
                ('a AND b', 'b[Title] AND a[Title]', {'field': 'Title'}, True),
                ('a[ti] OR b[ti] AND c[ti]', 'a[ti] AND b[ti] AND c[ti]', {}, False),
                ('bone AND root[ti]', '("bone"[MeSH Terms] OR "bone"[All Fields]) AND root[Title]', {}, True),
                ('root[ti]', 'root[Title] AND other[Journal]', {}, False)):
            data = recall(term, batch, f'({echo}) AND {uid}', **scope)
            assert data['retrieved'] == ['11']  # 本项目自订：实际响应
            assert data['found'] == (['11'] if valid else []), (term, echo)  # 本项目自订：确认命中
            assert [row['pmid'] for row in data['missing']] == (['22'] if valid else []), (term, echo)  # 本项目自订：确认未命中
            assert data['unknown'] == ([] if valid else batch.split()), (term, echo)

    def test_server_mapping_can_only_replace_its_own_operand(self):
        # 本项目自订：用同一个扩词表检验整支正常展开、遗漏词、反转排除与冒用外加编号。
        term = 'knee pain NOT review[pt]'
        knee = '"knee"[MeSH Terms] OR "knee joint"[All Fields]'
        pain = '"pain"[MeSH Terms] OR "pain"[All Fields]'
        mapping = [{'from': 'knee', 'to': knee}, {'from': 'pain', 'to': pain}]
        for topic, maps, ok in (
                (f'({knee}) AND ({pain}) NOT review[Publication Type]', mapping, True),
                (f'({knee}) AND ({pain}) AND review[Publication Type]', mapping, False),
                (f'({knee}) NOT review[Publication Type]', mapping, False),
                (f'({knee}) AND ({pain}) NOT review[Title]', mapping + [{'from': 'review[pt]', 'to': 'review[Title]'}], False),
                ('other[UID] NOT review[Publication Type]', [{'from': 'knee pain', 'to': 'other[UID]'}], False)):
            d = {**page(f'({topic}) AND (11[uid] OR 12[uid])'), 'translationset': maps}  # 本项目自订：本批交集与回显字段
            with patch.object(m, '_esearch', AsyncMock(return_value=d)), patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])):
                report = json.loads(run(m.check_recall, term, '11 12', True))  # 本项目自订：同批已知集
            assert report['retrieved'] == ['11']  # 本项目自订：实取保持
            assert report['found'] == (['11'] if ok else []), (topic, maps)  # 本项目自订：只确认可靠批
            assert report['unknown'] == ([] if ok else ['11', '12']), (topic, maps)  # 本项目自订：失真整批未知

    def test_explicit_filters_modifiers_and_user_identifiers(self):
        # 本项目自订：实际响应包含首号；限制被丢或反转时，未返回的号也不能算作漏检。
        batch = '11 12'  # 本项目自订：已知集
        clause = '(11[uid] OR 12[uid])'  # 本项目自订：本批交集
        for term, changed, intact in (
                ('tooth NOT review[pt]', 'tooth[All Fields] AND review[Publication Type]', 'tooth[All Fields] NOT review[Publication Type]'),
                ('review[pt]', 'review[Title]', '"review"[Publication Type]'),
                ('asthma[mh:noexp]', 'asthma[MeSH Terms]', 'asthma[MeSH Terms:noexp]'),
                ('"root pain"[tiab:~3]', '"root pain"[Title/Abstract:~2]', '"root pain"[Title/Abstract:~3]'),  # 本项目自订：邻近距离
                ('22[uid] NOT 33[uid]', '33[PMID] NOT 22[PMID]', '22[PMID] NOT 33[PMID]'),  # 本项目自订：用户编号不是本批编号
                ('knee[tw] OR hip[tw]', 'knee[Text Word] AND hip[Text Word]', 'hip[Text Word] OR knee[Text Word]'),
                ('exercise[All Fields]', 'exercise[Title]', 'exercise[all]')):
            for echo, ok in ((changed, False), (intact, True)):
                result = recall(term, batch, f'({echo}) AND {clause}')
                assert result['retrieved'] == ['11']  # 本项目自订：实取号保留
                assert result['unknown'] == ([] if ok else batch.split()), (term, echo, result['warnings'])
                assert result['found'] == (['11'] if ok else []), (term, echo)  # 本项目自订：可靠命中
                assert [r['pmid'] for r in result['missing']] == (['12'] if ok else []), (term, echo)  # 本项目自订：可靠未命中

    def test_ordinary_automatic_mapping_is_not_compared_to_tagged_skeleton(self):
        # 本项目自订：未标注词由 NCBI 自动映射/展开，不能按明确标签的布尔骨架去判失真。
        data = recall('bone AND root', '11',
                      '("bone"[MeSH Terms] OR "bone"[All Fields]) AND "root"[All Fields] AND 11[uid]')
        assert data['found'] == ['11']
        assert data['unknown'] == []

    def test_unparseable_translation_is_unknown_not_missing(self):
        # 本项目自订：执行式混用 OR/AND 且无括号，无法可靠核对；证据不足只能记为未知。
        data = recall('bone[Title] AND root[Title]', '11',
                      'bone[Title] OR root[Title] AND 11[uid]')
        assert data['retrieved'] == ['11']
        assert data['found'] == []
        assert data['unknown'] == ['11']


class TestLinksShareMembershipCheck:
    def _links(self, term, qt):
        async def elink(path, params, timeout):
            assert path == 'elink.fcgi'
            return json.dumps({'linksets': [{'ids': [params['id']], 'linksetdbs': [
                {'linkname': 'pubmed_pubmed', 'links': ['2']}]}]})  # 本项目自订：唯一边候选

        with patch.object(m, '_pmids_exist', AsyncMock(return_value={'1': 'present'})), \
                patch.object(m, '_get_async', elink), \
                patch.object(m, '_esearch', AsyncMock(return_value=page(qt, ids=('2',)))):
            return json.loads(run(m.links, '1', to='pubmed', term=term, compact=True))

    def test_links_uses_same_membership_and_keeps_retrieved_candidate(self):
        term = '(a[ti] AND b[ti]) OR (c[ti] AND d[ti])'
        for echo, expected in [('(a[Title] AND c[ti]) OR (b[ti] AND d[ti])', None),
                               ('(a[Journal] AND b[ti]) OR (c[ti] AND d[ti])', None),
                               ('(d[ti] AND c[ti]) OR (b[ti] AND a[Title])', True)]:
            result = self._links(term, f'({echo}) AND 2[UID]')  # 本项目自订：同一近邻候选
            assert [item['pmid'] for item in result['targets']] == ['2']  # 本项目自订：不因判断不可靠而丢候选
            assert result['targets'][0]['in_term'] is expected  # 本项目自订：读取唯一候选

        valid = self._links('bone[Title] AND root[Title]',
                            'bone[Title] AND root[Title] AND 2[uid]')
        assert valid['targets'][0]['in_term'] is True
        assert valid['match_summary']['unknown'] == 0

        unsafe = self._links('bone[Title] AND root[Title]',
                             '(bone[Title] OR root[Title]) AND 2[uid]')
        assert [row['pmid'] for row in unsafe['targets']] == ['2']
        assert unsafe['targets'][0]['in_term'] is None
        assert unsafe['match_summary']['unknown'] == 1
        assert any('主题概念' in warning or '布尔关系' in warning
                   for warning in unsafe['warnings'])
