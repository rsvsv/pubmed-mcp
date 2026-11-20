"""Read selected portions of one PMC JATS article. No PDF, OCR or disk cache."""
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from itertools import islice
from threading import Lock
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import quoteattr

import anyio
from pydantic import StrictInt, StrictStr

_PAGE = 12  # 本项目自订：每页默认目录项或原文片段数
_MAX_PAGE = 40  # 本项目自订：单页条数上限
_FRAGMENT = 1200  # 本项目自订：单个原文片段字符上限，长段可接续
_CACHE_ARTICLES = 4  # 本项目自订：每个服务实例最多保留的文章快照数
_CACHE_SECONDS = 300  # 本项目自订：自获取起的复用时限，不因翻页延长
_CACHE_XML_CHARS = 2_000_000  # 本项目自订：超过此 XML 字符预算的文章仍可读，但不驻留
_KINDS = {'body': 'body', 'sec': 'section', 'boxed-text': 'box', 'fig': 'figure', 'table-wrap': 'table'}
_MEDIA = {'graphic', 'inline-graphic', 'media', 'supplementary-material', 'inline-supplementary-material', 'math', 'tex-math', 'disp-formula', 'inline-formula'}
_EXCLUDED = {'sub-article', 'response', 'ref-list', 'permissions', 'alt-text', 'long-desc'}


def _children(node, path):
    counts = Counter()
    for child in node:
        tag = child.tag
        counts[tag] += 1  # 本项目口径：同名兄弟节点按 XML 次序编号
        yield child, f'{path}/{tag}[{counts[tag]}]'


def _text(node):
    """Plain-text projection: retain inline adjacency, mark typography and omissions."""
    if node is None:
        return ''
    def parts(el):
        yield el.text or ''
        for child in el:
            tag = child.tag
            if tag in _MEDIA:
                yield f' [{tag}: not rendered] '
            elif tag in _KINDS or tag in _EXCLUDED:
                yield ' '
            elif tag == 'xref':
                attrs = ''.join(f' {key}={quoteattr(child.attrib[key])}'
                                for key in ('ref-type', 'rid') if key in child.attrib)
                yield f'<xref{attrs}>'
                yield from parts(child)
                yield '</xref>'
            elif tag in ('sub', 'sup'):
                yield f'<{tag}>'
                yield from parts(child)
                yield f'</{tag}>'
            else:
                block = tag in ('title', 'label', 'p', 'list-item', 'break', 'fn', 'def', 'term')
                if block:
                    yield ' '
                yield from parts(child)
                if block:
                    yield ' '
            yield child.tail or ''
    return ' '.join(''.join(parts(node)).split())


def _walk(node, path, *, stop=frozenset()):
    for child, loc in _children(node, path):
        if child.tag in _EXCLUDED:
            continue
        yield child, loc
        if child.tag not in stop and child.tag not in _MEDIA:
            yield from _walk(child, loc, stop=stop)


def _index(article, token, *, include_body):
    entries = {}
    counts = Counter()
    def visit(node, path, parent):
        kind = _KINDS.get(node.tag)
        if kind:
            counts[kind] += 1  # 本项目口径：各类目录项按本次 XML 遍历次序编号
            key = f'{kind}:{counts[kind]}@{token}'
            label = _text(node.find('label'))
            title = _text(node.find('title')) or _text(node.find('caption/title'))
            meta = {'selector': key, 'kind': kind, 'title': (title[:159] + '…' if len(title) > 160 else title) or label or kind,
                    'label': label, 'jats_id': node.get('id'), 'path': path, 'parent': parent}  # 本项目自订：目录标题预览，非正文
            entries[key] = (meta, node)
            parent = key
        for child, loc in _children(node, path):
            if child.tag not in _EXCLUDED and child.tag not in _MEDIA:
                visit(child, loc, parent)
    for node, loc in _children(article, '/article'):
        if node.tag == 'floats-group' or (include_body and node.tag == 'body'):
            visit(node, loc, None)
    return entries


def _footnotes(node, path):
    if (node.text or '').strip():
        yield path, node.text.strip(), {'role': 'footnote'}
    for child, loc in _children(node, path):
        if child.tag == 'fn-group':
            yield from _footnotes(child, loc)
        else:
            yield loc, _text(child), {'role': 'footnote', 'jats_id': child.get('id')}


def _blocks(meta, node):
    path, kind = meta['path'], meta['kind']
    if kind in ('figure', 'table'):
        for child, loc in _children(node, path):
            if child.tag == 'caption':
                yield loc, _text(child), {'role': 'caption'}
        if kind == 'figure':
            return
        for el, loc in _walk(node, path, stop=_KINDS.keys() | {'td', 'th', 'table-wrap-foot'}):
            if el.tag in ('td', 'th') and '/tr[' in loc:
                fields = {a: el.get(a) for a in ('id', 'scope', 'headers', 'rowspan', 'colspan') if a in el.attrib}
                yield loc, _text(el), {'role': el.tag, **fields}
            elif el.tag == 'table-wrap-foot':
                yield from _footnotes(el, loc)
        return
    # Nested sections, boxes and floats have their own selectors, even inside a paragraph.
    for el, loc in _walk(node, path, stop=_KINDS.keys() | {'p', 'title', 'label', 'list-item', 'statement', 'disp-quote', 'verse-group', 'fn'}):
        if el.tag in _KINDS or el.tag in ('label', 'title') and loc.startswith(path + '/' + el.tag + '['):
            continue
        if el.tag in _MEDIA:
            yield loc, f'[{el.tag}: not rendered]', {'role': el.tag, 'jats_id': el.get('id')}
        elif el.tag == 'fn':
            yield loc, _text(el), {'role': 'footnote', 'jats_id': el.get('id')}
        elif el.tag in ('p', 'title', 'label', 'list-item', 'statement', 'disp-quote', 'verse-group') or not len(el):
            text = _text(el)
            if text:
                yield loc, text, {'role': el.tag}


def _fragments(blocks):
    for loc, text, fields in blocks:
        for start in range(0, max(1, len(text)), _FRAGMENT):  # 本项目口径：规范化文本片段字符偏移从零计
            yield {'path': loc, **fields, 'text': text[start:start + _FRAGMENT],
                   'char_start': start, 'block_chars': len(text)}


def _page(records, offset, limit):
    end = offset + limit
    return {'items': records[offset:end], 'offset': offset, 'next_offset': end if end < len(records) else None}


def _has_body_material(node):
    if node.tag in ('fig', 'table-wrap') or node.tag in _MEDIA:
        return True
    if node.tag in _EXCLUDED or node.tag in ('title', 'label'):
        return False
    return bool((node.text or '').strip()) or any(
        _has_body_material(child) or (child.tail or '').strip() for child in node)


def _body_guidance(article, base):
    """Describe this XML, not the accessibility of a destination we haven't visited."""
    ids = base['article_ids']
    urls = {'pmc': base['pmc_url']}
    doi = ids.get('doi', '')
    if re.fullmatch(r'10\.[0-9]+/\S+', doi):  # 来源：DOI 标识前缀；这里只生成解析入口，不验证目标页
        urls['doi'] = 'https://doi.org/' + quote(doi, safe='/')
    if re.fullmatch(r'[1-9][0-9]*', ids.get('pmid', '')):
        urls['pubmed'] = f"https://pubmed.ncbi.nlm.nih.gov/{ids['pmid']}/"
    notices = []
    meta = article.find('front/article-meta')
    for el, path in _children(meta, '/article/front[1]/article-meta[1]'):  # 本项目口径：主文章元数据的 JATS 路径
        if el.tag == 'self-uri' and 'pdf' not in urls:
            href = el.get('{http://www.w3.org/1999/xlink}href') or el.get('href') or ''  # 来源：XLink 命名空间标识
            try:
                url = urlsplit(href)
                if (url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password
                        and not re.search(r'[\s\x00-\x1f\\]', href)
                        and (el.get('content-type', '').lower() in ('pdf', 'application/pdf') or url.path.lower().endswith('.pdf'))):
                    urls['pdf'] = href
            except ValueError:
                pass  # 单条不可用入口不影响正文状态
        elif el.tag == 'article-version' and _text(el):
            notices.append({'label': el.tag, 'text': _text(el), 'path': path})
        elif el.tag == 'custom-meta-group':
            for entry, loc in _children(el, path):
                label = _text(entry.find('meta-name'))
                text = _text(entry.find('meta-value'))
                if entry.tag == 'custom-meta' and label.casefold() in ('embargo', 'embargo-date', 'access-status', 'access-restriction') and text:
                    notices.append({'label': label, 'text': text, 'path': loc + '/meta-value[1]'})  # 本项目口径：标注值的原始位置
    observation = '没有 <body>' if base['body_status'] == 'missing' else '<body> 中没有可读内容'
    note = (f'本次 XML {observation}；正文未取得，原因未确认，不代表没有全文。'
            '先打开 alternative_urls.pmc 查看全文或 PDF 入口；其余链接按需核对，本工具未访问这些页面。')
    if notices:
        note += ' source_notices 是本文元数据中的说明；版本或访问标注本身不证明正文缺失的原因。'
    return {'note': note, 'alternative_urls': urls, **({'source_notices': notices} if notices else {})}


def _parse_snapshot(xml, pmcid, pmid):
    if '<!ENTITY' in xml or '�' in xml:
        raise ValueError('PMC XML 含不支持的实体声明或解码替代字符；未返回选段')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f'PMC 回包不是可解析的 JATS XML：{exc}') from None
    for el in root.iter():
        el.tag = el.tag.rsplit('}', 1)[-1]
    articles = [root] if root.tag == 'article' else root.findall('article') if root.tag == 'pmc-articleset' else []
    if len(articles) != 1 or root.find('ERROR') is not None:
        raise ValueError('PMC 未返回可唯一核对身份的文章 XML；不代表没有全文')
    article = articles[0]
    ids = {}
    for el in article.findall('front/article-meta/article-id'):
        key, val = el.get('pub-id-type'), _text(el)
        if key in ('pmc', 'pmcid'):
            key, val = 'pmcid', 'PMC' + val.removeprefix('PMC')
        if key in ('pmcid', 'pmid', 'doi', 'pmcid-ver') and val:
            if key in ids and ids[key] != val:
                raise ValueError('PMC XML 内文章标识冲突；未返回选段')
            ids[key] = val
    if ids.get('pmcid') != pmcid or (pmid is not None and ids.get('pmid') != pmid):
        raise ValueError('PMC XML 文章身份与请求号无法对应；未返回选段')
    digest = hashlib.sha256(ET.tostring(article, encoding='utf-8')).hexdigest()
    bodies = article.findall('body')
    has_body_content = any(_has_body_material(body) for body in bodies)
    body_status = 'present' if has_body_content else ('empty' if bodies else 'missing')
    base = {'article_ids': ids, 'article_title': _text(article.find('front/article-meta/title-group/article-title')),
            'pmc_url': f'https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/', 'article_sha256': digest,
            'body_status': body_status}
    entries = _index(article, digest[:16], include_body=has_body_content)  # 本项目自订：选择编号带文档指纹前缀；完整指纹随结果返回
    guidance = None if has_body_content else _body_guidance(article, base)
    return _Snapshot(base, entries, pmid, guidance)


class _Snapshot:
    def __init__(self, base, entries, linked_pmid, guidance):
        self.base, self.entries, self.linked_pmid = base, entries, linked_pmid
        self.body_guidance = guidance
        self.outline = [meta for meta, _ in entries.values()]
        self.sections = {}
        self.lock = Lock()

    def project(self, selector, offset, limit):
        if not selector:
            if not self.entries:
                return {**self.base, 'status': 'body_unavailable', 'items': [], **self.body_guidance}
            result = {**self.base, 'status': 'outline', **_page(self.outline, offset, limit)}
            if self.body_guidance:
                result.update(self.body_guidance)
                result['note'] += ' 目录仍列出可选的独立图表。'
            return result
        if selector not in self.entries:
            raise ValueError('选择编号不属于本次文章目录或原文版本已变；请重新列目录')
        meta, node = self.entries[selector]
        # Workers may page the same snapshot concurrently. Advance its iterator only once.
        with self.lock:
            if selector not in self.sections:
                details = {}
                if meta['kind'] == 'figure':
                    details['note'] = '只提供图注，未读取图像。'
                if meta['kind'] == 'table':
                    details['structured_cells_available'] = any(el.tag in ('td', 'th') and '/tr[' in loc for el, loc in _walk(node, meta['path'], stop=_KINDS.keys()))
                    if not details['structured_cells_available']:
                        details['note'] = '本表未取得结构化单元格；表题和脚注不是表内数据，未做 OCR。'
                self.sections[selector] = [], _fragments(_blocks(meta, node)), details
            indexed, remaining, details = self.sections[selector]
            need = offset + limit + 1 - len(indexed)  # 本项目自订：多看一条判定是否有续页
            if need > 0:
                indexed.extend(islice(remaining, need))
            if not indexed and meta['kind'] in ('body', 'section') and 'note' not in details:
                children = [row for row in self.outline if row['parent'] == selector]
                if children:
                    details['children'] = [{key: row[key] for key in ('kind', 'title', 'selector')}
                                           for row in children[:_MAX_PAGE]]
                    details['note'] = '本项没有直属正文片段。请从 children 选择子项，复制其完整 selector 读取。'
                    if len(children) > _MAX_PAGE:
                        details['note'] += ' 这里只列部分子项；可翻目录按 parent 找其余子项。'
                        details['children_total'] = len(children)
                else:
                    details['note'] = '本项没有可返回的直属正文片段，也没有可选子项。'
            return {**self.base, 'status': 'selection', 'selected': meta, **details, **_page(indexed, offset, limit)}


def _direct_link(data, pmid):
    sets = data.get('linksets') if isinstance(data, dict) else None
    if not isinstance(sets, list) or len(sets) != 1 or not isinstance(sets[0], dict):
        raise ValueError('ELink 未给出唯一来源；PMC 关联未知')
    source = sets[0]
    if source.get('dbfrom') != 'pubmed' or source.get('ids') != [pmid] or source.get('error') or source.get('ERROR') or ('id' in source and source['id'] not in ([pmid], pmid)):
        raise ValueError('ELink 来源未核对成功；PMC 关联未知')
    groups = source.get('linksetdbs', [])
    if not isinstance(groups, list):
        raise ValueError('ELink 关系组无效；PMC 关联未知')
    targets = set()
    for group in groups:
        if not isinstance(group, dict) or group.get('error') or group.get('ERROR') or group.get('dbto') != 'pmc' or not group.get('linkname'):
            raise ValueError('ELink 目标库或关系组未核对成功')
        if group['linkname'] != 'pubmed_pmc':
            continue
        links = group.get('links')
        if not isinstance(links, list) or not all(re.fullmatch(r'[1-9][0-9]*', str(i)) for i in links):
            raise ValueError('ELink 直接关联缺少有效目标名单')
        targets.update('PMC' + str(i) for i in links)
    if len(targets) > 1:
        raise ValueError('ELink 返回多个直接 PMCID；请核对后按 PMCID 阅读')
    return next(iter(targets), None)


DEPENDENCIES = ('_get_async', '_safe_json', '_int', '_audit', 'NcbiError')


class Implementation:
    def __init__(self, dependencies):
        self.d = dependencies
        self._snapshots = OrderedDict()

    def _cached(self, ident, selector):
        now = time.monotonic()
        for pmcid, (expires, _) in list(self._snapshots.items()):
            if now >= expires:
                del self._snapshots[pmcid]
        for pmcid, (_, snap) in self._snapshots.items():
            if ident in (pmcid, snap.linked_pmid) and selector in snap.entries:
                self._snapshots.move_to_end(pmcid)
                return snap
        return None

    async def read_pmc(self, article_id: str, selector: str = '', offset: StrictInt | StrictStr = 0, limit: StrictInt | StrictStr = _PAGE) -> str:
        """PMID/PMCID 读原文。省略 selector 刷新目录；复制 selector 选段，按 next_offset 续页。图注非图像，选段非通读。"""
        ident = article_id.strip().upper()
        if not re.fullmatch(r'(?:PMC)?[1-9][0-9]{0,19}', ident):  # 本项目自订：一次一个正整数编号，不接受 URL
            raise self.d.NcbiError('请传单个 PMID（数字）或 PMCID（PMC 加数字）')
        off = self.d._int(offset, 'offset')
        lim = self.d._int(limit, 'limit')
        if not 0 <= off <= 2**31-1:  # 本项目自订：分页索引防护，越界不改写请求
            raise self.d.NcbiError('offset 超出允许范围')
        if not 1 <= lim <= _MAX_PAGE:  # 本项目自订：单页条数须在分页预算内
            raise self.d.NcbiError(f'limit 须在 1 到 {_MAX_PAGE} 之间')  # 本项目自订：同上
        selector = selector.strip()
        snap = self._cached(ident, selector)
        reused = snap is not None
        pmid = None if ident.startswith('PMC') else ident
        try:
            pmcid = snap.base['article_ids']['pmcid'] if reused else ident
            if pmid and not reused:
                args = {'dbfrom': 'pubmed', 'db': 'pmc', 'linkname': 'pubmed_pmc', 'id': pmid, 'retmode': 'json'}
                pmcid = _direct_link(self.d._safe_json(await self.d._get_async('elink.fcgi', args, 25), 'elink'), pmid)  # 本项目自订：复用检索传输超时口径
            if pmcid is None:
                result = {'status': 'no_direct_link_returned', 'pmid': pmid, 'items': [], 'note': '本次未返回文章自己的 PMC 关联；未核实题录存在性，不代表没有全文。'}
            else:
                if snap is None:
                    args = {'db': 'pmc', 'id': pmcid[3:], 'retmode': 'xml'}  # 来源：NCBI EFetch 的 PMC 数字号请求
                    xml = await self.d._get_async('efetch.fcgi', args, 45)  # 本项目自订：文章请求超时
                    snap = await anyio.to_thread.run_sync(_parse_snapshot, xml, pmcid, pmid)
                    self._snapshots.pop(pmcid, None)
                    if len(xml) <= _CACHE_XML_CHARS:
                        self._snapshots[pmcid] = (time.monotonic() + _CACHE_SECONDS, snap)
                        if len(self._snapshots) > _CACHE_ARTICLES:
                            self._snapshots.popitem(last=False)
                result = await anyio.to_thread.run_sync(snap.project, selector, off, lim)
        except ValueError as exc:
            raise self.d.NcbiError(str(exc)) from None
        result['snapshot_reused'] = reused
        self.d._audit('read_pmc', pmcid=pmcid, pmid=pmid, snapshot_reused=reused, reading_status=result['status'], returned=len(result['items']),
                      article_sha256=result.get('article_sha256'), next_offset=result.get('next_offset'))
        return json.dumps(result, ensure_ascii=False)
