from harness import slim

TASKS = """# TASKS

규칙: `▶` 항목만 구현한다.

## M1
[x] T1. 골격 (조건부 PASS 8/12, 2026-10-01 — 메모는 T2로 이관)
  - acceptance: 길고 긴 본문
  - touch: `a.ts`

[x] T2. 명단 (+ T1 REVIEW 메모: 보강) (PASS 10/12, 2026-10-02)
  - acceptance: 본문 2

[x] T3. 코어(스킬 제외) (+ T2 메모) (재작업 PASS 11/12, 2026-10-02)
  - acceptance: 본문 3

▶ [x] T4. 배당 (PASS 11/12)
  - acceptance: 본문 4

[ ] T5. 경제
  - acceptance: 본문 5
  - touch: `b.ts`
"""


def make(tmp_path, tasks=TASKS):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "TASKS.md").write_text(tasks, encoding="utf-8")
    return docs


def test_slim_tasks_collapses_old_done_items_and_archives_full_text(tmp_path):
    docs = make(tmp_path)
    assert slim.slim_tasks(docs) == 2  # 최근 완료 2개(T3, T4)는 상세 유지
    text = (docs / "TASKS.md").read_text(encoding="utf-8")
    assert "[x] T1. 골격 (조건부 PASS 8/12) — 상세는 docs/archive/TASKS-done.md" in text
    assert "[x] T2. 명단 (PASS 10/12)" in text
    assert "본문 2" not in text and "본문 3" in text and "본문 4" in text and "본문 5" in text
    assert "## M1" in text and "규칙:" in text
    arch = (docs / "archive" / "TASKS-done.md").read_text(encoding="utf-8")
    assert "길고 긴 본문" in arch and "본문 2" in arch
    assert slim.slim_tasks(docs) == 0  # 두 번째는 할 일이 없다 (중복 보관 없음)
    assert (docs / "archive" / "TASKS-done.md").read_text(encoding="utf-8") == arch


def test_slim_tasks_title_keeps_parenthesis_attached_to_word(tmp_path, monkeypatch):
    docs = make(tmp_path)
    monkeypatch.setattr(slim, "KEEP_FULL_DONE", 0)
    slim.slim_tasks(docs)
    assert "[x] T3. 코어(스킬 제외) (재작업 PASS 11/12)" in (docs / "TASKS.md").read_text(encoding="utf-8")


def test_slim_tasks_keeps_open_items_untouched_and_parseable(tmp_path):
    from harness import run
    docs = make(tmp_path)
    slim.slim_tasks(docs)
    text = (docs / "TASKS.md").read_text(encoding="utf-8")
    assert "[ ] T5. 경제\n  - acceptance: 본문 5\n  - touch: `b.ts`" in text
    assert run.current_task_touch(text.replace("[ ] T5.", "- [ ] T5.")) == ["b.ts"]


def test_slim_plan_keeps_latest_notes(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    notes = [f"- T{i} REVIEW 메모: 내용{i}" for i in range(1, 7)]
    (docs / "PLAN.md").write_text("# PLAN\n\n## 위험·메모\n- 일반 메모\n" + "\n".join(notes) + "\n", encoding="utf-8")
    assert slim.slim_plan(docs) == 3
    text = (docs / "PLAN.md").read_text(encoding="utf-8")
    assert "내용1" not in text and "내용3" not in text and "내용4" in text and "내용6" in text and "일반 메모" in text
    assert "내용1" in (docs / "archive" / "PLAN-notes.md").read_text(encoding="utf-8")


def test_slim_journal_rotates_only_when_clearly_long(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "JOURNAL.md").write_text("\n".join(f"- 줄{i}" for i in range(50)) + "\n", encoding="utf-8")
    assert slim.slim_journal(docs) == 0  # 60줄 이하면 건드리지 않는다
    (docs / "JOURNAL.md").write_text("\n".join(f"- 줄{i}" for i in range(100)) + "\n", encoding="utf-8")
    assert slim.slim_journal(docs) == 60
    kept = (docs / "JOURNAL.md").read_text(encoding="utf-8").splitlines()
    assert len(kept) == 40 and kept[0] == "- 줄60" and kept[-1] == "- 줄99"
    assert "- 줄0" in (docs / "archive" / "JOURNAL-old.md").read_text(encoding="utf-8")


def test_slim_docs_handles_missing_files(tmp_path):
    (tmp_path / "docs").mkdir()
    assert slim.slim_docs(tmp_path) == {"tasks": 0, "plan": 0, "journal": 0}


def test_report_summarizes_by_agent_and_counts_recorded_calls():
    from harness import report
    runs = [
        {"agent": "planner", "seconds": 60, "input_tokens": 1, "cache_write_tokens": 10, "cache_read_tokens": 100,
         "output_tokens": 5, "cost_usd": 0.5},
        {"agent": "planner", "seconds": 60},  # 기록 도입 전 호출
        {"agent": "evaluator", "seconds": 120, "total_tokens": 9000},
    ]
    s = report.summarize(runs)
    assert s["planner"] == {"calls": 2, "seconds": 120, "cost_usd": 0.5, "total_tokens": 116, "recorded": 1}
    assert s["evaluator"]["total_tokens"] == 9000 and s["evaluator"]["recorded"] == 1
