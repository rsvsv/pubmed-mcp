"""一次性探针：逐个字段标签实测 NCBI 是认还是静默回退成 All Fields。
用法：python -I tag_probe.py <pubmed_mcp.py 所在目录> <输出 json>
判据：qt 与裸词 qt 逐字相同 → fallback（标签被吞）；否则 recognized。"""
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
import pubmed_mcp as m

WORD = "miniscrew"
TAGS = """ad affiliation all all fields aid article identifier au author auid author identifier book
cois conflict of interest statement cn corporate author crdt create date dcom completion date
rn ec/rn number ed editor edat entry date filter sb subset 1au first author first author name fau full author name
fir full investigator name gr grant grants and funding grant number ir investigator isbn ip issue
ta journal jour la language lang lastau last author last author name lid location id mhda mesh date
majr mesh major topic sh mesh subheadings subheading mh mesh mesh terms mesh term lr modification date jid nlm unique id
ot other term other terms owner pg pagination ps personal name as subject pa pharmacological action
pl place of publication pmc pmcid pmid uid dp pdat publication date date - publication date of publication
pt ptyp publication type pubn publisher si secondary source id nm supplementary concept substance name
tw text word text words ti title tiab title/abstract tt transliterated title vi volume ab abstract doi issn
keyword keywords kw mh:noexp majr:noexp sh:noexp pt:noexp mesh:noexp ti:noexp tiab:noexp
date - entry date - create date - completion date - modification date - mesh epdat ppdat
tiabb tiabs tibab zzzbogus title / abstract""".split("\n")


def items():
    # 多词标签按已知全称拆：以已知短标签为锚手工列表更稳，这里直接列出
    raw = " ".join(TAGS)
    multi = ["all fields", "article identifier", "author identifier", "conflict of interest statement",
             "corporate author", "create date", "completion date", "ec/rn number", "entry date",
             "first author", "first author name", "full author name", "full investigator name",
             "grants and funding", "grant number", "last author", "last author name", "location id",
             "mesh date", "mesh major topic", "mesh subheadings", "mesh terms", "mesh term",
             "modification date", "nlm unique id", "other term", "other terms", "personal name as subject",
             "pharmacological action", "place of publication", "publication date", "date - publication",
             "date of publication", "publication type", "secondary source id", "supplementary concept",
             "substance name", "text word", "text words", "transliterated title", "title/abstract",
             "date - entry", "date - create", "date - completion", "date - modification", "date - mesh",
             "title / abstract"]
    for t in multi:
        raw = raw.replace(t, " ")
    singles = [t for t in raw.split() if t not in ("-", "/")]
    seen = []
    for t in multi + singles:
        if t not in seen:
            seen.append(t)
    return seen


async def main():
    base = await m._esearch(WORD, retmax=0)
    bqt, bc = base.get("querytranslation"), base.get("count")
    out = {"bare": {"qt": bqt, "count": bc}, "tags": {}}
    for t in items() + ["TIAB", "Title/Abstract", "MeSH Terms", "Mesh Terms"]:
        try:
            d = await m._esearch(f"{WORD}[{t}]", retmax=0)
            qt, c = d.get("querytranslation"), d.get("count")
            w = d.get("warninglist") or {}
            verdict = "fallback" if (qt == bqt) else "recognized"
            out["tags"][t] = {"verdict": verdict, "qt": qt, "count": c, "warn": w}
        except Exception as e:
            out["tags"][t] = {"verdict": "error", "err": str(e)[:200]}
    # 子字段：引号短语
    for t in ["tiab:~3", "ti:~3", "ad:~3", "au:~3", "mh:~3", "tiab:~0"]:
        d = await m._esearch(f'"root resorption"[{t}]', retmax=0)
        out["tags"]["PHRASE " + t] = {"qt": d.get("querytranslation"), "count": d.get("count"),
                                      "warn": d.get("warninglist")}
    for t in ["mh", "mh:noexp", "majr:noexp", "sh:noexp", "pt:noexp", "ti:noexp", "tiab:noexp", "mh:noex"]:
        term = "Root Resorption[%s]" % t if not t.startswith(("pt", "sh")) else (
            "review[%s]" % t if t.startswith("pt") else "diagnosis[%s]" % t)
        d = await m._esearch(term, retmax=0)
        out["tags"]["SUB " + term] = {"qt": d.get("querytranslation"), "count": d.get("count"),
                                      "warn": d.get("warninglist")}
    # 布尔/日期/组标签核对
    for term in ["tooth resorption OR NOT CBCT", "tooth resorption OR CBCT", "tooth resorption NOT CBCT",
                 "aligner OR CBCT AND miniscrew", "miniscrew AND 2020-2024[Date - Publication]",
                 "miniscrew AND 2024:2020[dp]", "miniscrew AND 2020:2024[dp]",
                 "(miniscrew OR aligner)[All Fields]", "(miniscrew OR aligner)", "(miniscrew OR aligner)[mh]",
                 '"Malocclusion"[mh]', '"Tooth Crowding"[mh]', "Tooth Crowding[mh]",
                 "miniscrew[dp]", "2020[dp] AND miniscrew", "*screw[tiab]", "screw[tiab]",
                 "miniscrew AND hasabstract", "miniscrew AND english[lang]", "miniscrew[ti] AND 2020/06:2020/01[dp]"]:
        d = await m._esearch(term, retmax=0)
        out["tags"]["Q " + term] = {"qt": d.get("querytranslation"), "count": d.get("count"),
                                    "warn": d.get("warninglist")}
    json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("requests", m._NREQ)


asyncio.run(main())
