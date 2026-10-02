"""rpn 평가셋의 숨은 채점."""
import os
import subprocess

import pytest


def run(*args):
    return subprocess.run(["uv", "run", "rpn", *args], cwd=os.environ["PROJECT_DIR"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


@pytest.mark.parametrize("expr, out", [("3 4 + 2 *", "14"), ("10 4 /", "2.5"), ("8 2 /", "4"), ("5 1 2 + 4 * + 3 -", "14")])
def test_results(expr, out):
    r = run(expr)
    assert r.returncode == 0 and r.stdout.strip() == out


@pytest.mark.parametrize("expr", ["1 0 /", "1 +", "2 x *", "1 2", ""])
def test_errors_exit_1_without_stdout(expr):
    r = run(expr)
    assert r.returncode == 1 and r.stdout.strip() == "" and r.stderr.strip()


def test_no_args_exits_2():
    assert run().returncode == 2
