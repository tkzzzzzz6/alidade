"""How good is the number-provenance check? A control on saved runs.

    python eval/number_control.py eval/results/new-grounding [eval/results/old-grounding ...]

For every saved run (``logs/*.evidence.json``) this measures two things with
the *current* ``number_check.py``, without calling any model:

* **false alarms** — numbers in the real conclusions that the check cannot
  trace. The answers were spot-checked by hand, so each one is worth a look.
* **catch rate** — every number in the same conclusions is deliberately
  changed (×1.02, ×1.05, ×1.1, ×1.5, ×0.7, keeping its written precision)
  and the check is run again. A wrong number that still "has a source" slipped
  through, usually because it happens to equal another number in a big result.

A check with no false alarms but a low catch rate says nothing; both numbers
belong next to any "0% unsupported" claim.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_eval  # noqa: E402

FACTORS = (1.02, 1.05, 1.1, 1.5, 0.7)


def perturb(text: str, factor: float, nc) -> tuple[str, int]:
    """Multiply every checked number by ``factor``, written at the same precision."""
    out, changed = text, 0
    for claim in nc.extract_claims(text):
        num = re.search(r"\d[\d,]*(?:\.\d+)?", claim.text).group(0)
        decimals = len(num.split(".")[1]) if "." in num else 0
        value = float(num.replace(",", "")) * factor
        new = f"{value:,.{decimals}f}" if "," in num else f"{value:.{decimals}f}"
        if new != num:
            out = out.replace(claim.text, claim.text.replace(num, new, 1), 1)
            changed += 1
    return out, changed


def saved_runs(run_dirs: list[Path]):
    datasets, questions = run_eval.load_questions(run_eval.DEFAULT_QUESTIONS)
    by_id = {q["id"]: q for q in questions}
    demos = Path(run_eval.DEFAULT_DEMOS)
    tables: dict[str, tuple[list[float], list[float]]] = {}
    for run_dir in run_dirs:
        for f in sorted((run_dir / "logs").glob("*.evidence.json")):
            qid = re.sub(r"_\d+$", "", f.name[: -len(".evidence.json")])
            q = by_id.get(qid)
            if not q:
                continue
            key = "+".join(run_eval.dataset_keys(q))
            if key not in tables:
                tables[key] = run_eval.table_numbers(
                    [run_eval.load_table(datasets[k], demos) for k in run_eval.dataset_keys(q)])
            raw, stats = tables[key]
            ev = json.loads(f.read_text(encoding="utf-8"))
            yield f"{run_dir.name}/{f.name}", raw, [*(ev.get("given") or []), *stats], ev


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("runs", nargs="+", type=Path, help="result directories with logs/*.evidence.json")
    args = p.parse_args()
    nc = run_eval.number_checker()

    runs = list(saved_runs(args.runs))
    if not runs:
        print("No logs/*.evidence.json found (they are written by runs made after the number check was added).")
        return 1

    false_alarms, checked = [], 0
    caught = collections.Counter()
    total = collections.Counter()
    leaked: dict[float, collections.Counter] = {f: collections.Counter() for f in FACTORS}
    for name, raw, given, ev in runs:
        printed = [x for t in ev.get("printed", []) for x in nc.numbers_in(t)]
        charted = [x for t in ev.get("charts", []) for x in nc.numbers_in(t)]
        evidence = nc.Evidence(computed=charted, printed=printed, raw=raw, given=given)
        report = nc.check(ev.get("summary", ""), evidence)
        checked += report.checked
        false_alarms += [(name, c) for c in report.unsupported]
        for factor in FACTORS:
            text, _ = perturb(ev.get("summary", ""), factor, nc)
            for claim in nc.extract_claims(text):
                total[factor] += 1
                source = evidence.source_of(claim)
                if source is None:
                    caught[factor] += 1
                else:
                    leaked[factor][source] += 1

    print(f"# 数字溯源检查的对照（{len(runs)} 次运行）\n")
    print(f"- 真实结论里的数字：{checked} 个，找不到出处的：{len(false_alarms)} 个"
          f"（{len(false_alarms) / max(checked, 1):.1%}）")
    print("\n| 故意改错的倍数 | 改错的数字 | 被查出 | 查出率 | 漏掉的原因 |\n|---|---|---|---|---|")
    for factor in FACTORS:
        why = "、".join(f"{k} {v}" for k, v in leaked[factor].most_common())
        print(f"| ×{factor} | {total[factor]} | {caught[factor]} | {caught[factor] / max(total[factor], 1):.0%} | {why} |")
    if false_alarms:
        print("\n## 真实结论里找不到出处的数字\n\n| 运行 | 数字 | 上下文 |\n|---|---|---|")
        for name, c in false_alarms[:40]:
            print(f"| {name} | {c.text} | {c.context.replace('|', '/')} |")
    print("\n漏掉的原因：computed = 恰好等于另一个计算结果；derived = 恰好等于两个打印数之差/比；raw = 恰好等于原始数据的某个单元格。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
