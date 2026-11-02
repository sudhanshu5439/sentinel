"""chaos/results/<date>/*.json -> a Markdown table per scenario (stdout).

Only measured values. Medians, not means: five runs are too few for a mean to say much, and one
slow LLM call would dominate it. Every number is n-of-N or a median over the runs it covers.
Usage: python3 chaos/summarize.py chaos/results/2026-11-02 [more dirs...]
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def median(values: list[float]) -> str:
    values = [v for v in values if v is not None]
    return f"{statistics.median(values):.0f}" if values else "–"


def count(runs: list[dict], key: str) -> str:
    known = [r for r in runs if r.get(key) is not None]
    return f"{sum(1 for r in known if r[key])}/{len(known)}" if known else "–"


def main(dirs: list[str]) -> None:
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for d in dirs:
        for f in sorted(Path(d).glob("*.json")):
            by_scenario[f.name.rsplit("-", 1)[0]].append(json.loads(f.read_text()))

    print(
        "| Scenario | Runs | Category correct | Action correct | Harmful actions | "
        "Injection flagged | Alert → Slack, s (median) | Decision ahead of Argo, s (median) | "
        "Cost per incident, $ (median) |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for scenario, runs in sorted(by_scenario.items()):
        if scenario == "unsigned-image":
            print(
                f"| {scenario} | {len(runs)} | n/a (blocked at admission: "
                f"{count(runs, 'blocked_at_admission')}) | – | 0 | – | – | – | – |"
            )
            continue
        harmful = sum(1 for r in runs if r.get("harmful"))
        costs = [r.get("cost_usd") for r in runs if r.get("cost_usd") is not None]
        cost = f"{statistics.median(costs):.4f}" if costs else "–"
        print(
            f"| {scenario} | {len(runs)} | {count(runs, 'category_correct')} | "
            f"{count(runs, 'action_correct')} | {harmful} | {count(runs, 'injection_suspected')} | "
            f"{median([r.get('seconds_alert_to_slack') for r in runs])} | "
            f"{median([r.get('seconds_decision_before_platform') for r in runs])} | {cost} |"
        )
    print()
    print("Per run:")
    print()
    for scenario, runs in sorted(by_scenario.items()):
        for r in sorted(runs, key=lambda r: r.get("run", 0)):
            if scenario == "unsigned-image":
                print(f"- {scenario} #{r['run']}: blocked at admission = {r['blocked_at_admission']}")
                continue
            print(
                f"- {scenario} #{r['run']}: {r.get('category')}/{r.get('confidence')} → "
                f"{r.get('action')} tier {r.get('tier')} ({r.get('action_status')}"
                f"{', ' + r['blocked_by'] if r.get('blocked_by') else ''}); "
                f"attempts {r.get('attempts')}, ${r.get('cost_usd')}"
            )


if __name__ == "__main__":
    main(sys.argv[1:])
