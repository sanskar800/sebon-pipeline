"""Build a network graph of every extracted prospectus.

Nodes: people (shareholders + directors) and companies (the prospectus company +
each affiliated company). Edges: person -> company (shareholder / director) and
person -> affiliated company. People and companies are deduped across all
prospectuses by a normalised key, so shared promoters / shared companies link
separate IPOs into one ownership web.

  python graph.py            # reads output/*.json -> output/graph.html
"""
import glob
import json
import os
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output"

_PERSON_PREFIX = re.compile(r"^(श्री|श्रीमती|सुश्री|डा\.?|इन्जि\.?)\s*")
_COMPANY_SUFFIX = re.compile(
    r"\s*(प्रा\.?\s*लि\.?|प्रा\.?\s*ली\.?|पाई\.?\s*लि\.?|लिमिटेड|कम्पनी|लि\.?|ली\.?|प्रा\.?|संस्था)\.?\s*$")


def key(name, kind):
    s = (name or "").strip()
    if kind == "person":
        s = _PERSON_PREFIX.sub("", s)
    else:
        prev = None
        while prev != s:                       # strip stacked suffixes (प्रा. लि.)
            prev, s = s, _COMPANY_SUFFIX.sub("", s).strip()
    return re.sub(r"[^ऀ-ॿa-zA-Z0-9]", "", s)


def build():
    nodes, edges, seen = {}, [], set()

    def node(name, group):
        kind = "person" if group == "person" else "company"
        nid = (kind[0] + ":" + key(name, kind))
        if len(nid) < 4:
            return None
        if nid not in nodes:
            nodes[nid] = {"id": nid, "label": name, "group": group, "deg": 0}
        elif group == "prospectus":            # prospectus company outranks affiliated
            nodes[nid].update(group="prospectus", label=name)
        return nid

    def edge(a, b, rel, title):
        if a and b and (a, b, rel) not in seen:
            seen.add((a, b, rel))
            edges.append({"from": a, "to": b, "rel": rel, "title": title})
            nodes[a]["deg"] += 1
            nodes[b]["deg"] += 1

    for f in sorted(glob.glob(str(OUT / "*.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        if "shareholders" not in d:
            continue
        c = node(d.get("company"), "prospectus")
        for s in d.get("shareholders", []):
            p = node(s.get("name"), "person")
            pct = s.get("share_percent")
            edge(p, c, "shareholder", f"शेयरधनी{f' {pct}%' if pct else ''}")
        for x in d.get("directors", []):
            edge(node(x.get("name"), "person"), c, "director", f"सञ्चालक · {x.get('position') or ''}")
        for a in d.get("director_affiliations", []):
            p = node(a.get("director_name"), "person")
            for af in a.get("affiliations", []):
                edge(p, node(af.get("company"), "affiliated"), "affiliation", af.get("role") or "आवद्ध")
        for pc in d.get("promoter_companies", []):       # corporate promoter + its own board
            cn = node(pc.get("company"), "affiliated")
            edge(cn, c, "promoter", "प्रवर्द्धक संस्था")
            for dr in pc.get("directors", []):
                edge(node(dr.get("name"), "person"), cn, "director", "सञ्चालक")
    return list(nodes.values()), edges


GROUP = {"prospectus": ("#2980b9", "IPO company"),
         "affiliated": ("#16a085", "other company"),
         "person": ("#e67e22", "promoter / director")}
REL = {"shareholder": "#9b59b6", "director": "#2980b9", "affiliation": "#16a085",
       "promoter": "#c0392b"}


def write_html(nodes, edges):
    for n in nodes:
        col = GROUP[n["group"]][0]
        n["value"] = n.pop("deg") + 1
        n["color"] = col
        n["title"] = f"{n['label']} ({GROUP[n['group']][1]}, {n['value'] - 1} links)"
    for e in edges:
        e["color"] = {"color": REL[e["rel"]], "opacity": 0.5}
    legend = " ".join(f'<span style="color:{c}">● {lbl}</span>' for c, lbl in GROUP.values())
    hubs = sorted([n for n in nodes if n["group"] == "person"], key=lambda n: -n["value"])[:8]
    hub_html = "".join(f"<li>{n['label']} — {n['value'] - 1}</li>" for n in hubs)

    (OUT / "graph.html").write_text(f"""<!doctype html><meta charset=utf-8>
<title>Prospectus ownership graph</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>html,body{{margin:0;height:100%;font-family:Segoe UI,sans-serif;background:#0e1726}}
#net{{position:absolute;inset:0}}
.panel{{position:absolute;top:12px;left:12px;background:#16213a;color:#dfe6f3;padding:12px 16px;
border-radius:10px;font-size:13px;max-width:280px;z-index:2;box-shadow:0 4px 16px #0006}}
.panel h1{{margin:0 0 6px;font-size:15px}}.panel small{{color:#8da2c0}}
.panel ol{{margin:8px 0 0;padding-left:20px;color:#cdd9ec}} .lg span{{margin-right:10px;font-size:12px}}</style>
<div id=net></div>
<div class=panel><h1>Prospectus ownership graph</h1>
<small>{len(nodes)} nodes · {len(edges)} links · people &amp; companies deduped across IPOs</small>
<div class=lg style="margin:8px 0">{legend}</div>
<b style="font-size:12px">Most-connected people</b><ol>{hub_html}</ol></div>
<script>
const nodes=new vis.DataSet({json.dumps(nodes, ensure_ascii=False)});
const edges=new vis.DataSet({json.dumps(edges, ensure_ascii=False)});
const net=new vis.Network(document.getElementById('net'),{{nodes,edges}},{{
  nodes:{{shape:'dot',scaling:{{min:6,max:42,label:{{min:10,max:26}}}},font:{{color:'#dfe6f3',size:12}}}},
  edges:{{smooth:{{type:'continuous'}},width:0.6}},
  physics:{{barnesHut:{{gravitationalConstant:-9000,springLength:130,springConstant:0.02}},stabilization:{{iterations:200}}}},
  interaction:{{hover:true,tooltipDelay:80}}}});
net.on('click',p=>{{                       // click a node -> isolate its neighbourhood
  if(!p.nodes.length){{nodes.update(nodes.get().map(n=>({{id:n.id,hidden:false}})));return;}}
  const keep=new Set([p.nodes[0],...net.getConnectedNodes(p.nodes[0])]);
  nodes.update(nodes.get().map(n=>({{id:n.id,hidden:!keep.has(n.id)}})));
}});
</script>""", encoding="utf-8")


if __name__ == "__main__":
    nodes, edges = build()
    write_html(nodes, edges)
    print(f"{len(nodes)} nodes, {len(edges)} edges -> output/graph.html")
