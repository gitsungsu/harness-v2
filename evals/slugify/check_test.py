"""슬러그 평가셋의 숨은 채점. PROJECT_DIR 환경변수가 가리키는 프로젝트를 실제로 실행해 본다."""
import os
import subprocess


def run(*args):
    return subprocess.run(["uv", "run", "slugify", *args], cwd=os.environ["PROJECT_DIR"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


def test_basic():
    assert run("Hello, World!").stdout.strip() == "hello-world"
    assert run("  a  b  ").stdout.strip() == "a-b"
    assert run("C++ & Rust").stdout.strip() == "c-rust"


def test_empty_result_exits_1_with_message():
    r = run("!!!")
    assert r.returncode == 1 and r.stdout.strip() == "" and r.stderr.strip()


def test_no_args_exits_2():
    assert run().returncode == 2
