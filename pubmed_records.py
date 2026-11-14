"""Pure XML record parsing and text/RIS formatting. No network or file operations.
Moved rules and numeric literals retain their original source/policy annotations.
"""
import re


def _xml_text(el):
    """元素内全部文字（含内联 <i>/<sup> 等子元素文字），空白压成单空格。"""
    if el is None:
        return ""
    return " ".join("".join(el.itertext()).split())



def _xml_sections(abstract_el):
    if abstract_el is None:
        return []
    out = []
    for t in abstract_el.findall("AbstractText"):
        label = t.get("Label") or ""
        text = _xml_text(t)
        if text or label:
            out.append({"label": label, "text": text})
    return out


def _xml_authors(author_list):
    out = []
    for a in (author_list.findall("Author") if author_list is not None else []):
        coll = _xml_text(a.find("CollectiveName"))
        if coll:
            out.append({"collective": coll})
            continue
        rec = {"last": _xml_text(a.find("LastName")), "fore": _xml_text(a.find("ForeName")),
               "initials": _xml_text(a.find("Initials"))}
        orcid = [_xml_text(i) for i in a.findall("Identifier") if (i.get("Source") or "").upper() == "ORCID"]
        if orcid:
            rec["orcid"] = orcid[0]
        out.append(rec)
    return out


def _xml_pubdate(pd):
    if pd is None:
        return ""
    md = _xml_text(pd.find("MedlineDate"))
    if md:
        return md
    return " ".join(x for x in (_xml_text(pd.find("Year")), _xml_text(pd.find("Month")),
                                _xml_text(pd.find("Day"))) if x)


def _xml_ids(id_list):
    ids = {}
    for i in (id_list.findall("ArticleId") if id_list is not None else []):
        t = (i.get("IdType") or "").lower()
        if t in ("doi", "pmc", "pii", "bookaccession") and t not in ids:
            ids[t] = _xml_text(i)
    return ids


def _xml_record(node):
    """PubmedArticle / PubmedBookArticle → 记录字典（全部字段；按 fields 投影在外面做）。"""
    if node.tag == "PubmedBookArticle":
        bd = node.find("BookDocument")
        book = bd.find("Book") if bd is not None else None
        title = _xml_text(bd.find("ArticleTitle")) or _xml_text(book.find("BookTitle") if book is not None else None)
        authors = _xml_authors(bd.find("AuthorList"))
        if not authors and book is not None:
            authors = _xml_authors(book.find("AuthorList"))
        return {
            "pmid": _xml_text(bd.find("PMID")), "source": "book", "title": title,
            "abstract": _xml_sections(bd.find("Abstract")), "authors": authors,
            "journal": {"book": _xml_text(book.find("BookTitle")) if book is not None else ""},
            "pubdate": _xml_pubdate(book.find("PubDate")) if book is not None else "",
            "ids": _xml_ids(bd.find("ArticleIdList")),
            "pt": [_xml_text(p) for p in bd.findall("PublicationType")],
            "lang": [_xml_text(x) for x in bd.findall("Language")],
            "mesh": [], "comments": [], "coi": "", "other_abstracts": [],
            "_issn": [], "_artdate": "", "_keywords": [_xml_text(k) for k in bd.findall("KeywordList/Keyword") if _xml_text(k)],
            "_publisher": " ".join(x for x in (_xml_text(book.find("Publisher/PublisherName")) if book is not None else "",) if x),
        }
    mc = node.find("MedlineCitation")
    art = mc.find("Article")
    j = art.find("Journal")
    ji = j.find("JournalIssue") if j is not None else None
    eloc = {(e.get("EIdType") or "").lower(): _xml_text(e) for e in art.findall("ELocationID")}
    ids = _xml_ids(node.find("PubmedData/ArticleIdList"))
    for k in ("doi", "pii"):
        if k not in ids and eloc.get(k):
            ids[k] = eloc[k]
    mesh = []
    for h in mc.findall("MeshHeadingList/MeshHeading"):
        dn = h.find("DescriptorName")
        quals = [_xml_text(q) for q in h.findall("QualifierName")]
        major = (dn.get("MajorTopicYN") == "Y") or any(q.get("MajorTopicYN") == "Y" for q in h.findall("QualifierName"))
        mesh.append(_xml_text(dn) + "".join("/" + q for q in quals) + ("*" if major else ""))
    comments = []
    for c in mc.findall("CommentsCorrectionsList/CommentsCorrections"):
        comments.append({"type": c.get("RefType") or "", "ref": _xml_text(c.find("RefSource")),
                         "pmid": _xml_text(c.find("PMID")) or None})
    other = []
    for o in mc.findall("OtherAbstract"):
        secs = _xml_sections(o)
        if secs:
            other.append({"type": o.get("Type") or "", "language": o.get("Language") or "", "sections": secs})
    return {
        "pmid": _xml_text(mc.find("PMID")), "source": "article", "title": _xml_text(art.find("ArticleTitle"))
        or _xml_text(art.find("VernacularTitle")),
        "abstract": _xml_sections(art.find("Abstract")), "authors": _xml_authors(art.find("AuthorList")),
        "journal": {"title": _xml_text(j.find("Title")) if j is not None else "",
                    "iso": _xml_text(j.find("ISOAbbreviation")) if j is not None else "",
                    "volume": _xml_text(ji.find("Volume")) if ji is not None else "",
                    "issue": _xml_text(ji.find("Issue")) if ji is not None else "",
                    "pages": _xml_text(art.find("Pagination/MedlinePgn")),
                    "article_number": "" if _xml_text(art.find("Pagination/MedlinePgn")) else eloc.get("pii", "")},
        "pubdate": _xml_pubdate(ji.find("PubDate") if ji is not None else None),
        "ids": ids,
        "pt": [_xml_text(p) for p in art.findall("PublicationTypeList/PublicationType")],
        "lang": [_xml_text(x) for x in art.findall("Language")],
        "mesh": mesh, "comments": comments, "coi": _xml_text(mc.find("CoiStatement")),
        "other_abstracts": other,
        # 以下只给 RIS 用（不在 ABSTRACT_FIELDS 里，fetch_abstracts 输出不受影响）
        "_issn": [(_xml_text(x), x.get("IssnType") or "") for x in ([j.find("ISSN")] if j is not None else []) if x is not None],
        "_artdate": "/".join(x for x in (_xml_text(art.find("ArticleDate/Year")), _xml_text(art.find("ArticleDate/Month")),
                                         _xml_text(art.find("ArticleDate/Day"))) if x),
        "_keywords": [_xml_text(k) for k in mc.findall("KeywordList/Keyword") if _xml_text(k)],
        "_publisher": "",
    }



def _project(rec, fields):
    out = {"pmid": rec["pmid"]}
    if rec.get("source") == "book":
        out["source"] = "book"
    for f in fields:
        out[f] = rec[f]
    if "authors" in fields:
        # JSON 作者压成「姓 首字母」字符串（团体作者原样）；逐字段对象体积约为正文的 1/4〔实测 2026-11-13，60 篇〕。
        out["authors"] = [a["collective"] if "collective" in a else (a["last"] + " " + a["initials"]).strip() or a["fore"]
                          for a in rec["authors"]]
    if "abstract" in fields:
        out["has_abstract"] = bool(rec["abstract"])
    return out


def _fmt_authors(authors, limit=6):
    names = []
    for a in authors:
        if "collective" in a:
            names.append(a["collective"])
        else:
            names.append((a["last"] + " " + a["initials"]).strip() or a["fore"])
    s = ", ".join(names[:limit])
    if len(names) > limit:
        s += f"，共 {len(names)} 人"
    return s


def _fmt_compact(rec, fields):
    """紧凑文本：一条记录一块，只含 fields 指定的内容；不含作者单位。"""
    j = rec["journal"]
    src = j.get("book") or j.get("iso") or j.get("title") or ""
    cite = ""
    if j.get("volume") or j.get("pages") or j.get("article_number"):
        cite = " " + j.get("volume", "") + (f"({j['issue']})" if j.get("issue") else "") + \
               (":" + (j.get("pages") or j.get("article_number")) if (j.get("pages") or j.get("article_number")) else "")
    head = [f"PMID {rec['pmid']}"]
    if "pubdate" in fields and rec["pubdate"]:
        head.append(rec["pubdate"])
    if "journal" in fields and src:
        head.append(src + cite)
    if "pt" in fields:
        types = [p for p in rec["pt"] if p != "Journal Article"]
        if types:
            head.append("/".join(types))
    if "lang" in fields and rec["lang"] and rec["lang"] != ["eng"]:
        head.append("语言 " + "/".join(rec["lang"]))
    lines = ["｜".join(head)]
    if "title" in fields:
        lines.append("  " + (rec["title"] or "(无标题)"))
    if "authors" in fields and rec["authors"]:
        lines.append("  " + _fmt_authors(rec["authors"]))
    if "ids" in fields and rec["ids"]:
        lines.append("  " + "｜".join(f"{k}:{v}" for k, v in rec["ids"].items() if k in ("doi", "pmc")))
    if "comments" in fields:
        key = [c for c in rec["comments"] if c["type"] in ("RetractionIn", "RetractionOf", "ExpressionOfConcernIn",
                                                            "ErratumIn", "UpdateIn", "UpdateOf")]
        for c in key:
            lines.append(f"  ⚠️ {c['type']}：{c['ref']}" + (f"（PMID {c['pmid']}）" if c["pmid"] else ""))
    if "abstract" in fields:
        if rec["abstract"]:
            for s in rec["abstract"]:
                lines.append("  " + (f"[{s['label']}] " if s["label"] else "") + s["text"])
        else:
            lines.append("  （此记录无摘要；有记录不等于有摘要）")
    if "other_abstracts" in fields:
        for o in rec["other_abstracts"]:
            lines.append(f"  [其它摘要 {o['type']}/{o['language']}] " + " ".join(s["text"] for s in o["sections"]))
    if "mesh" in fields and rec["mesh"]:
        lines.append("  MeSH：" + "; ".join(rec["mesh"]))
    if "coi" in fields and rec["coi"]:
        lines.append("  利益冲突：" + rec["coi"])
    return "\n".join(lines)



_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _ris_date(pubdate):
    """PubDate 文本（'2024 Nov 13' / '2024 Nov-Dec' / '2024'）→ RIS DA 'YYYY/MM/DD/'（缺位留空）。"""
    m = re.match(r"(\d{4})(?:\s+([A-Za-z]{3}))?(?:\s+(\d{1,2}))?", pubdate or "")
    if not m:
        return "", ""
    mo = _MONTHS.get((m[2] or "").title(), 0)
    return m[1], f"{m[1]}/{mo:02d}/{int(m[3]):02d}/" if (mo and m[3]) else (f"{m[1]}/{mo:02d}//" if mo else f"{m[1]}///")


def _ris_block(r):
    """一条 XML 记录 → RIS 文本块（含 ER）。只写非空字段；作者为「姓, 名」全名，团体作者原样。"""
    L = []

    def tag(t, v):
        v = " ".join(str(v or "").split())
        if v:
            L.append(f"{t}  - {v}")

    book = r.get("source") == "book"
    tag("TY", "CHAP" if book else "JOUR")
    tag("TI", r["title"])
    for a in r["authors"]:
        if "collective" in a:
            tag("AU", a["collective"])
        else:
            tag("AU", a["last"] + (", " + a["fore"] if a["fore"] else (", " + a["initials"] if a["initials"] else "")))
    j = r["journal"]
    if book:
        tag("T2", j.get("book"))
        tag("PB", r.get("_publisher"))
    else:
        tag("T2", j.get("title"))
        tag("JO", j.get("iso"))
        tag("VL", j.get("volume"))
        tag("IS", j.get("issue"))
        pg = j.get("pages") or ""
        if pg:
            sp, _, ep = pg.partition("-")
            if ep and ep.isdigit() and sp.isdigit() and len(ep) < len(sp):
                ep = sp[:len(sp) - len(ep)] + ep      # MEDLINE 缩写页码 637-41 → 637-641
            tag("SP", sp)
            tag("EP", ep)
        else:
            tag("SP", j.get("article_number"))        # 无页码文章用文章号（ELocationID pii）
        for num, kind in r.get("_issn", []):
            tag("SN", num)
    py, da = _ris_date(r["pubdate"])
    tag("PY", py or (r["pubdate"] or "")[:4])
    tag("DA", da)
    for lang in r["lang"]:
        tag("LA", lang)
    tag("DO", r["ids"].get("doi"))
    tag("AN", r["pmid"])
    tag("C2", r["ids"].get("pmc"))
    tag("UR", f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/")
    if r["abstract"]:
        tag("AB", " ".join((s["label"] + ": " if s["label"] else "") + s["text"] for s in r["abstract"]))
    for k in r["mesh"]:
        tag("KW", k)
    for k in r.get("_keywords", []):
        tag("KW", k)
    for t in r["pt"]:
        tag("M3", t)
    for c in r["comments"]:
        if c["type"] in ("RetractionIn", "RetractionOf", "ExpressionOfConcernIn", "ErratumIn", "UpdateIn"):
            tag("N1", f"{c['type']}: {c['ref']}" + (f" (PMID {c['pmid']})" if c["pmid"] else ""))
    L.append("ER  - ")
    return "\n".join(L)
