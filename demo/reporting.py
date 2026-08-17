"""Terminal presentation for the closed-loop demo.

Kept apart from the steps so that a step reads as the governance it
performs, not as a wall of ``print``. Every function here is pure
formatting over values the steps already obtained — nothing in this
module queries the database or decides anything.
"""

from __future__ import annotations

from typing import Any

WIDTH = 60
RULE = "=" * WIDTH
THIN = "-" * WIDTH


def banner(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def section(title: str) -> None:
    print(f"\n{title}\n{THIN}")


def detail(message: str) -> None:
    print(f"  {message}", flush=True)


def bullet(message: str) -> None:
    print(f"    - {message}", flush=True)


def kv(label: str, value: Any, *, width: int = 15) -> None:
    print(f"  {label:<{width}}: {value}", flush=True)


def state_block(title: str, rows: list[tuple[str, str]]) -> None:
    """The initial/intermediate/final state summary."""
    section(title)
    for label, value in rows:
        kv(label, value)
    print(THIN)


def step_header(number: int, total: int, title: str) -> None:
    banner(f"[{number}/{total}]  {title}")


def config_block(config: dict[str, Any]) -> None:
    """Print the run's parameters — the reproducibility record."""
    section("Configuration (the full reproducibility record)")
    for key, value in config.items():
        kv(key, value, width=24)
    print(THIN)


def drift_report(
    *,
    reference_label: str,
    production_label: str,
    reference_samples: int,
    production_samples: int,
    feature_results: list[Any],
    score: float,
    threshold: float,
    detected: bool,
    notes: str = "",
    top_n: int = 8,
) -> None:
    """Statistical evidence, not a boolean.

    Prints the drifted features first and then a sample of stable ones,
    because "these six moved and these twenty-four did not" is the claim
    worth making — a targeted covariate shift and a broken data pipeline
    look identical if you only report the overall verdict.
    """
    section("Drift monitoring")
    kv("Reference", reference_label, width=20)
    kv("Production window", production_label, width=20)
    kv("Reference samples", f"{reference_samples:,}", width=20)
    kv("Production samples", f"{production_samples:,}", width=20)

    drifted = [f for f in feature_results if f.drift_detected]
    stable = [f for f in feature_results if not f.drift_detected]

    if drifted:
        print("\n  Features flagged (p < threshold):")
        for f in drifted[:top_n]:
            p = "n/a" if f.p_value is None else f"{f.p_value:.2e}"
            bullet(f"{f.feature:<8} {f.method:<6} stat={f.score:.4f}  p={p}")
        if len(drifted) > top_n:
            bullet(f"... and {len(drifted) - top_n} more")

    print(f"\n  Features not flagged: {len(stable)}")
    for f in stable[:3]:
        p = "n/a" if f.p_value is None else f"{f.p_value:.2e}"
        bullet(f"{f.feature:<8} {f.method:<6} stat={f.score:.4f}  p={p}")
    if len(stable) > 3:
        bullet(f"... and {len(stable) - 3} more")

    print()
    kv("Overall score", f"{score:.4f}", width=20)
    kv("Threshold applied", f"{threshold:.3e}", width=20)
    if notes:
        kv("Method", notes, width=20)
    kv("Status", "DRIFT DETECTED" if detected else "NORMAL", width=20)
    print(THIN)


def metric_comparison(
    *,
    v1_stored: dict[str, Any],
    v1_live: dict[str, Any],
    v2_metrics: dict[str, Any],
    thresholds: dict[str, float],
) -> None:
    """The V1-vs-V2 table the promotion decision is made against.

    Three columns, not two, because "V1 stored" and "V1 on today's
    traffic" are different claims and conflating them is how a retrain
    gets justified by a number nobody measured. The threshold column is
    what the policy actually enforces.
    """
    section("Model validation — acceptance criteria")
    header = (
        f"  {'Metric':<12}{'V1 (stored)':>14}{'V1 (live)':>13}"
        f"{'V2':>10}{'Required':>11}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    keys = ["f1", "precision", "recall", "roc_auc", "pr_auc"]
    for key in keys:
        if not any(
            k.get(key) is not None for k in (v1_stored, v1_live, v2_metrics)
        ) and key not in thresholds:
            continue
        print(
            f"  {key:<12}"
            f"{_fmt(v1_stored.get(key)):>14}"
            f"{_fmt(v1_live.get(key)):>13}"
            f"{_fmt(v2_metrics.get(key)):>10}"
            f"{_fmt(thresholds.get(key)):>11}"
        )
    print(THIN)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def lineage_block(graph: Any) -> None:
    """Render the answer to 'why does this model exist?'."""
    section("Lineage")
    for node in graph.nodes:
        attrs = node.attributes or {}
        summary = ", ".join(
            f"{k}={v}" for k, v in attrs.items() if v not in (None, "")
        )
        detail(f"{node.type:<16} {node.label:<28} {summary}")
    print()
    for edge in graph.edges:
        detail(f"{edge.source}  --{edge.type}-->  {edge.target}")
    print(THIN)


def evidence_block(entries: list[dict[str, Any]]) -> None:
    """The structured log the run produced, replayed in order."""
    section("Structured event log")
    for entry in entries:
        extra = {
            k: v
            for k, v in entry.items()
            if k
            not in {"timestamp", "component", "event", "dataset_version", "model_version"}
            and v is not None
        }
        suffix = ("  " + " ".join(f"{k}={v}" for k, v in extra.items())) if extra else ""
        detail(
            f"{entry['timestamp']}  {entry['component']:<22} "
            f"{entry['event']:<26}{suffix}"
        )
    print(THIN)
