"""docs/runs.jsonl의 토큰·비용을 역할별로 합산한다.

  uv run python -m harness.report <프로젝트 디렉터리>
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

CLAUDE_KEYS = ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens")


def summarize(runs: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"calls": 0, "seconds": 0, "cost_usd": 0.0, "total_tokens": 0, "recorded": 0})
    for r in runs:
        s = out[r.get("agent", "?")]
        s["calls"] += 1
        s["seconds"] += r.get("seconds") or 0
        tokens = sum(r.get(k) or 0 for k in CLAUDE_KEYS) + (r.get("total_tokens") or 0)
        if tokens:
            s["recorded"] += 1  # 토큰 기록이 있는 호출 수 (기능 도입 전 기록은 0)
        s["total_tokens"] += tokens
        s["cost_usd"] += r.get("cost_usd") or 0.0
    return dict(out)


def main(argv: list[str]) -> int:
    if not argv:
        print("사용법: uv run python -m harness.report <프로젝트 디렉터리>")
        return 2
    path = Path(argv[0]) / "docs" / "runs.jsonl"
    if not path.exists():
        print(f"{path}가 없습니다.")
        return 1
    runs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"{'역할':<10}{'호출':>5}{'기록':>5}{'토큰(합)':>14}{'비용(USD)':>11}{'시간(분)':>9}")
    for agent, s in summarize(runs).items():
        print(f"{agent:<10}{s['calls']:>5}{s['recorded']:>5}{s['total_tokens']:>14,}{s['cost_usd']:>11.2f}{s['seconds'] / 60:>9.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
