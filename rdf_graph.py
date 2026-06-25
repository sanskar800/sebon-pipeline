"""RDF knowledge graph from the extracted SEBON prospectus data.

output/*.json -> rdflib Graph with a small ontology (Company / Person classes;
directorOf / shareholderOf / promoterOf / affiliatedWith / controls properties,
with inverses, a subproperty chain and a transitive 'controls') -> OWL-RL
inference (owlrl) -> output/graph.ttl + sample SPARQL queries.

People and companies are deduped to one URI per normalised name, so the same
person/company across prospectuses is one node (entity resolution).

  python rdf_graph.py
"""
import glob
import json
import re
from pathlib import Path
from urllib.parse import quote

import owlrl
from rdflib import Graph, Namespace, BNode, Literal, RDF, RDFS, OWL

OUT = Path(__file__).resolve().parent / "output"
CORP = Namespace("http://nthset.np/corp#")          # ontology terms
ENT = Namespace("http://nthset.np/id/")             # entity ids

_PFX = re.compile(r"^(श्री|श्रीमती|सुश्री|डा\.?|इन्जि\.?)\s*")
_SFX = re.compile(r"\s*(प्रा\.?\s*लि\.?|प्रा\.?\s*ली\.?|पाई\.?\s*लि\.?|लिमिटेड|कम्पनी|लि\.?|ली\.?|प्रा\.?|संस्था)\.?\s*$")


def key(name, person):
    s = (name or "").strip()
    if person:
        s = _PFX.sub("", s)
    else:
        prev = None
        while prev != s:
            prev, s = s, _SFX.sub("", s).strip()
    return re.sub(r"\s+", " ", s).strip()


def uri(name, person):
    k = key(name, person)
    return ENT[("person/" if person else "company/") + quote(k)] if len(k) >= 3 else None


def ontology(g):
    g.add((CORP.Company, RDF.type, RDFS.Class))
    g.add((CORP.Person, RDF.type, RDFS.Class))

    def op(name, inverse=None, sub=None, transitive=False):
        g.add((name, RDF.type, OWL.ObjectProperty))
        if inverse:
            g.add((inverse, RDF.type, OWL.ObjectProperty))
            g.add((name, OWL.inverseOf, inverse))
        if sub:
            g.add((name, RDFS.subPropertyOf, sub))
        if transitive:
            g.add((name, RDF.type, OWL.TransitiveProperty))

    op(CORP.directorOf, inverse=CORP.hasDirector)
    op(CORP.shareholderOf, inverse=CORP.hasShareholder)
    op(CORP.controls, transitive=True)
    op(CORP.promoterOf, inverse=CORP.hasPromoter, sub=CORP.shareholderOf)  # promoter ⇒ shareholder
    g.add((CORP.promoterOf, RDFS.subPropertyOf, CORP.controls))            # promoter ⇒ controls
    op(CORP.affiliatedWith)


def build():
    g = Graph()
    g.bind("corp", CORP)
    g.bind("id", ENT)
    ontology(g)

    def node(name, person, cls):
        u = uri(name, person)
        if u and (u, RDF.type, cls) not in g:
            g.add((u, RDF.type, cls))
            g.add((u, RDFS.label, Literal(name)))
        return u

    for f in sorted(glob.glob(str(OUT / "*.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        if "shareholders" not in d:
            continue
        co = node(d.get("company"), False, CORP.Company)
        for s in d.get("shareholders", []):
            p = node(s.get("name"), True, CORP.Person)
            if p and co:
                g.add((p, CORP.shareholderOf, co))
                if s.get("share_percent") is not None:
                    b = BNode()
                    g.add((b, RDF.type, CORP.Shareholding))
                    g.add((b, CORP.holder, p))
                    g.add((b, CORP.inCompany, co))
                    g.add((b, CORP.percent, Literal(s["share_percent"])))
        for x in d.get("directors", []):
            p = node(x.get("name"), True, CORP.Person)
            if p and co:
                g.add((p, CORP.directorOf, co))
        for a in d.get("director_affiliations", []):
            p = node(a.get("director_name"), True, CORP.Person)
            for af in a.get("affiliations", []):
                o = node(af.get("company"), False, CORP.Company)
                if p and o:
                    g.add((p, CORP.affiliatedWith, o))
        for pc in d.get("promoter_companies", []):
            pco = node(pc.get("company"), False, CORP.Company)
            if pco and co:
                g.add((pco, CORP.promoterOf, co))
            for dr in pc.get("directors", []):
                p = node(dr.get("name"), True, CORP.Person)
                if p and pco:
                    g.add((p, CORP.directorOf, pco))
    return g


QUERIES = {
    "People on >=2 company boards (cross-company connectors)": """
        SELECT ?name (COUNT(DISTINCT ?co) AS ?boards) WHERE {
            ?p corp:directorOf ?co ; rdfs:label ?name .
        } GROUP BY ?p ?name HAVING (COUNT(DISTINCT ?co) >= 2)
          ORDER BY DESC(?boards) LIMIT 10""",
    "Largest shareholdings (person -> company, %)": """
        SELECT ?person ?company ?pct WHERE {
            ?s a corp:Shareholding ; corp:holder ?h ; corp:inCompany ?c ; corp:percent ?pct .
            ?h rdfs:label ?person . ?c rdfs:label ?company .
        } ORDER BY DESC(xsd:decimal(?pct)) LIMIT 8""",
    "INFERRED corp:hasDirector (inverse of directorOf) — boards never asserted directly": """
        SELECT ?company (COUNT(?p) AS ?n) WHERE {
            ?c corp:hasDirector ?p ; rdfs:label ?company .
        } GROUP BY ?c ?company ORDER BY DESC(?n) LIMIT 5""",
    "INFERRED corp:controls (promoterOf -> controls, transitive)": """
        SELECT ?owner ?owned WHERE {
            ?x corp:controls ?y . ?x rdfs:label ?owner . ?y rdfs:label ?owned .
        } LIMIT 10""",
}


def main():
    g = build()
    g.serialize(OUT / "graph.ttl", format="turtle")     # clean asserted graph
    print(f"{len(g)} triples asserted -> output/graph.ttl")
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
    print(f"{len(g)} triples after OWL-RL inference (in-memory)\n")
    pre = f"PREFIX corp: <{CORP}> PREFIX rdfs: <{RDFS}> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
    for title, q in QUERIES.items():
        print("=== " + title + " ===")
        for row in g.query(pre + q):
            print("   " + " | ".join(str(x) for x in row))
        print()


if __name__ == "__main__":
    main()
