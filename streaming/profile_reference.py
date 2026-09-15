"""Read-only audit; never 'repair' the historical fixture silently."""
import json
from pathlib import Path
from .contracts import reference, rules


def audit(data):
    issues = []
    def walk(value, path):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
        elif isinstance(value, (float, int)) and value < 0:
            issues.append({"kind": "negative_measure", "path": path, "value": value})
    walk(data, "root")
    for process, kpis in data["kpiCounts"].items():
        for key, rows in kpis.items():
            published = {r["quarter"]: r["value"] for r in data["quarterlyData"][process].get(key, {}).get("data", [])}
            for row in rows:
                if "sample_n" in row:
                    continue
                n, d = row["numerator"], row["denominator"]
                if n > d:
                    issues.append({"kind": "numerator_exceeds_denominator", "process": process, "kpi": key, **row})
                if d > 0 and row["quarter"] in published and abs(n / d * 100 - published[row["quarter"]]) > .01:
                    issues.append({"kind": "percentage_disagrees_with_counts", "process": process, "kpi": key, **row, "published": published[row["quarter"]]})
    return {"sections": list(data), "indicator_count": len(rules()), "issues": issues,
            "policy": "Reference for shape, targets and simulation scale only; not reconstructed real transactions."}


if __name__ == "__main__":
    result = audit(reference())
    out = Path("streaming/reference-audit.json")
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"indicators": result["indicator_count"], "issues": len(result["issues"]), "report": str(out)}))
