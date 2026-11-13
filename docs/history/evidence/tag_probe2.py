import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
import pubmed_mcp as m
W = "miniscrew"
TAGS = ["All Fields","MeSH Terms","MeSH Major Topic","MeSH Subheading","Title","Title/Abstract","Text Word","Other Term",
 "Publication Type","Date - Publication","Date - Entry","Date - Create","Date - Completion","Date - Modification","Date - MeSH",
 "Language","Author","Journal","Affiliation","Conflict of Interest Statements","UID","PMID","Supplementary Concept",
 "Pharmacological Action","Filter","Subset","Grant Number","Grants and Funding","Investigator","Full Author Name",
 "First Author Name","Last Author Name","Corporate Author","Author - Identifier","Author - Full","Author - First","Author - Last",
 "Author - Corporate","Investigator - Full","Secondary Source ID","EC/RN Number","Location ID","Publisher","Place of Publication",
 "Issue","Volume","Pagination","ISBN","Book","Editor","Transliterated Title","Personal Name as Subject","NLM Unique ID","jt",
 "prop","Properties","DOI","MeSH Terms:noexp","MeSH Major Topic:noexp","MeSH Subheading:noexp","Publication Type:noexp",
 "nlm id","Date - Electronic Publication","Date - Print Publication","pmcbook","so","source","dep","ppdat","mhda",
 " tiab","tiab "," tiab ","MeSH  Terms","Title /Abstract","tiab:~ 3","Title/Abstract:~3","mh: noexp","mh :noexp",
 "Author - First:~3","sub","subh","mesh:noexp","majr:noexp","tw:~3","tiab:~100"]
async def main():
    base = await m._esearch(W, retmax=0)
    out = {"bare": base.get("querytranslation"), "tags": {}}
    for t in TAGS:
        d = await m._esearch(f"{W}[{t}]", retmax=0)
        qt = d.get("querytranslation")
        out["tags"][t] = {"verdict": "fallback" if qt == base.get("querytranslation") else "recognized", "qt": qt, "count": d.get("count")}
    for term in ["(free full text)[sb]", "free full text[sb]", "(review)[pt]", "review[pt]", "(miniscrew)[tiab]",
                 '("root resorption")[tiab]', '"root resorption"[tiab]', "(miniscrew OR aligner)[All Fields]",
                 '"root resorption"[tiab:~3]', 'root resorption[tiab:~3]', 'root resorption[tiab]',
                 "miniscrew[tiab] AND (2020:2024[dp])", "Smith J[au]", "Smith J[1au]", "orthodontics[mh] AND review[pt] OR meta-analysis[pt]",
                 "miniscrew AND 2020/01/01:2024/12/31[dp]", "miniscrew AND 2020 : 2024[dp]", "miniscrew AND 2020–2024[dp]"]:
        d = await m._esearch(term, retmax=0)
        out["tags"]["Q " + term] = {"qt": d.get("querytranslation"), "count": d.get("count"), "warn": d.get("warninglist")}
    json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("requests", m._NREQ)
asyncio.run(main())
