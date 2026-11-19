"""候选版范围与展示回归；编号、日期、计数和位置均为本项目自订测试夹具。"""
import json
from unittest.mock import AsyncMock, patch

from test_gap_assistance import m, response, run


def test_protected_type_subfield():
    term = 'bone[tiab] AND root[tiab] AND review[pt:noexp]'
    with patch.object(m, '_esearch', AsyncMock(side_effect=lambda t, *a, **kw: response(t))):
        result = json.loads(run(m.search, term, as_json=True))
    assert result['diagnostics']['protected_blocks'] == ['review[pt:noexp]']
    assert all('review[pt:noexp]' in row['term'] for row in result['diagnostics']['rows'])


def test_relative_diagnostic_delta_requires_same_days():
    term = 'bone[tiab] AND root[tiab]'
    original = '2024/01/01:2024/01/31[Date - Entry]'  # 本项目自订：父窗口
    moved = '2024/01/02:2024/02/01[Date - Entry]'  # 本项目自订：跨日子窗口
    for child_window, expected in ((original, 'ok'), (moved, 'unreliable_query')):
        def reply(t, *args, **kwargs):
            return response(t + ' AND ' + (original if t == term else child_window))
        with patch.object(m, '_esearch', AsyncMock(side_effect=reply)):
            rows = json.loads(run(m.search, term, reldate=30, datetype='edat', as_json=True))['diagnostics']['rows']  # 本项目自订：相对天数
        assert rows and all(row['status'] == expected for row in rows)
        assert all((row['delta'] is None) == (child_window != original) for row in rows)


def test_search_wire_date_claims_require_echo():
    fixed = '2024/01/01:2024/12/31[Date - Publication]'  # 本项目自订：固定窗口
    relative = '2024/01/01:2024/01/31[Date - Publication]'  # 本项目自订：相对窗口
    for qt, ok in ((f'tooth AND {fixed} AND {relative}', True), (f'tooth AND {fixed}', False),
                   (f'tooth AND ({relative} OR {fixed})', False), ('tooth', False)):
        wire = json.dumps({'esearchresult': response(qt)})
        with patch.object(m, '_get_async', AsyncMock(return_value=wire)):
            text = run(m.count, 'tooth', mindate='2024', maxdate='2024', reldate=30)  # 本项目自订：混合请求
        assert ('回显核实：两段 AND' in text) == ok
        assert ('请求日期范围未核实' in text) != ok


def test_duplicate_internal_dates_without_parameters():
    day = '2024/01/01[Date - Publication]'  # 本项目自订：重复日期
    data = response(f'tooth AND {day} AND {day}')
    assert '同一日期窗口出现了多次' in '\n'.join(m._note(data))
    assert '同一日期窗口出现了多次' not in '\n'.join(m._note(response(f'tooth AND ({day} OR {day})')))


def test_corporate_author_alias_roundtrip():
    for spelling in ('corporated author', 'Corporate Author'):
        canon = m._check_field(spelling)
        assert m._check_field(canon) == canon


def test_explicit_membership_agrees_on_fields_and_long_boolean_groups():
    terms = [f'concept{n}[Title]' for n in range(12)]  # 本项目自订：长表达式超过旧真值枚举预算
    uid = '(11[UID] OR 22[UID])'  # 本项目自订：已知集
    base = ' AND '.join(terms)
    for term, echoed, valid in [
        (base, f'({base}) AND {uid}', True),
        (base, '(' + ' OR '.join(terms) + ') AND ' + uid, False),
        ('bone[Title] AND root[Title]', 'bone[Title] AND root[Journal] AND '+uid, False),
        ('2024[dp]', uid, False),  # 本项目自订：仅日期的原式也不能整个消失
        ('bone[Title] AND (2024[dp] OR root[Title])', 'bone[Title] AND '+uid, False),  # 本项目自订：整支概念消失
    ]:
        args = ('11 22',)  # 本项目自订：已知集
        with patch.object(m, '_esearch', AsyncMock(return_value=response(echoed, '1', ['11']))), patch.object(m, '_esummary_dicts', AsyncMock(return_value=[])):  # 本项目自订：控制实取首号
            report = json.loads(run(m.check_recall, term, *args, as_json=True))
        assert report['retrieved'] == ['11']  # 本项目自订：原实取保持
        assert bool(report['found']) == valid, (term, echoed, report['warnings'])
        assert bool(report['unknown']) != valid


def test_compact_conflicting_source_orders_preserve_absolute_positions():
    entries = [('a', ['x', 'y']), ('b', ['y', 'x'])]
    sources = [dict(source=p, source_status='present', relation_status='available', error=None,
                    page_positions=dict(first=3, last=4, returned=2), target_ids=targets)  # 本项目自订：来源分页范围
               for p, targets in entries]
    page = dict(sources=sources, counts={'per_source': {'a': 4, 'b': 4}}, to='refs', pmids=['a','b'],  # 本项目自订：每源总数
                pagination={'offset': 2, 'limit': 2}, warnings=[])  # 本项目自订：翻页参数
    targets = m._tools_module._compact_links(page)['targets']
    for p, expected in entries:
        recovered = sorted(targets, key=lambda t: t['source_positions'][p])
        assert [t['pmid'] for t in recovered] == expected
        assert [t['source_positions'][p] for t in recovered] == [3, 4]  # 本项目自订：绝对位置
