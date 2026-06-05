#!/usr/bin/env python3
"""Build the workflow-generated Shiki CCA evidence manifest."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from shiki_evidence import build_cca_evidence_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Shiki CCA evidence manifest")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manifest = build_cca_evidence_manifest(
        repository=args.repo,
        pr=args.pr,
        head_sha=args.head_sha,
        workflow_name=args.workflow_name,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        event_name=args.event_name,
        artifact_name=args.artifact_name,
        evidence_dir=Path(args.evidence_dir),
    )
    manifest["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote CCA evidence manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
