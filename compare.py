import json
from pathlib import Path
import yaml

def load(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

def main():
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    cur = load(cfg["output"]["current"])
    prev = load(cfg["output"]["previous"])

    C = {x["listing_id"]: x for x in cur if x.get("listing_id")}
    P = {x["listing_id"]: x for x in prev if x.get("listing_id")}

    price_changes = []
    for k in C.keys() & P.keys():
        old, new = P[k], C[k]
        if old.get("price_krw_raw") != new.get("price_krw_raw"):
            price_changes.append({"before": old, "after": new})

    changes = {
        "new": [C[k] for k in C.keys() - P.keys()],
        "removed": [P[k] for k in P.keys() - C.keys()],
        "price_changed": price_changes,
        "note": "Deletion means the listing disappeared from the observed public result; it does not prove the property was sold."
    }

    Path(cfg["output"]["changes"]).write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(cfg["output"]["previous"]).write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
