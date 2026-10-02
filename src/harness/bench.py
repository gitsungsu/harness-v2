"""평가셋 실행기: 정답이 정해진 작은 PRD로 하네스를 돌려 사이클 수·HALT·소요 시간을 잰다.

프롬프트나 모델을 바꿀 때마다 돌려서 evals/results.jsonl의 이전 줄과 비교한다.
실제 claude를 호출하므로 --run을 줘야 실행된다.
  uv run python -m harness.bench               # 케이스 목록만 보여 줌
  uv run python -m harness.bench --run slugify # 케이스 하나 실행
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
EVALS = PACKAGE.parents[1] / "evals"
RESULTS = EVALS / "results.jsonl"


def prompt_version() -> str:
    """프롬프트 전체의 해시. 결과 줄마다 남겨 어떤 프롬프트로 잰 값인지 알 수 있게 한다."""
    h = hashlib.sha256()
    for f in sorted((PACKAGE / "prompts").glob("*.md")):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def summarize(project: Path) -> dict:
    """프로젝트의 docs/ 기록에서 사이클 수·종료 상태·소요 시간을 읽는다."""
    docs = project / "docs"
    try:
        cycles = int((docs / ".cycle_count").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        cycles = 0
    try:
        runs = [json.loads(line) for line in (docs / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        runs = []
    return {"cycles": cycles, "done": (docs / "DONE").exists(), "halt": (docs / "HALT").exists(),
            "calls": len(runs),
            "seconds": sum(r.get("seconds") or 0 for r in runs)}


def judge(case: Path, project: Path) -> bool:
    """숨겨 둔 check_test.py로 만들어진 결과물을 채점한다 (하네스 에이전트는 이 파일을 보지 못한다)."""
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", str(case / "check_test.py")],
                       env={**os.environ, "PROJECT_DIR": str(project)}, capture_output=True, text=True)
    return r.returncode == 0


def run_case(case: Path, max_iterations: int) -> dict:
    # mkdtemp는 Windows(Python 3.13+)에서 폴더 권한을 만든 계정 전용으로 좁혀서, Codex 샌드박스가 쓴 파일(REVIEW.md)을
    # 이 계정이 못 읽는다(실측). 일반 mkdir로 만든 폴더는 정상이므로 이름만 임의로 만들고 mkdir을 쓴다.
    project = Path(tempfile.gettempdir()) / f"bench-{case.name}-{uuid.uuid4().hex[:8]}"
    (project / "docs").mkdir(parents=True)
    shutil.copy(case / "PRD.md", project / "docs" / "PRD.md")
    env = {**os.environ, "MAX_ITERATIONS": str(max_iterations)}
    subprocess.run([sys.executable, "-m", "harness.run", str(project)], env=env)
    result = {"case": case.name, "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
              "prompt_version": prompt_version(), "project": str(project),
              **summarize(project), "passed": judge(case, project)}
    with RESULTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def main(argv: list[str]) -> int:
    cases = sorted(p for p in EVALS.iterdir() if (p / "PRD.md").exists())
    run = "--run" in argv
    names = [a for a in argv if not a.startswith("--")] or [c.name for c in cases]
    if not run:
        print("케이스:", ", ".join(c.name for c in cases), "— 실제 실행은 --run [케이스...]")
        return 0
    max_iterations = int(os.environ.get("BENCH_MAX_ITERATIONS", "10"))
    failed = 0
    for case in (c for c in cases if c.name in names):
        r = run_case(case, max_iterations)
        failed += not r["passed"]
        print(json.dumps(r, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
