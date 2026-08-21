"""Enforce GPT-editor limits on the action schema (300 chars per summary/description, 700 per parameter description)."""
import sys

import yaml

spec = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "chatgpt/openapi.yaml", encoding="utf-8"))
problems = []
for path, ops in spec["paths"].items():
    for method, op in ops.items():
        for field in ("summary", "description"):
            text = op.get(field) or ""
            if len(text) > 300:
                problems.append(f"{method.upper()} {path} {field}: {len(text)} chars")
        for param in op.get("parameters") or []:
            if "$ref" in param:
                continue
            if len(param.get("description") or "") > 700:
                problems.append(f"{method.upper()} {path} param {param['name']}: {len(param['description'])} chars")
        if "{" in path:
            problems.append(f"{path}: path templates are unreliable in GPT Actions; use query parameters")
        for param in op.get("parameters") or []:
            if param.get("in") == "header":
                problems.append(f"{method.upper()} {path}: header parameters are ignored by GPT Actions")
for name, param in (spec.get("components", {}).get("parameters") or {}).items():
    if len(param.get("description") or "") > 700:
        problems.append(f"components.parameters.{name}: {len(param['description'])} chars")
ops = [op.get("operationId") for p in spec["paths"].values() for op in p.values()]
if len(ops) > 30:
    problems.append(f"{len(ops)} operations (max 30)")
print("operations:", ops)
if problems:
    print("\n".join(problems)); sys.exit(1)
print("schema within GPT editor limits")
