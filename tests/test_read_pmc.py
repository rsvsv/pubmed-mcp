"""All XML, ids, indices and budgets in fixtures are synthetic project test cases."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio
import pytest

spec = importlib.util.spec_from_file_location('pmc_test_core', Path(__file__).resolve().parents[1] / 'pubmed_mcp.py')  # 本项目自订测试路径
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
XML = '''<pmc-articleset><article xmlns="urn:jats"><front><article-meta>
<article-id pub-id-type="pmc">42</article-id><article-id pub-id-type="pmid">17</article-id><!-- 本项目自订：合成文章编号 -->
<title-group><article-title>Example</article-title></title-group></article-meta></front>
<body><p>Opening words</p><boxed-text><caption><title>Box title</title></caption><p>Box text</p></boxed-text>
<sec id="methods"><title>Methods</title><p>Before <italic>italics</italic> x<sup>n</sup>.
<fig id="fig"><label>Fig A</label><caption><p>Caption words</p></caption><graphic/></fig> After.</p>
<sec id="child"><title>Methods</title><p>Child only</p></sec></sec></body>
<floats-group><table-wrap id="tab"><label>Table A</label><caption><title>Layout</title></caption>
<table><thead><tr><th colspan="2" id="unit">Units</th></tr></thead><tbody><tr><td headers="unit" rowspan="2">Value</td><td/></tr></tbody></table><!-- 本项目自订：合成表格跨度 -->
<table-wrap-foot><fn id="foot"><p>Footnote words</p></fn></table-wrap-foot></table-wrap>
<table-wrap id="img"><label>Table B</label><caption><p>Image table</p></caption><graphic/></table-wrap></floats-group>
<back><ref-list><ref>Outside scope</ref></ref-list></back>
</article></pmc-articleset>'''  # 本项目自订：以上编号、跨行跨列和内容仅为合成夹具
PMCID, PMID = 'PMC42', '17'  # 本项目自订测试号


@pytest.fixture(autouse=True)
def isolated_snapshots():
    core._pmc_impl._snapshots.clear()
    yield
    core._pmc_impl._snapshots.clear()


def elink(groups, **changes):
    source = {'dbfrom': 'pubmed', 'ids': [PMID], 'linksetdbs': groups, **changes}
    return json.dumps({'linksets': [source]})


def group(name='pubmed_pmc', **changes):
    return {'dbto': 'pmc', 'linkname': name, 'links': [PMCID.removeprefix('PMC')], **changes}


def run(article=PMCID, xml=XML, link=None, **kw):
    responses = [link, xml] if link is not None else [xml]
    get = AsyncMock(side_effect=responses)
    async def go():
        return json.loads(await core.read_pmc(article, **kw))
    with patch.object(core, '_get_async', get):
        value = anyio.run(go)
    return value, [c.args[:2] for c in get.await_args_list]  # 本项目口径：测试只核对请求名与参数


def lookup(key, **args):
    index, _ = run()
    sel = next(v for v in index['items'] if key in (v['kind'], v['jats_id']))
    return run(selector=sel['selector'], **args)[0]


def test_source_binding_and_outline_only():
    result, calls = run(PMID, link=elink([group('pubmed_pmc_refs', links=['81']), group()]))  # 本项目自订：无关关系号
    assert result['status'] == 'outline'
    assert [c[0] for c in calls] == ['elink.fcgi', 'efetch.fcgi']
    assert calls[0][1]['linkname'] == 'pubmed_pmc' and calls[1][1]['id'] == PMCID.removeprefix('PMC')
    assert result['article_ids'] == {'pmid': PMID, 'pmcid': PMCID}
    assert {v['kind'] for v in result['items']} == {'section', 'body', 'figure', 'table', 'box'}
    assert all(s not in json.dumps(result) for s in ['Opening words', 'Box text', 'italics', 'Caption words', 'Value', 'Footnote words', 'Outside scope'])
    direct, direct_calls = run()
    assert len(direct_calls) == 1 and direct['items'] == result['items']  # 本项目自订：直接 PMCID 不做映射


def test_sections_are_separate_and_locatable():
    assert lookup('body')['items'][0]['text'] == 'Opening words'
    assert 'Box text' in [v['text'] for v in lookup('box')['items']]
    parent = lookup('methods')['items']
    assert [v['text'] for v in parent] == ['Before italics x<sup>n</sup>. After.']
    assert parent[0]['path'].endswith('/sec[1]/p[1]')  # 本项目自订：夹具 JATS 路径
    assert lookup('child')['items'][0]['text'] == 'Child only'
    fig = lookup('fig')
    assert [v['role'] for v in fig['items']] == ['caption'] and '未读取图像' in fig['note']


def test_table_headers_spans_notes_and_image_only():
    table = lookup('tab')
    assert table['structured_cells_available'] is True
    cap, head, cell, empty, foot = table['items']
    assert cap['role'] == 'caption' and foot['role'] == 'footnote' and foot['jats_id'] == 'foot'
    assert head['role'] == 'th' and head['colspan'] == '2' and '/thead[' in head['path']  # 本项目自订夹具
    assert cell['text'] == 'Value' and cell['rowspan'] == '2' and cell['headers'] == head['id']  # 本项目自订夹具
    assert empty['role'] == 'td' and empty['text'] == ''
    img = lookup('img')
    assert not img['structured_cells_available'] and '未做 OCR' in img['note']
    assert [v['role'] for v in img['items']] == ['caption']


def test_paging_fragment_and_version_binding():
    first, _ = run(limit=1)  # 本项目自订分页夹具
    rest, _ = run(offset=first['next_offset'])
    assert first['items'] + rest['items'] == run()[0]['items'] and rest['next_offset'] is None
    key = first['items'][0]['selector']
    long = 'abc' * 600  # 本项目自订长段夹具
    updated = XML.replace('Opening words', long)
    pinned, calls = run(xml=updated, selector=key)
    assert not calls and pinned['snapshot_reused'] and pinned['items'][0]['text'] == 'Opening words'
    refreshed, calls = run(xml=updated)
    assert calls and not refreshed['snapshot_reused']
    with pytest.raises(core.NcbiError, match='版本'):
        run(xml=updated, selector=key)
    key = refreshed['items'][0]['selector']
    page, _ = run(xml=updated, selector=key, limit=1)  # 本项目自订分页夹具
    last, _ = run(xml=updated, selector=key, offset=page['next_offset'])
    assert last['next_offset'] is None and page['items'] + last['items'] == run(xml=updated, selector=key)[0]['items']
    assert ''.join(v['text'] for v in page['items'] + last['items']) == long
    assert last['items'][0]['char_start'] == len(page['items'][0]['text'])


@pytest.mark.parametrize('groups', [[], [group('pubmed_pmc_refs')]])
def test_no_direct_link_not_absent_article(groups):
    result, calls = run(PMID, link=elink(groups))
    assert result['status'] == 'no_direct_link_returned' and len(calls) == 1  # 本项目自订夹具


@pytest.mark.parametrize('link', [elink([], ids=['99']), elink([], id='99'), elink([], dbfrom='pmc'), elink([], error='bad'), '{"linksets":[]}', 'invalid json', elink([group(links=None)]), elink([group(links=['42','43'])]), elink([group(dbto='pubmed')])])  # 本项目自订：合成错误、歧义及错号
def test_malformed_mapping_is_not_a_negative(link):
    with pytest.raises(core.NcbiError):
        run(PMID, link=link)


@pytest.mark.parametrize('xml', ['<html/>', '<incomplete', XML.replace('>42<', '>43<'), XML.replace('>17<', '>18<'), XML.replace('</pmc-articleset>', '<ERROR>fetch failed</ERROR></pmc-articleset>')])  # 本项目自订：错号及非文章回包
def test_wrong_xml_rejected_before_content(xml):
    with pytest.raises(core.NcbiError):
        run(PMID, xml=xml, link=elink([group()]))


def with_body(body, *, floats=True):
    before, after = XML.split('<body>')
    _, after = after.split('</body>')
    if not floats:
        after = after.split('</floats-group>')[1]  # 本项目自订夹具：删去独立图表，保留 back
    return before + body + after


def test_container_navigation_returns_direct_selectors_not_child_text():
    body = ('<body><sec id="methods"><title>Materials and methods</title>'
            '<sec><title>Sampling</title><p>Collected samples.</p>'
            '<sec><title>Nested detail</title><p>Detail.</p></sec></sec>'
            '<sec><title>Analysis</title><p>Analysis text.</p></sec></sec></body>')
    outline, _ = run(xml=with_body(body, floats=False))
    parent = next(row for row in outline['items'] if row['jats_id'] == 'methods')
    page, calls = run(selector=parent['selector'])
    assert not calls and page['items'] == [] and page['next_offset'] is None
    expected = [{k: row[k] for k in ('kind', 'title', 'selector')} for row in outline['items']
                if row['parent'] == parent['selector']]
    assert page['children'] == expected and [row['title'] for row in expected] == ['Sampling', 'Analysis']
    assert '直属' in page['note'] and 'Collected samples.' not in json.dumps(page)
    selector = expected[0]['selector']
    actual, calls = run(selector=selector)
    assert not calls and [row['text'] for row in actual['items']] == ['Collected samples.']
    past_end, _ = run(selector=selector, offset=len(actual['items']))
    assert past_end['items'] == [] and 'children' not in past_end and 'note' not in past_end


@pytest.mark.parametrize('body,observation', [('', '没有 <body>'), ('<body/>', '<body> 中没有可读内容')])
def test_body_unavailable_separates_observation_from_unverified_fallbacks(body, observation):
    xml = with_body(body, floats=False)
    result, calls = run(xml=xml)
    assert result['status'] == 'body_unavailable' and observation in result['note']
    assert '原因未确认' in result['note'] and not result.get('source_notices')
    assert result['alternative_urls'] == {'pmc': result['pmc_url'], 'pubmed': f'https://pubmed.ncbi.nlm.nih.gov/{PMID}/'}
    assert [name for name, _ in calls] == ['efetch.fcgi']


def test_body_unavailable_quotes_metadata_and_uses_article_pdf_only():
    doi = '10.1234/example#article'  # 本项目自订：合成 DOI，用于检查 URL 转义
    pdf = 'https://publisher.example/article.pdf'
    meta = (f'<article-id pub-id-type="doi">{doi}</article-id>'
            '<article-version>Author manuscript</article-version>'
            '<custom-meta-group><custom-meta><meta-name>embargo</meta-name>'
            '<meta-value>Embargo reported by the publisher</meta-value></custom-meta></custom-meta-group>'
            f'<self-uri xmlns:xlink="http://www.w3.org/1999/xlink" content-type="pdf" xlink:href="{pdf}"/>')  # 来源：XML 使用的 XLink 命名空间标识
    xml = with_body('', floats=False).replace('</article-meta>', meta + '</article-meta>')
    result, _ = run(xml=xml)
    assert result['alternative_urls']['doi'] == 'https://doi.org/10.1234/example%23article'  # 本项目自订：合成 DOI 的解析入口
    assert result['alternative_urls']['pdf'] == pdf
    assert {(row['label'], row['text']) for row in result['source_notices']} == {
        ('article-version', 'Author manuscript'), ('embargo', 'Embargo reported by the publisher')}
    assert all(row['path'].startswith('/article/front') for row in result['source_notices'])
    assert '原因未确认' in result['note']  # 版本和访问说明都不是 XML 缺失的因果证据


def test_fallbacks_not_guessed_from_other_article_or_relative_pdf_name():
    xml = with_body('', floats=False).replace(f'<article-id pub-id-type="pmid">{PMID}</article-id>', '')
    xml = xml.replace('</article-meta>', '<self-uri content-type="pdf" href="article.pdf"/></article-meta>')
    xml = xml.replace('<ref>Outside scope', '<ref><self-uri content-type="pdf" href="https://other.example/cited.pdf"/>Outside scope')
    result, _ = run(xml=xml)
    assert result['alternative_urls'] == {'pmc': result['pmc_url']}


@pytest.mark.parametrize('body,status', [('', 'missing'), ('<body/>', 'empty'), ('<body><p> </p></body>', 'empty'),
                                         ('<body><title><italic>Heading only</italic></title></body>', 'empty')])
def test_no_body_does_not_hide_independent_floats(body, status):
    xml = with_body(body)
    first, _ = run(xml=xml, limit=1)  # 本项目自订：独立表格目录分页
    assert first['status'] == 'outline' and first['body_status'] == status
    assert [x['jats_id'] for x in first['items']] == ['tab'] and 'Value' not in json.dumps(first)
    second, _ = run(xml=xml, offset=first['next_offset'])
    assert [x['jats_id'] for x in second['items']] == ['img'] and second['next_offset'] is None
    table, _ = run(xml=xml, selector=first['items'][0]['selector'])
    assert table['body_status'] == status and table['structured_cells_available']
    assert any(v['role'] == 'td' and v['text'] == 'Value' for v in table['items'])
    img, _ = run(xml=xml, selector=second['items'][0]['selector'])
    assert not img['structured_cells_available'] and '未做 OCR' in img['note']


@pytest.mark.parametrize('body', ['', '<body/>', '<body><p/></body>'])
def test_no_body_and_no_independent_floats(body):
    result, _ = run(xml=with_body(body, floats=False))
    assert result['status'] == 'body_unavailable' and result['items'] == []


@pytest.mark.parametrize('ref_type', ['table-fn', 'fn', ''])
def test_footnote_reference_never_joins_cell_digits(ref_type):
    attr = f' ref-type="{ref_type}"' if ref_type else ''
    ref = f'<xref{attr} rid="foot">2</xref>'  # 本项目自订：数字脚注夹具，不是论文数据
    xml = XML.replace('>Value</td>', f'><bold>5</bold>{ref} mg</td>')  # 本项目自订：模拟贴着数值的脚注
    ix, _ = run(xml=xml)
    key = next(item['selector'] for item in ix['items'] if item['jats_id'] == 'tab')
    page, _ = run(xml=xml, selector=key)
    cell = next(item for item in page['items'] if item['role'] == 'td')
    assert cell['text'].startswith('5<xref') and cell['text'].endswith('>2</xref> mg')  # 本项目自订：明确区分值与标号
    assert 'rid="foot"' in cell['text'] and '52' not in cell['text']  # 本项目自订：旧错误投影
    assert any(x['role'] == 'footnote' and x['jats_id'] == 'foot' for x in page['items'])
    assert cell['block_chars'] == len(cell['text'])


def test_untyped_note_in_body_and_nested_marker_in_cell():
    xml = XML.replace('Opening words', '5<xref rid="body-note">2</xref> mg').replace(  # 本项目自订：数字脚注及单位，非论文数据
        '</body>', '<fn-group><fn id="body-note"><p>Body note.</p></fn></fn-group></body>').replace(
        '>Value</td>', '>5<sup><xref ref-type="table-fn" rid="foot">2</xref></sup> mg</td>')  # 本项目自订：带上标脚注夹具
    outline, _ = run(xml=xml)
    for key, expected, note in [('body', '5<xref rid="body-note">2</xref> mg', 'body-note'),  # 本项目自订：标号与值分开
                                ('tab', '5<sup><xref ref-type="table-fn" rid="foot">2</xref></sup> mg', 'foot')]:  # 本项目自订：嵌套标记
        sel = next(it['selector'] for it in outline['items'] if key in (it['kind'], it['jats_id']))
        result, _ = run(xml=xml, selector=sel)
        assert any(it['text'] == expected for it in result['items'])
        assert any(it['role'] == 'footnote' and it['jats_id'] == note for it in result['items'])


def test_bibliographic_xrefs_preserve_body_and_caption_boundaries():
    marked = '5<xref ref-type="bibr" rid="r2">2</xref> mg'  # 本项目自订：正文及图注的数字边界夹具
    index, _ = run(xml=XML.replace('Opening words', marked).replace('Caption words', marked))
    for kind in ('body', 'figure'):
        selector = next(x['selector'] for x in index['items'] if x['kind'] == kind)
        page, calls = run(selector=selector)
        assert not calls and [x['text'] for x in page['items']] == [marked]


def test_grouped_table_notes_match_cell_references():
    notes = '<fn-group><fn id="unit-note"><p>Units.</p></fn><fn id="scope-note"><p>Scope.</p></fn></fn-group>'
    cells = '<td><xref rid="unit-note">u</xref></td><td><xref rid="scope-note">s</xref></td>'
    table = XML.replace('<fn id="foot"><p>Footnote words</p></fn>', notes).replace('<td headers="unit" rowspan="2">Value</td><td/>', cells)  # 本项目自订：替换合成跨行单元格
    outline, _ = run(xml=table)
    selector = next(x['selector'] for x in outline['items'] if x['jats_id'] == 'tab')
    page, _ = run(selector=selector)
    notes = [x for x in page['items'] if x['role'] == 'footnote']
    assert [x['text'] for x in notes] == ['Units.', 'Scope.']
    assert [x['jats_id'] for x in notes] == ['unit-note', 'scope-note']
    assert len({x['path'] for x in notes}) == len(notes)
    for cell, foot in zip((x for x in page['items'] if x['role'] == 'td'), notes, strict=True):
        assert f'rid="{foot["jats_id"]}"' in cell['text'] and '/fn-group[' in foot['path']


@pytest.mark.parametrize('before,after', [('', ''), ('<p>Before</p>', '<p>After</p>')])
def test_display_formula_visible_in_its_own_position(before, after):
    xml = with_body(f'<body><sec>{before}<disp-formula id="eq"><graphic/></disp-formula>{after}</sec></body>', floats=False)
    outline, _ = run(xml=xml)
    selector = next(x['selector'] for x in outline['items'] if x['kind'] == 'section')
    page, _ = run(selector=selector)
    formula, = (x for x in page['items'] if x['role'] == 'disp-formula')
    assert outline['body_status'] == 'present' and '/disp-formula[' in formula['path'] and formula['jats_id'] == 'eq'
    assert [x['text'] for x in page['items']] == (['Before'] if before else []) + ['[disp-formula: not rendered]'] + (['After'] if after else [])


def test_pinned_pages_do_not_rebuild_or_rescan():
    mod = core._pmc_module
    with patch.object(mod, '_index', wraps=mod._index) as index, patch.object(mod, '_blocks', wraps=mod._blocks) as blocks:
        outline, _ = run(PMID, link=elink([group()]))
        key = next(x['selector'] for x in outline['items'] if x['jats_id'] == 'tab')
        first, calls = run(PMID, selector=key, limit=1)  # 本项目自订：连续翻页，不重查直接关联
        second, other_calls = run(PMCID, selector=key, offset=first['next_offset'], limit=1)  # 本项目自订：同一版本的下一项
        assert not calls and not other_calls and first['items'] != second['items']
        assert first['article_sha256'] == second['article_sha256'] == outline['article_sha256']
        index.assert_called_once()
        blocks.assert_called_once()


def test_expired_selector_revalidates_article_version():
    mod = core._pmc_module
    with patch.object(mod, 'time') as clock:
        clock.monotonic.return_value = 0  # 本项目自订：测试时钟起点
        index, _ = run()
        key = index['items'][0]['selector']
        clock.monotonic.return_value = mod._CACHE_SECONDS
        with pytest.raises(core.NcbiError, match='版本'):
            run(selector=key, xml=XML.replace('Opening words', 'changed'))


def test_snapshot_limits_and_article_identity():
    mod = core._pmc_module
    with patch.object(mod, '_CACHE_XML_CHARS', len(XML) - 1):  # 本项目自订：模拟超出缓存大小预算
        ix, _ = run()
        page, calls = run(selector=ix['items'][0]['selector'])
        assert calls and not page['snapshot_reused']
    other = 'PMC43'  # 本项目自订：另一个合成文章标识
    other_xml = XML.replace('>42</article-id>', '>43</article-id>')  # 本项目自订：独立编号
    with patch.object(mod, '_CACHE_ARTICLES', 1):  # 本项目自订：单快照容量
        ix, _ = run()
        key = ix['items'][0]['selector']
        with pytest.raises(core.NcbiError, match='版本'):
            run(other, selector=key, xml=other_xml)
        page, calls = run(selector=key)
        assert calls and not page['snapshot_reused']


@pytest.mark.parametrize('article,kw', [('42,43', {}), ('https://example.org', {}), (PMCID, {'offset': True}), (PMCID, {'limit': 'not an integer'}),  # 本项目自订：非法入参
                                       (PMCID, {'offset': '-1'}), (PMCID, {'offset': 2**31}), (PMCID, {'limit': '0'}),  # 本项目自订：越界偏移及空页请求
                                       (PMCID, {'limit': core._pmc_module._MAX_PAGE + 1})])  # 本项目自订：超出单页预算
def test_input_rejected_before_network(article, kw):
    get = AsyncMock(side_effect=AssertionError('network'))
    with patch.object(core, '_get_async', get), pytest.raises(core.NcbiError):
        anyio.run(lambda: core.read_pmc(article, **kw))
    get.assert_not_awaited()
