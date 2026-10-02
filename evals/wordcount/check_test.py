"""wordcount 평가셋의 숨은 채점."""
import os
import subprocess


def run(*args):
    return subprocess.run(["uv", "run", "wordcount", *args], cwd=os.environ["PROJECT_DIR"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


def lines(r):
    return r.stdout.strip().splitlines()


def test_top_default_and_tie_order(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("the cat the dog The end", encoding="utf-8")
    assert lines(run(str(f))) == ["the 3", "cat 1", "dog 1"]


def test_top_option_and_apostrophe(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("don't stop, don't go. go go", encoding="utf-8")
    assert lines(run(str(f), "--top", "2")) == ["go 3", "don't 2"]


def test_missing_file_exits_2(tmp_path):
    r = run(str(tmp_path / "none.txt"))
    assert r.returncode == 2 and r.stderr.strip()


def test_empty_file_prints_nothing(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("  !!! ", encoding="utf-8")
    r = run(str(f))
    assert r.returncode == 0 and r.stdout.strip() == ""
