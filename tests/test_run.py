import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from harness import run


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    (r / "docs").mkdir(parents=True)
    monkeypatch.setattr(run, "_log_file", None)
    monkeypatch.setattr(run, "GIT_CHECKPOINT", True)
    return r


AGENTS = {r: {"model": f"m-{r}", "effort": "high"} for r in run.ROLES}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 결정 합의
def test_read_choice_accepts_single_capital_letter(tmp_path):
    f = write(tmp_path / "x.md", "선택: A\n근거")
    assert run.read_choice(f) == "A"
    assert run.read_choice(write(tmp_path / "y.md", "선택: A안\n근거")) == "A"  # 'A안'도 A로 정규화
    assert run.read_choice(write(tmp_path / "z.md", "선택: a")) is None
    assert run.read_choice(write(tmp_path / "w.md", "선택: 옵션 A")) is None
    assert run.read_choice(tmp_path / "missing.md") is None


def make_topic(root, stem="db", planner="선택: A", evaluator="선택: A", topic="후보 A, B"):
    d = root / "docs" / "decisions"
    write(d / f"{stem}.md", topic)
    if planner is not None:
        write(d / f"{stem}.planner.md", planner)
    if evaluator is not None:
        write(d / f"{stem}.evaluator.md", evaluator)
    return d


def test_resolve_consensus_and_blind_reset(root):
    d = make_topic(root)
    run.resolve_decisions(root)
    assert "합의: A" in (d / "db.md").read_text(encoding="utf-8")
    assert not (d / "db.planner.md").exists() and not (d / "db.evaluator.md").exists()


def test_resolve_disagreement_retries_then_halts(root):
    d = make_topic(root, planner=None, evaluator=None)
    for attempt in range(1, run.MAX_DECISION_TRIES + 1):
        write(d / "db.planner.md", "선택: A")  # 에이전트가 매 시도 블라인드로 다시 기록
        write(d / "db.evaluator.md", "선택: B")
        run.resolve_decisions(root)
        if attempt < run.MAX_DECISION_TRIES:
            assert not (root / "docs" / "HALT").exists()
            assert (d / "db.md").read_text(encoding="utf-8").count("합의 불발") == attempt
    assert (root / "docs" / "HALT").exists()


def test_resolve_waits_for_other_side_and_skips_resolved(root):
    d = make_topic(root, evaluator=None)
    run.resolve_decisions(root)
    assert (d / "db.planner.md").exists()  # 한쪽만 있으면 건드리지 않는다
    write(d / "done.md", "후보\n합의: B (시도 1회)\n")
    write(d / "done.planner.md", "선택: A")
    write(d / "done.evaluator.md", "선택: B")
    run.resolve_decisions(root)
    assert (d / "done.md").read_text(encoding="utf-8").count("합의:") == 1  # 확정된 주제는 다시 판정하지 않음
    assert not (root / "docs" / "HALT").exists()


def test_resolve_deletes_malformed_choice_file(root):
    d = make_topic(root, planner="A로 하겠다", evaluator="선택: B")
    run.resolve_decisions(root)
    assert not (d / "db.planner.md").exists()
    assert (d / "db.evaluator.md").exists()  # 정상 쪽은 유지
    assert "형식 오류" in (d / "db.md").read_text(encoding="utf-8")


def test_archive_resolved_moves_only_agreed_topics(root):
    d = make_topic(root, stem="agreed", planner=None, evaluator=None, topic="후보\n합의: A (시도 1회)")
    write(d / "open.md", "후보만 있음")
    run.archive_resolved(root)
    assert (d / "resolved" / "agreed.md").exists() and (d / "open.md").exists()


# ---------------------------------------------------------------- touch 범위
TASKS = """# TASKS
- [x] T1 끝난 작업
  - touch: `src/old.py`
- [ ] T2 다음 작업
  - touch: `src/a.py`, `tests/test_a.py`
- [ ] ▶ T3 선정된 작업
  - touch: `src/pkg/`(신규), `tests/test_b.py`
  - acceptance: `uv run pytest`
"""


def test_current_task_touch_prefers_marked_item():
    assert run.current_task_touch(TASKS) == ["src/pkg/", "tests/test_b.py"]
    assert run.current_task_touch(TASKS.replace("▶ ", "")) == ["src/a.py", "tests/test_a.py"]
    assert run.current_task_touch("- [x] 전부 완료\n") is None


def test_touch_violations():
    touch = ["src/pkg/", "tests/test_b.py"]
    changed = {"src/pkg/x.py", "tests/test_b.py", "docs/IMPL.md", "uv.lock", "src/other.py", "README.md"}
    assert run.touch_violations(changed, touch) == ["README.md", "src/other.py"]


# ---------------------------------------------------------------- 합격 게이트
def review(root, spec, result):
    return write(root / "docs" / "REVIEW.md",
                 f"| 평가 축 | 점수 | 근거 |\n|---|---:|---|\n| 사양 충족 | {spec}/3 | x |\n\n## 결과\n\n{result}\n")


def test_gate_forces_fail_when_spec_below_two(root):
    p = review(root, 1, "PASS — 10/12")
    run.enforce_review_gate(root, True, [])
    text = p.read_text(encoding="utf-8")
    assert "FAIL — 10/12" in text and "사양 충족 1/3 < 2" in text and "원래 판정 PASS" in text


def test_gate_forces_fail_when_checks_failed(root):
    p = review(root, 3, "조건부 PASS — 8/12")
    run.enforce_review_gate(root, False, [])
    assert "FAIL" in p.read_text(encoding="utf-8") and "외부 검증" in p.read_text(encoding="utf-8")


def test_gate_leaves_good_and_failed_reviews_alone(root):
    p = review(root, 3, "PASS — 11/12")
    run.enforce_review_gate(root, True, [])
    assert p.read_text(encoding="utf-8").count("PASS — 11/12") == 1 and "정정" not in p.read_text(encoding="utf-8")
    p = review(root, 0, "FAIL — 3/12")
    before = p.read_text(encoding="utf-8")
    run.enforce_review_gate(root, False, [])
    assert p.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------- 사용량 · 경로 가드
def set_cache(monkeypatch, tmp_path, five=0.1, seven=0.1, status="allowed", age=0):
    f = write(tmp_path / "cache.json", json.dumps(
        {"usageData": {"utilization5h": five, "utilization7d": seven, "limitStatus": status}}))
    os.utime(f, (time.time() - age,) * 2)
    monkeypatch.setattr(run, "USAGE_CACHE", f)


def test_usage_thresholds(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "USAGE_STRICT", False)
    set_cache(monkeypatch, tmp_path, five=0.5, seven=0.79)
    assert run.usage_exceeded() is False
    set_cache(monkeypatch, tmp_path, five=0.8)
    assert run.usage_exceeded() is True
    set_cache(monkeypatch, tmp_path, status="rejected")
    assert run.usage_exceeded() is True


def test_usage_missing_or_stale_cache_warns_and_strict_stops(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run, "USAGE_CACHE", tmp_path / "none.json")
    monkeypatch.setattr(run, "USAGE_STRICT", False)
    assert run.usage_exceeded() is False and "경고" in capsys.readouterr().out
    monkeypatch.setattr(run, "USAGE_STRICT", True)
    assert run.usage_exceeded() is True
    set_cache(monkeypatch, tmp_path, age=run.USAGE_MAX_AGE + 60)
    assert run.usage_exceeded() is True  # 낡은 캐시 + strict
    monkeypatch.setattr(run, "USAGE_STRICT", False)
    assert run.usage_exceeded() is False and "경고" in capsys.readouterr().out


def test_check_root_denies_company_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_ALLOW_ROOT", raising=False)
    with pytest.raises(SystemExit):
        run.check_root(tmp_path / "OneDrive - Corp" / "proj")
    run.check_root(tmp_path / "personal" / "proj")
    monkeypatch.setenv("HARNESS_ALLOW_ROOT", "1")
    run.check_root(tmp_path / "OneDrive - Corp" / "proj")


def test_import_and_parse_args_have_no_side_effects(monkeypatch):
    monkeypatch.delenv("PROJECT_DIR", raising=False)
    with pytest.raises(SystemExit):
        run.parse_args([])  # 인자 없으면 오류 — import 시점이 아니라 호출 시점
    assert run.parse_args(["x"]).name == "x"


# ---------------------------------------------------------------- 에이전트 호출
class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_run_agent_passes_prompt_by_stdin_and_records(root, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return FakeProc(json.dumps({"result": "끝", "num_turns": 3}))

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    run.run_agent(root, "generator", "generator.md", ["Read", "Bash(uv run *)"], 2, accept_edits=True,
                  model="m", effort="medium")
    assert seen["cmd"][:2] == ["claude", "-p"] and "acceptEdits" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "m"
    assert seen["cmd"][seen["cmd"].index("--effort") + 1] == "medium"
    assert "Bash(uv run *)" in seen["cmd"]
    assert "당신의 임무: Generator" in seen["input"] and "공통 규칙" in seen["input"]
    assert all("당신의 임무" not in a for a in seen["cmd"])  # 프롬프트는 argv에 없다
    assert seen["timeout"] == run.AGENT_TIMEOUT
    rec = json.loads((root / "docs" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["agent"] == "generator" and rec["effort"] == "medium" and "cost_usd" not in rec and rec["turns"] == 3 and len(rec["prompt_sha"]) == 12


@pytest.mark.parametrize("proc, code", [
    (FakeProc("{}", returncode=3), 3),
    (FakeProc(json.dumps({"result": "x", "is_error": True})), 1),
])
def test_run_agent_failure_raises(root, monkeypatch, proc, code):
    monkeypatch.setattr(run.subprocess, "run", lambda *a, **k: proc)
    with pytest.raises(run.AgentFailed) as e:
        run.run_agent(root, "planner", "planner.md", [], 1)
    assert e.value.code == code


def test_run_agent_timeout_raises(root, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("claude", 1)
    monkeypatch.setattr(run.subprocess, "run", boom)
    with pytest.raises(run.AgentFailed) as e:
        run.run_agent(root, "planner", "planner.md", [], 1)
    assert e.value.code == 124
    assert json.loads((root / "docs" / "runs.jsonl").read_text(encoding="utf-8"))["exit"] == "timeout"


def test_tool_scopes_are_narrow():
    assert "Bash" not in run.GENERATOR_TOOLS and "Bash" not in run.EVALUATOR_TOOLS  # 맨 Bash 금지
    assert not any(t.startswith("Bash") for t in run.PLANNER_TOOLS)
    assert "Write" not in run.EVALUATOR_TOOLS and "Edit" not in run.EVALUATOR_TOOLS
    assert "Edit(docs/**)" in run.EVALUATOR_TOOLS


# ---------------------------------------------------------------- git 체크포인트
def init_repo(path: Path):
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    init_repo(tmp_path)
    proj = tmp_path / "proj"
    write(proj / "src" / "a.py", "x = 1\n")
    write(tmp_path / "outside.txt", "keep\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (proj / "docs").mkdir()
    monkeypatch.setattr(run, "_log_file", None)
    monkeypatch.setattr(run, "GIT_CHECKPOINT", True)
    return proj


def test_changed_files_and_checkpoint_scope(repo):
    write(repo / "src" / "a.py", "x = 2\n")
    write(repo / "src" / "new.py", "y = 1\n")
    write(repo / "__pycache__" / "junk.pyc", "x")
    write(repo.parent / "outside.txt", "changed\n")
    assert run.changed_files(repo) == {"src/a.py", "src/new.py"}  # 상대 경로, 캐시·밖 파일 제외
    run.checkpoint(repo, "harness: test")
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True).stdout
    assert "harness: test" in log
    assert run.changed_files(repo) == set()
    status = subprocess.run(["git", "status", "--short"], cwd=repo.parent, capture_output=True, text=True).stdout
    assert "outside.txt" in status  # 프로젝트 밖 변경은 커밋하지 않는다


def test_checkpoint_disabled_or_no_repo(tmp_path, monkeypatch, root):
    monkeypatch.setattr(run, "GIT_CHECKPOINT", False)
    run.checkpoint(root, "x")  # 예외 없이 지나간다
    monkeypatch.setattr(run, "GIT_CHECKPOINT", True)
    monkeypatch.setattr(run, "in_git_repo", lambda r: False)
    run.checkpoint(root, "x")


# ---------------------------------------------------------------- 외부 검증과 사이클
def test_run_checks_writes_checks_md(root, monkeypatch):
    assert run.run_checks(root, 1) is None  # pyproject 없으면 건너뜀
    write(root / "pyproject.toml", "")
    monkeypatch.setattr(run, "CHECK_COMMANDS", [("ok", ["python", "-c", "print('good')"]),
                                                ("bad", ["python", "-c", "import sys; sys.exit(2)"])])
    assert run.run_checks(root, 4, ["src/x.py"]) is False
    text = (root / "docs" / "CHECKS.md").read_text(encoding="utf-8")
    assert "ok: PASS" in text and "bad: FAIL (exit 2)" in text and "`src/x.py`" in text and "사이클: 4" in text


def stub_cycle(monkeypatch, calls, planner_effect=None, evaluator_effect=None, checks=True):
    def fake_agent(root, name, prompt_file, tools, cycle, **kw):
        calls.append(name)
        effect = {"planner": planner_effect, "evaluator": evaluator_effect}.get(name)
        if effect:
            effect(root)
    monkeypatch.setattr(run, "run_agent", fake_agent)
    monkeypatch.setattr(run, "run_checks", lambda root, cycle, v=None: checks)


def test_done_is_rejected_when_checks_fail(root, monkeypatch):
    calls = []
    stub_cycle(monkeypatch, calls, planner_effect=lambda r: (r / "docs" / "DONE").write_text(""),
               evaluator_effect=lambda r: review(r, 3, "PASS — 10/12"), checks=False)
    assert run.run_cycle(root, 1, AGENTS) == "ok"
    assert calls == ["planner", "generator", "evaluator"] and not (root / "docs" / "DONE").exists()
    assert "FAIL" in (root / "docs" / "REVIEW.md").read_text(encoding="utf-8")  # 외부 검증 실패 → 게이트


def test_done_accepted_when_checks_pass(root, monkeypatch):
    calls = []
    stub_cycle(monkeypatch, calls, planner_effect=lambda r: (r / "docs" / "DONE").write_text(""))
    assert run.run_cycle(root, 1, AGENTS) == "done" and calls == ["planner"]


def test_halt_from_planner_stops_before_generator(root, monkeypatch):
    calls = []
    stub_cycle(monkeypatch, calls, planner_effect=lambda r: (r / "docs" / "HALT").write_text("why"))
    assert run.run_cycle(root, 1, AGENTS) == "halt" and calls == ["planner"]


def test_stale_review_fails_cycle(root, monkeypatch):
    review(root, 3, "PASS — 10/12")
    os.utime(root / "docs" / "REVIEW.md", (1, 1))  # 오래된 REVIEW만 남아 있다
    stub_cycle(monkeypatch, [])
    with pytest.raises(run.AgentFailed):
        run.run_cycle(root, 1, AGENTS)


def test_main_saves_counter_only_after_success_and_resumes(root, monkeypatch):
    write(root / "docs" / "PRD.md", "prd")
    monkeypatch.setattr(run, "usage_exceeded", lambda: False)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(run, "MAX_ITERATIONS", 2)
    monkeypatch.setattr(run, "check_root", lambda r: None)
    outcomes = iter(["ok", "ok"])
    monkeypatch.setattr(run, "run_cycle", lambda r, i, a: next(outcomes))
    assert run.main(root) == 0 and run.load_cycle_count(root) == 2
    # 실패하면 카운터를 올리지 않고 그 종료코드를 돌려준다
    def fail(r, i, a):
        raise run.AgentFailed("planner", 7)
    monkeypatch.setattr(run, "run_cycle", fail)
    assert run.main(root) == 7 and run.load_cycle_count(root) == 2
    # halt는 카운터를 저장하고 1을 돌려준다
    monkeypatch.setattr(run, "run_cycle", lambda r, i, a: "halt")
    assert run.main(root) == 1 and run.load_cycle_count(root) == 3


def test_cycle_flags_touch_violation_and_commits_each_agent(repo, monkeypatch):
    write(repo / "docs" / "TASKS.md", "- [ ] ▶ T1 작업\n  - touch: `src/a.py`\n")

    def fake_agent(root, name, prompt_file, tools, cycle, **kw):
        if name == "generator":
            write(root / "src" / "a.py", "x = 3\n")          # 범위 안
            write(root / "src" / "sneaky.py", "bad = 1\n")   # 범위 밖
        if name == "evaluator":
            review(root, 3, "PASS — 11/12")

    seen = {}
    monkeypatch.setattr(run, "run_agent", fake_agent)
    monkeypatch.setattr(run, "run_checks", lambda root, cycle, v=None: seen.setdefault("v", v) or True)
    assert run.run_cycle(repo, 7, AGENTS) == "ok"
    assert seen["v"] == ["src/sneaky.py"]
    log = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout
    assert "harness: cycle 7 generator" in log and "harness: cycle 7 evaluator" in log


# ---------------------------------------------------------------- agents.toml
def toml(tmp_path, text):
    return write(tmp_path / "agents.toml", text)


FULL = """
[bootstrap]
model = "a"
[planner]
model = "b"
effort = "max"
[generator]
effort = "low"
[evaluator]
model = "c"
effort = "xhigh"
"""


def test_shipped_agents_toml_is_valid():
    agents = run.load_agents()
    assert set(agents) == set(run.ROLES)
    assert all(a.get("model") and a.get("effort") in run.EFFORTS for a in agents.values())


def test_load_agents_allows_omitted_fields(tmp_path):
    agents = run.load_agents(toml(tmp_path, FULL))
    assert agents["generator"] == {"effort": "low"} and agents["planner"] == {"model": "b", "effort": "max"}


@pytest.mark.parametrize("text, msg", [
    (FULL + "[reviewer]\nmodel = 'x'\n", "알 수 없는 섹션"),
    (FULL.replace('effort = "low"', 'effort = "ultra"'), "effort"),
    (FULL.replace('model = "b"', 'modle = "b"'), "알 수 없는 키"),
    (FULL.replace('model = "b"', 'model = ""'), "model"),
    (FULL.replace("[evaluator]", "[evaluatr]"), "알 수 없는 섹션"),
    ("[planner]\nmodel = 'x'\n", "섹션이 없습니다"),
    ("[planner\n", "읽을 수 없습니다"),
])
def test_load_agents_rejects_bad_config(tmp_path, text, msg):
    with pytest.raises(SystemExit, match=msg):
        run.load_agents(toml(tmp_path, text))


def test_load_agents_missing_file(tmp_path):
    with pytest.raises(SystemExit, match="읽을 수 없습니다"):
        run.load_agents(tmp_path / "none.toml")


def test_cycle_passes_each_roles_model_and_effort(root, monkeypatch):
    seen = {}

    def fake_agent(root, name, prompt_file, tools, cycle, **kw):
        seen[name] = kw
        if name == "evaluator":
            review(root, 3, "PASS — 11/12")

    monkeypatch.setattr(run, "run_agent", fake_agent)
    monkeypatch.setattr(run, "run_checks", lambda root, cycle, v=None: True)
    agents = {**AGENTS, "generator": {"model": "gm", "effort": "low"}, "evaluator": {"effort": "max"}}
    run.run_cycle(root, 1, agents)
    assert seen["planner"] == {"model": "m-planner", "effort": "high"}
    assert seen["generator"] == {"accept_edits": True, "model": "gm", "effort": "low"}
    assert seen["evaluator"] == {"effort": "max"}


def test_bootstrap_uses_bootstrap_section(root, monkeypatch):
    seen = {}
    monkeypatch.setattr(run.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and FakeProc())
    with pytest.raises(SystemExit):  # 가짜 claude는 PRD를 만들지 않으므로 종료
        run.bootstrap_prd(root, {"model": "bm", "effort": "low"})
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "bm"
    assert seen["cmd"][seen["cmd"].index("--effort") + 1] == "low"


# ---------------------------------------------------------------- codex backend
def test_shipped_evaluator_uses_codex_gpt_6_1_sol_high():
    ev = run.load_agents()["evaluator"]
    assert ev == {"backend": "codex", "model": "gpt-6.1-sol", "effort": "high"}


def test_load_agents_backend_rules(tmp_path):
    base = FULL.replace("[evaluator]", '[evaluator]\nbackend = "codex"')
    assert run.load_agents(toml(tmp_path, base))["evaluator"]["backend"] == "codex"
    ultra = base.replace('effort = "xhigh"', 'effort = "ultra"')
    assert run.load_agents(toml(tmp_path, ultra))["evaluator"]["effort"] == "ultra"  # codex만 허용
    with pytest.raises(SystemExit, match="effort"):
        run.load_agents(toml(tmp_path, FULL.replace('effort = "xhigh"', 'effort = "ultra"')))
    with pytest.raises(SystemExit, match="backend"):
        run.load_agents(toml(tmp_path, FULL.replace("[evaluator]", '[evaluator]\nbackend = "gemini"')))
    with pytest.raises(SystemExit, match="bootstrap"):
        run.load_agents(toml(tmp_path, FULL.replace("[bootstrap]", '[bootstrap]\nbackend = "codex"')))


def test_codex_command_shape(tmp_path):
    cmd = run.codex_command("gpt-6.1-sol", "high", tmp_path / "o.txt")
    assert cmd[1] == "exec" and cmd[-1] == "-"  # 프롬프트는 stdin
    assert cmd[cmd.index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in cmd and 'approval_policy="never"' in cmd
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert "--ignore-user-config" not in cmd and "notify=[]" in cmd and "--ephemeral" in cmd
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "o.txt")
    assert "-m" not in run.codex_command(None, None, tmp_path / "o.txt")


def test_run_agent_codex_runs_codex_and_prints_last_message(root, monkeypatch, capsys):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        Path(cmd[cmd.index("-o") + 1]).write_text("PASS — 10/12", encoding="utf-8")
        return FakeProc("진행 로그 많음", returncode=0)

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    run.run_agent(root, "evaluator", "evaluator.md", ["Read"], 3, model="gpt-6.1-sol", effort="high", backend="codex")
    assert "exec" in seen["cmd"] and "claude" not in seen["cmd"][0] and "Read" not in seen["cmd"]
    assert "당신의 임무: Evaluator" in seen["input"]
    assert "PASS — 10/12" in capsys.readouterr().out
    assert seen["env"]["UV_CACHE_DIR"]  # 샌드박스가 쓸 수 있는 캐시 경로
    rec = json.loads((root / "docs" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["backend"] == "codex" and rec["model"] == "gpt-6.1-sol" and "cost_usd" not in rec
    assert not list(Path(seen["cmd"][seen["cmd"].index("-o") + 1]).parent.glob(Path(seen["cmd"][seen["cmd"].index("-o") + 1]).name))


def test_run_agent_codex_failure_and_missing_cli(root, monkeypatch):
    monkeypatch.setattr(run.subprocess, "run", lambda cmd, **kw: FakeProc("", "model not supported", returncode=1))
    with pytest.raises(run.AgentFailed) as e:
        run.run_agent(root, "evaluator", "evaluator.md", [], 1, backend="codex")
    assert e.value.code == 1

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(run.subprocess, "run", missing)
    with pytest.raises(run.AgentFailed) as e:
        run.run_agent(root, "evaluator", "evaluator.md", [], 1, backend="codex")
    assert e.value.code == 127


def test_docs_only_check_for_codex_roles(repo):
    codex = {"backend": "codex"}
    before = run.changed_files(repo)
    write(repo / "docs" / "REVIEW.md", "ok")
    write(repo / "uv.lock", "lock")
    run.check_docs_only(repo, "evaluator", codex, before)  # docs/와 uv.lock은 허용
    write(repo / "src" / "a.py", "x = 99\n")
    with pytest.raises(run.AgentFailed) as e:
        run.check_docs_only(repo, "evaluator", codex, before)
    assert e.value.code == 3
    run.check_docs_only(repo, "evaluator", {"backend": "claude"}, before)  # claude는 도구 패턴이 막으므로 검사 안 함
    run.check_docs_only(repo, "generator", codex, before)  # generator는 코드 수정이 정상
    run.check_docs_only(repo, "evaluator", codex, None)  # git이 아니면 경고만
