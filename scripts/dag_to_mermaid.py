#!/usr/bin/env python3
"""Convert Shiki DAG JSON files to Mermaid diagram syntax.

DAG ノードの状態を色分けして可視化する。
出力は Issue/PR コメントに埋め込み可能な Mermaid 記法。

Usage:
  python3 scripts/dag_to_mermaid.py [dag-file]
  python3 scripts/dag_to_mermaid.py .shiki/dag/DAG-1.json
  python3 scripts/dag_to_mermaid.py --all
  python3 scripts/dag_to_mermaid.py --wrap

Options:
  dag-file    Path to a specific DAG JSON file
  --all       Process all DAG files in .shiki/dag/
  --wrap      Wrap output in Markdown code fence (```mermaid ... ```)
  --lr        Use left-to-right layout (default: top-to-bottom)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


# Node status to Mermaid style class mapping
# Colors: pending=gray, running=blue, completed=green, failed=red, skipped=orange
STATUS_STYLES = {
    "pending":   "fill:#e0e0e0,stroke:#999,color:#333",
    "running":   "fill:#42a5f5,stroke:#1565c0,color:#fff",
    "completed": "fill:#66bb6a,stroke:#2e7d32,color:#fff",
    "failed":    "fill:#ef5350,stroke:#c62828,color:#fff",
    "skipped":   "fill:#ffa726,stroke:#e65100,color:#fff",
}

# Engine to shape mapping
ENGINE_SHAPES = {
    "codex":         ("[/", "/]"),     # parallelogram
    "claude-team":   ("[[", "]]"),     # subroutine shape
    "claude-leader": ("{{", "}}"),     # hexagon
    "claude-member": ("([", "])"),     # stadium
    "human":         ("[(", ")]"),     # cylinder
}

DEFAULT_SHAPE = ("[", "]")  # rectangle


def sanitize_id(node_id: str) -> str:
    """Sanitize a node ID for Mermaid compatibility."""
    return node_id.replace("-", "_").replace("/", "_").replace(" ", "_")


def dag_to_mermaid(dag: Dict[str, Any], direction: str = "TB") -> str:
    """Convert a DAG dictionary to Mermaid diagram syntax.

    Args:
        dag: Parsed DAG JSON object
        direction: Graph direction - TB (top-bottom) or LR (left-right)

    Returns:
        Mermaid diagram string
    """
    lines: List[str] = []
    dag_id = dag.get("dag_id", "DAG")
    status = dag.get("status", "unknown")
    nodes = dag.get("nodes", [])
    edges = dag.get("edges", [])
    metadata = dag.get("metadata", {})

    total_batches = metadata.get("total_batches", 0)
    current_batch = metadata.get("current_batch", 0)

    lines.append(f"graph {direction}")
    lines.append(f"    %% DAG: {dag_id} | Status: {status}")
    lines.append(f"    %% Batches: {total_batches} | Current: {current_batch}")
    lines.append("")

    # Group nodes by batch using subgraphs
    batch_groups: Dict[int, List[Dict]] = {}
    for node in nodes:
        batch = node.get("batch", 0)
        batch_groups.setdefault(batch, []).append(node)

    # Generate subgraphs per batch
    for batch_num in sorted(batch_groups.keys()):
        batch_nodes = batch_groups[batch_num]
        batch_label = f"Batch {batch_num}"

        # Determine batch status indicator
        batch_statuses = [n.get("status", "pending") for n in batch_nodes]
        if all(s == "completed" for s in batch_statuses):
            batch_indicator = " [DONE]"
        elif any(s == "running" for s in batch_statuses):
            batch_indicator = " [RUNNING]"
        elif any(s == "failed" for s in batch_statuses):
            batch_indicator = " [FAILED]"
        else:
            batch_indicator = ""

        lines.append(f"    subgraph batch{batch_num}[\"{batch_label}{batch_indicator}\"]")

        for node in batch_nodes:
            node_id = sanitize_id(node["node_id"])
            task_id = node.get("task_id", "???")
            engine = node.get("engine", "unknown")
            node_status = node.get("status", "pending")
            estimated = node.get("estimated_tokens", 0)
            actual = node.get("actual_tokens", 0)

            # Build label
            label_parts = [task_id]
            if engine != "unknown":
                label_parts.append(f"[{engine}]")
            if actual > 0:
                label_parts.append(f"({actual:,}tok)")
            elif estimated > 0:
                label_parts.append(f"(~{estimated:,}tok)")

            label = " ".join(label_parts)

            # Get engine shape
            shape_open, shape_close = ENGINE_SHAPES.get(engine, DEFAULT_SHAPE)

            lines.append(f"        {node_id}{shape_open}\"{label}\"{shape_close}")

        lines.append("    end")
        lines.append("")

    # Generate edges
    if edges:
        lines.append("    %% Dependencies")
        for edge in edges:
            from_id = sanitize_id(edge["from"])
            to_id = sanitize_id(edge["to"])
            edge_type = edge.get("type", "depends_on")

            if edge_type == "depends_on":
                lines.append(f"    {from_id} --> {to_id}")
            elif edge_type == "blocks":
                lines.append(f"    {from_id} -.-x {to_id}")
            elif edge_type == "suggests":
                lines.append(f"    {from_id} -.-> {to_id}")
            else:
                lines.append(f"    {from_id} --> {to_id}")

        lines.append("")

    # Generate style classes for node statuses
    lines.append("    %% Status styles")
    for status_name, style in STATUS_STYLES.items():
        # Find nodes with this status
        styled_nodes = [sanitize_id(n["node_id"]) for n in nodes if n.get("status") == status_name]
        if styled_nodes:
            for sn in styled_nodes:
                lines.append(f"    style {sn} {style}")

    # Style the batch subgraphs
    lines.append("")
    lines.append("    %% Batch subgraph styles")
    for batch_num in sorted(batch_groups.keys()):
        if batch_num < current_batch:
            lines.append(f"    style batch{batch_num} fill:#f0f0f0,stroke:#ccc")
        elif batch_num == current_batch:
            lines.append(f"    style batch{batch_num} fill:#e3f2fd,stroke:#1565c0,stroke-width:2px")
        else:
            lines.append(f"    style batch{batch_num} fill:#fafafa,stroke:#ddd")

    return "\n".join(lines)


def format_summary(dag: Dict[str, Any]) -> str:
    """Generate a text summary of the DAG status."""
    nodes = dag.get("nodes", [])
    metadata = dag.get("metadata", {})

    total = len(nodes)
    completed = sum(1 for n in nodes if n.get("status") == "completed")
    failed = sum(1 for n in nodes if n.get("status") == "failed")
    running = sum(1 for n in nodes if n.get("status") == "running")
    pending = sum(1 for n in nodes if n.get("status") == "pending")
    skipped = sum(1 for n in nodes if n.get("status") == "skipped")

    total_estimated = sum(n.get("estimated_tokens", 0) for n in nodes)
    total_actual = sum(n.get("actual_tokens", 0) for n in nodes)

    lines = [
        f"**DAG:** {dag.get('dag_id', '???')} | **Status:** {dag.get('status', '???')}",
        f"**Batches:** {metadata.get('total_batches', '?')} | **Current:** {metadata.get('current_batch', '?')}",
        f"**Nodes:** {total} total | {completed} completed | {running} running | {pending} pending | {failed} failed | {skipped} skipped",
    ]

    if total_estimated > 0 or total_actual > 0:
        lines.append(f"**Tokens:** {total_actual:,} actual / {total_estimated:,} estimated")

    return "\n".join(lines)


def process_dag_file(dag_path: str, wrap: bool = False, direction: str = "TB") -> str:
    """Process a single DAG file and return Mermaid output.

    Args:
        dag_path: Path to the DAG JSON file
        wrap: Whether to wrap in Markdown code fence
        direction: Graph direction

    Returns:
        Mermaid diagram string (optionally wrapped)
    """
    with open(dag_path, encoding="utf-8") as f:
        dag = json.load(f)

    mermaid = dag_to_mermaid(dag, direction=direction)
    summary = format_summary(dag)

    output_parts = [summary, ""]

    if wrap:
        output_parts.append("```mermaid")
        output_parts.append(mermaid)
        output_parts.append("```")
    else:
        output_parts.append(mermaid)

    return "\n".join(output_parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert Shiki DAG JSON to Mermaid diagram",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s .shiki/dag/DAG-1.json
  %(prog)s --all --wrap
  %(prog)s .shiki/dag/DAG-1.json --lr --wrap
        """,
    )

    parser.add_argument(
        "dag_file",
        nargs="?",
        help="Path to DAG JSON file",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all DAG files in .shiki/dag/",
    )
    parser.add_argument(
        "--wrap",
        action="store_true",
        help="Wrap output in Markdown code fence (```mermaid)",
    )
    parser.add_argument(
        "--lr",
        action="store_true",
        help="Use left-to-right layout (default: top-to-bottom)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path (default: stdout)",
    )

    args = parser.parse_args()
    direction = "LR" if args.lr else "TB"

    if not args.dag_file and not args.all:
        parser.print_help()
        return 1

    outputs: List[str] = []

    if args.all:
        dag_dir = Path(".shiki/dag")
        if not dag_dir.exists():
            print("[ERROR] .shiki/dag/ directory not found", file=sys.stderr)
            return 1

        dag_files = sorted(dag_dir.glob("*.json"))
        if not dag_files:
            print("[INFO] No DAG files found in .shiki/dag/", file=sys.stderr)
            return 0

        for dag_file in dag_files:
            if dag_file.name == ".keep":
                continue
            try:
                output = process_dag_file(str(dag_file), wrap=args.wrap, direction=direction)
                outputs.append(f"## {dag_file.name}\n\n{output}")
            except (json.JSONDecodeError, OSError) as e:
                print(f"[ERROR] Failed to process {dag_file}: {e}", file=sys.stderr)
    else:
        dag_path = args.dag_file
        if not os.path.exists(dag_path):
            print(f"[ERROR] File not found: {dag_path}", file=sys.stderr)
            return 1

        try:
            output = process_dag_file(dag_path, wrap=args.wrap, direction=direction)
            outputs.append(output)
        except json.JSONDecodeError as e:
            print(f"[ERROR] Invalid JSON in {dag_path}: {e}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"[ERROR] Cannot read {dag_path}: {e}", file=sys.stderr)
            return 1

    result = "\n\n---\n\n".join(outputs)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"[OK] Written to {args.output}", file=sys.stderr)
    else:
        print(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
