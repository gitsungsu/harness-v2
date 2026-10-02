"""사이클마다 Planner가 통째로 읽는 docs/를 줄인다. 상세는 docs/archive/로 옮기고 한 줄 요약만 남긴다.

TASKS·PLAN·JOURNAL은 사이클이 쌓일수록 커져서, 에이전트 세션마다 입력 토큰이 단조 증가한다.
에이전트가 아니라 run.py가 결정론적으로 정리한다(내용 판단 없이 형식만 본다).
"""
import re
from pathlib import Path

KEEP_FULL_DONE = 2  # 최근 완료 항목 N개는 상세를 그대로 둔다 (FAIL 재작업에 상세가 필요하다)
KEEP_PLAN_NOTES = 3  # PLAN의 "Tn REVIEW 메모" 줄은 최근 N개만 둔다
KEEP_JOURNAL_LINES = 40  # JOURNAL은 마지막 N줄만 둔다

ITEM_RE = re.compile(r"^(▶ )?(- )?\[([ xX])\] (T\w+\.)")
PLAN_NOTE_RE = re.compile(r"^- T\w+ REVIEW 메모")
RESULT_RE = re.compile(r"((?:재작업 |조건부 )?PASS \d+/\d+)")


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(text)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _split_items(lines: list[str]) -> list[tuple[int, int]]:
    """TASKS의 항목(머리 줄 + 들여쓴 본문)의 [시작, 끝) 줄 범위."""
    spans = []
    i = 0
    while i < len(lines):
        if ITEM_RE.match(lines[i]):
            j = i + 1
            while j < len(lines) and (lines[j].startswith((" ", "\t")) or not lines[j].strip()):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1  # 뒤따르는 빈 줄은 항목에 넣지 않는다
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def stub_line(head: str) -> str:
    m = ITEM_RE.match(head)
    title = re.sub(r"\s+\(.*$", "", head[m.end(4):].strip()) if m else head
    result = RESULT_RE.search(head)
    return f"[x] {m.group(4)} {title} ({result.group(1) if result else '완료'}) — 상세는 docs/archive/TASKS-done.md"


def slim_tasks(docs: Path) -> int:
    """완료([x]) 항목을 한 줄로 줄이고 원문은 archive에 덧붙인다. 줄인 항목 수를 돌려준다."""
    path = docs / "TASKS.md"
    if not path.exists():
        return 0
    lines = _read(path).splitlines()
    spans = _split_items(lines)
    done = [s for s in spans if ITEM_RE.match(lines[s[0]]).group(3) in "xX"
            and "상세는 docs/archive/" not in lines[s[0]]]
    all_done_idx = [k for k, s in enumerate(spans) if ITEM_RE.match(lines[s[0]]).group(3) in "xX"]
    protected = {spans[k] for k in all_done_idx[max(len(all_done_idx) - KEEP_FULL_DONE, 0):]}  # [-0:]은 전체라서 피한다
    targets = [s for s in done if s not in protected]
    if not targets:
        return 0
    archived = []
    for a, b in targets:
        archived.append("\n".join(lines[a:b]) + "\n\n")
    _append(docs / "archive" / "TASKS-done.md", "".join(archived))
    for a, b in reversed(targets):
        lines[a:b] = [stub_line(lines[a])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    return len(targets)


def slim_plan(docs: Path) -> int:
    """PLAN의 'Tn REVIEW 메모' 줄을 최근 N개만 남기고 나머지는 archive로 옮긴다."""
    path = docs / "PLAN.md"
    if not path.exists():
        return 0
    lines = _read(path).splitlines()
    idx = [i for i, l in enumerate(lines) if PLAN_NOTE_RE.match(l)]
    old = idx[:-KEEP_PLAN_NOTES] if len(idx) > KEEP_PLAN_NOTES else []
    if not old:
        return 0
    _append(docs / "archive" / "PLAN-notes.md", "\n".join(lines[i] for i in old) + "\n")
    drop = set(old)
    path.write_text("\n".join(l for i, l in enumerate(lines) if i not in drop) + "\n", encoding="utf-8", newline="")
    return len(old)


def slim_journal(docs: Path) -> int:
    """JOURNAL은 마지막 N줄만 남기고 앞부분은 archive로 옮긴다."""
    path = docs / "JOURNAL.md"
    if not path.exists():
        return 0
    lines = _read(path).splitlines()
    if len(lines) <= KEEP_JOURNAL_LINES + 20:  # 매 사이클 자잘하게 옮기지 않도록 여유를 둔다
        return 0
    old, keep = lines[:-KEEP_JOURNAL_LINES], lines[-KEEP_JOURNAL_LINES:]
    _append(docs / "archive" / "JOURNAL-old.md", "\n".join(old) + "\n")
    path.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="")
    return len(old)


def slim_docs(root: Path) -> dict[str, int]:
    docs = root / "docs"
    return {"tasks": slim_tasks(docs), "plan": slim_plan(docs), "journal": slim_journal(docs)}
