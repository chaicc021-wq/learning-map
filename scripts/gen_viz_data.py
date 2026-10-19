#!/usr/bin/env python3
"""Generate viz/data.js from upstream Marble Skill Taxonomy JSON (compact, browser-ready)."""
import json, pathlib

root = pathlib.Path(__file__).resolve().parents[1]
topics_raw = json.load(open(root / "data" / "topics.json", encoding="utf-8"))["topics"]
deps_raw = json.load(open(root / "data" / "dependencies.json", encoding="utf-8"))["dependencies"]

topics = []
for t in topics_raw:
    topics.append({
        "id": t["id"],
        "s": t["subject"],
        "d": t.get("domain") or "",
        "n": t["name"],
        "desc": t.get("description") or "",
        "a0": t.get("ageRangeStart"),
        "a1": t.get("ageRangeEnd"),
        "ty": t.get("type") or "",
        "c": round(t.get("centrality") or 0, 4),
        "ev": t.get("evidence") or [],
        "ap": t.get("assessmentPrompt") or "",
        "st": [x if isinstance(x, str) else (x.get("code") or "")
               for x in (t.get("standards") or []) if x],
    })

deps = [{"f": d["topicId"], "p": d["prerequisiteId"],
         "h": 1 if d.get("strength") == "hard" else 0,
         "r": d.get("reason") or ""} for d in deps_raw]

out = "window.TAXONOMY_DATA=" + json.dumps(
    {"topics": topics, "deps": deps,
     "meta": {"topics": len(topics), "deps": len(deps),
              "source": "Marble Skill Taxonomy v1 (withmarbleapp/os-taxonomy)",
              "license": "ODbL-1.0 + CC BY-SA 4.0"}},
    ensure_ascii=False, separators=(",", ":")) + ";"

viz = root / "viz"
viz.mkdir(exist_ok=True)
(viz / "data.js").write_text(out, encoding="utf-8")
print("wrote viz/data.js:", len(out), "bytes;", len(topics), "topics;", len(deps), "deps")
