import json

from harness import bench


def test_summarize_reads_docs_records(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / ".cycle_count").write_text("4", encoding="utf-8")
    (docs / "DONE").write_text("", encoding="utf-8")
    (docs / "runs.jsonl").write_text(
        json.dumps({"seconds": 10}) + "\n" + json.dumps({"seconds": 5}) + "\n", encoding="utf-8")
    assert bench.summarize(tmp_path) == {"cycles": 4, "done": True, "halt": False, "calls": 2, "seconds": 15}


def test_summarize_empty_project(tmp_path):
    assert bench.summarize(tmp_path)["cycles"] == 0 and not bench.summarize(tmp_path)["done"]


def test_eval_cases_have_prd_and_hidden_check():
    cases = [p for p in bench.EVALS.iterdir() if p.is_dir()]
    assert len(cases) >= 3
    for c in cases:
        assert (c / "PRD.md").exists() and (c / "check_test.py").exists()
        assert "(없음)" in (c / "PRD.md").read_text(encoding="utf-8")  # 미해결 결정이 없어야 HALT 없이 끝난다


def test_prompt_version_changes_with_prompt_content(tmp_path, monkeypatch):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.md").write_text("one", encoding="utf-8")
    monkeypatch.setattr(bench, "PACKAGE", tmp_path)
    v1 = bench.prompt_version()
    (tmp_path / "prompts" / "a.md").write_text("two", encoding="utf-8")
    assert bench.prompt_version() != v1
