"""Planner → Generator → Evaluator 사이클 오케스트레이터.

프롬프트에 글로만 있던 규칙(도구 범위, 테스트 통과, touch 범위, 합격 게이트)을 코드로 강제한다.
import해도 부작용이 없다: 인자 파싱과 실행은 main()/cli() 안에서만 일어난다.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path

from harness.slim import ITEM_RE, _split_items, slim_docs

PROMPTS = Path(__file__).resolve().parent / "prompts"  # 프롬프트는 패키지 안에 고정


# ---------------------------------------------------------------- 설정 (환경변수로 교체 가능)
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "8"))  # 한 번 실행에서 돌 최대 사이클
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "1800"))  # 에이전트 1회 호출 상한(초)
CHECK_TIMEOUT = int(os.environ.get("CHECK_TIMEOUT", "600"))  # pytest/ruff 1회 상한(초)
# 에이전트별 모델·effort는 agents.toml 한 파일에서 관리한다.
AGENTS_CONFIG = Path(os.environ.get("AGENTS_CONFIG") or PROMPTS.parent / "agents.toml")
ROLES = ("bootstrap", "planner", "generator", "evaluator")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BACKENDS = ("claude", "codex")  # 에이전트별로 어느 CLI를 부를지
CODEX_ONLY_EFFORTS = ("ultra",)
# codex는 도구별 허용 목록이 없고 샌드박스뿐이라, docs/만 쓰는 역할이 코드를 건드렸는지 run.py가 사후에 검사한다
DOCS_ONLY_ROLES = ("planner", "evaluator")
MAX_DECISION_TRIES = 3  # 결정 합의 재시도 상한

USAGE_STOP = float(os.environ.get("USAGE_STOP", "0"))  # 사용량이 이 비율(0~1) 이상이면 새 사이클 금지. 0이면 비율 기준 없음(limitStatus 거부는 항상 중단)
USAGE_MAX_AGE = int(os.environ.get("USAGE_MAX_AGE", "1800"))  # 캐시가 이보다 오래되면 낡은 값으로 본다(초)
USAGE_STRICT = os.environ.get("USAGE_STRICT") == "1"  # 1이면 캐시를 못 읽거나 낡았을 때 멈춘다
USAGE_CACHE = Path.home() / ".claude" / "vscode-claude-status-cache.json"

# 토큰 절감 스위치. 기본은 모두 켬. 끄려면 0.
ISOLATE_CLAUDE = os.environ.get("HARNESS_ISOLATE", "1") == "1"  # claude 호출에서 사용자 전역 설정·MCP·스킬·미사용 내장 도구를 빼 세션당 고정 입력을 줄인다
DOC_DIET = os.environ.get("DOC_DIET", "1") == "1"  # 사이클 시작 전에 TASKS·PLAN·JOURNAL의 지난 상세를 docs/archive/로 옮긴다
SKIP_PLANNER_ON_FAIL = os.environ.get("SKIP_PLANNER_ON_FAIL", "1") == "1"  # 직전 평가가 FAIL이면 Planner 없이 같은 TASK를 다시 연다
AGENT_MAX_BUDGET_USD = os.environ.get("AGENT_MAX_BUDGET_USD")  # claude 1회 호출의 비용 상한(API 환산, 달러). 없으면 제한 없음

GIT_CHECKPOINT = os.environ.get("GIT_CHECKPOINT", "1") == "1"  # 에이전트마다 git 커밋
# 사내 데이터가 든 폴더에서 돌리면 파일 내용이 API로 나간다. 경로에 이 문자열이 있으면 거부(쉼표 구분).
DENY_PATHS = [s.strip().lower() for s in os.environ.get("HARNESS_DENY_PATHS", "OneDrive - ").split(",") if s.strip()]

# 도구 범위. Bash는 패턴으로만 연다. 패턴 문법은 `/permissions`로 확인할 것.
# (진짜 격리는 컨테이너/WSL2가 필요하다. 이 패턴은 실수 방지용이지 샌드박스가 아니다.)
PLANNER_TOOLS = ["Read", "Glob", "Grep", "Edit(docs/**)"]
GENERATOR_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep",
                   "Bash(uv run)", "Bash(uv run *)", "Bash(uv add *)", "Bash(uv sync)", "Bash(uv sync *)"]
EVALUATOR_TOOLS = ["Read", "Glob", "Grep", "Edit(docs/**)",
                   "Bash(uv run)", "Bash(uv run *)",
                   "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)"]

# 사이클마다 run.py가 직접 돌리는 외부 검증
CHECK_COMMANDS = [("pytest", ["uv", "run", "pytest", "-q"]),
                  ("ruff", ["uv", "run", "ruff", "check", "src", "tests"])]
# package.json 프로젝트용. Windows의 npm은 npm.cmd라서 전체 경로로 풀어 준다
NPM = shutil.which("npm") or "npm"
NPM_CHECK_COMMANDS = [("npm test", [NPM, "test"]), ("npm build", [NPM, "run", "build"])]
# touch 범위 검사에서 늘 허용하는 경로 (docs/는 에이전트 문서, lock/pyproject는 uv add 결과)
ALWAYS_ALLOWED = ("docs/", "uv.lock", "pyproject.toml")
IGNORED_PARTS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".venv")

TASK_HEAD_RE = re.compile(r"^(?:▶\s*)?\[[ xX]\]")  # TASKS.md 항목 머리: "[ ] T1." 또는 "▶ [ ] T1."
LABEL_RE = re.compile(r"^선택:\s*([A-Z])(?![A-Za-z0-9])")  # 합의 라벨은 대문자 한 글자
_log_file: Path | None = None


class AgentFailed(Exception):
    def __init__(self, name: str, code: int):
        super().__init__(f"{name} 실패 (code={code})")
        self.code = code


# ---------------------------------------------------------------- 에이전트 설정 (agents.toml)
def load_agents(path: Path | None = None) -> dict[str, dict[str, str]]:
    """역할별 {"model":..., "effort":...}. 오타·잘못된 값은 첫 호출 전에 시작 단계에서 막는다."""
    path = path or AGENTS_CONFIG
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"에이전트 설정을 읽을 수 없습니다 ({path}): {e}")
    agents: dict[str, dict[str, str]] = {}
    for role, cfg in data.items():
        if role not in ROLES:
            raise SystemExit(f"{path}: 알 수 없는 섹션 [{role}] (허용: {', '.join(ROLES)})")
        unknown = set(cfg) - {"backend", "model", "effort"}
        if unknown:
            raise SystemExit(f"{path}: [{role}]에 알 수 없는 키 {sorted(unknown)} (허용: backend, model, effort)")
        backend = cfg.get("backend", "claude")
        if backend not in BACKENDS:
            raise SystemExit(f"{path}: [{role}] backend={backend!r} (허용: {', '.join(BACKENDS)})")
        if role == "bootstrap" and backend != "claude":
            raise SystemExit(f"{path}: [bootstrap]은 대화형 claude 세션이라 backend=\"claude\"만 쓸 수 있습니다")
        if "model" in cfg and not (isinstance(cfg["model"], str) and cfg["model"].strip()):
            raise SystemExit(f"{path}: [{role}] model은 비어 있지 않은 문자열이어야 합니다")
        efforts = EFFORTS + CODEX_ONLY_EFFORTS if backend == "codex" else EFFORTS
        if "effort" in cfg and cfg["effort"] not in efforts:
            raise SystemExit(f"{path}: [{role}] effort={cfg['effort']!r} (허용: {', '.join(efforts)})")
        agents[role] = dict(cfg)
    missing = [r for r in ROLES if r not in agents]
    if missing:
        raise SystemExit(f"{path}: 섹션이 없습니다: {', '.join(missing)}")
    return agents


# ---------------------------------------------------------------- 로그 · 기록
def log(message: str) -> None:
    line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    if _log_file is not None:
        with _log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def record_run(root: Path, **fields) -> None:
    """에이전트 호출 1회를 docs/runs.jsonl에 남긴다 (backend·모델·effort·소요 시간·프롬프트 해시)."""
    fields["ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
    with (root / "docs" / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(fields, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 입력 · 안전장치
def parse_args(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir", nargs="?", default=os.environ.get("PROJECT_DIR"),
                        help="작업 디렉터리 (예: ../projects/rss-wiki)")
    args = parser.parse_args(argv)
    if not args.project_dir:
        parser.error("작업 디렉터리를 지정하세요. 사용법: uv run python -m harness.run <프로젝트 디렉터리>")
    return Path(args.project_dir).resolve()


def check_root(root: Path) -> None:
    """사내/민감 경로 가드. HARNESS_ALLOW_ROOT=1이면 건너뛴다."""
    if os.environ.get("HARNESS_ALLOW_ROOT") == "1":
        return
    lowered = str(root).lower()
    for marker in DENY_PATHS:
        if marker in lowered:
            raise SystemExit(f"거부: 작업 디렉터리 경로에 '{marker}'가 들어 있습니다 ({root}). "
                             "claude -p는 파일 내용을 API로 보냅니다. 의도한 경로라면 HARNESS_ALLOW_ROOT=1.")


def usage_exceeded() -> bool:
    """VS Code 확장이 갱신하는 사용량 캐시(5시간·7일)로 한도 초과를 판단한다.
    읽기 실패·낡은 캐시는 조용히 넘기지 않고 경고한다. USAGE_STRICT=1이면 멈춘다."""
    try:
        usage = json.loads(USAGE_CACHE.read_text(encoding="utf-8"))["usageData"]
        used = max(usage["utilization5h"], usage["utilization7d"])
        age = time.time() - USAGE_CACHE.stat().st_mtime
    except (OSError, KeyError, ValueError, TypeError):
        log("경고: 사용량 캐시를 읽지 못했습니다. 사용량 가드가 꺼진 상태입니다 (USAGE_STRICT=1이면 중단).")
        return USAGE_STRICT
    if age > USAGE_MAX_AGE:
        log(f"경고: 사용량 캐시가 {age / 60:.0f}분 전 값입니다. VS Code가 닫혀 있으면 갱신되지 않습니다.")
        if USAGE_STRICT:
            return True
    log(f"사용량: 5h {usage['utilization5h']:.0%}, 7d {usage['utilization7d']:.0%} (중단 기준 {f"{USAGE_STOP:.0%}" if USAGE_STOP else "없음"})")
    return (USAGE_STOP > 0 and used >= USAGE_STOP) or usage.get("limitStatus", "allowed") not in ("allowed", "allowed_warning")


# ---------------------------------------------------------------- 사이클 카운터 · 마커 파일
def load_cycle_count(root: Path) -> int:
    try:
        return int((root / "docs" / ".cycle_count").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def save_cycle_count(root: Path, count: int) -> None:
    (root / "docs" / ".cycle_count").write_text(str(count), encoding="utf-8")


def clear_done_at_cycle_start(root: Path) -> None:
    """사이클마다 DONE을 지워 Planner가 PRD와 코드의 간극을 다시 평가하게 한다."""
    done = root / "docs" / "DONE"
    if done.exists():
        done.unlink()
        log("이전 사이클의 docs/DONE을 제거했습니다.")


def bootstrap_prd(root: Path, cfg: dict[str, str]) -> None:
    """PRD.md가 없으면 Planner를 대화형으로 띄워 사용자와 작성한다."""
    docs = root / "docs"
    if (docs / "PRD.md").exists():
        return
    log("PRD.md가 없습니다. Planner를 대화형으로 실행합니다. (끝나면 /exit)")
    prompt_text = (PROMPTS / "planner_bootstrap.md").read_text(encoding="utf-8")
    subprocess.run(["claude", "--allowedTools", "Read", "Glob", "Grep", "Edit(docs/**)",
                    *model_args(cfg), "--append-system-prompt", prompt_text], cwd=root)
    if not (docs / "PRD.md").exists():
        log("PRD.md가 작성되지 않았습니다. 종료합니다.")
        sys.exit(1)


# ---------------------------------------------------------------- 에이전트 호출
def build_prompt(prompt_file: str) -> str:
    """공통 규칙 + 역할 프롬프트 + 현재 시각. 시각은 모델이 아니라 run.py가 넣는다."""
    common = (PROMPTS / "common.md").read_text(encoding="utf-8")
    role = (PROMPTS / prompt_file).read_text(encoding="utf-8")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return f"{common}\n\n---\n\n{role}\n\n---\n현재 시각: {now} (JOURNAL의 {{ISO8601}}에는 이 값을 그대로 쓴다)\n"


def model_args(cfg: dict[str, str]) -> list[str]:
    args = []
    if cfg.get("model"):
        args += ["--model", cfg["model"]]
    if cfg.get("effort"):
        args += ["--effort", cfg["effort"]]
    return args


def extra_tools(env_name: str) -> list[str]:
    return [t.strip() for t in os.environ.get(env_name, "").split(",") if t.strip()]


def builtin_tools(allowed_tools: list[str]) -> list[str]:
    """허용 패턴("Bash(uv run *)", "Edit(docs/**)")에서 내장 도구 이름만 순서대로 뽑는다."""
    return list(dict.fromkeys(t.split("(", 1)[0] for t in allowed_tools))


def claude_command(allowed_tools: list[str], accept_edits: bool, model: str | None, effort: str | None) -> list[str]:
    cmd = ["claude", "-p", "--output-format", "json", "--allowedTools", *allowed_tools,
           "--permission-mode", "acceptEdits" if accept_edits else "default"]  # 사용자 auto 모드를 피한다
    if ISOLATE_CLAUDE:
        # --tools로 안 쓰는 내장 도구의 스키마를 빼는 것이 가장 크다(세션당 고정 입력 약 4.4만 → 1만 토큰, 실측).
        # 허용 패턴(--allowedTools)은 그대로라서 Edit(docs/**) 같은 범위 제한은 유지된다.
        cmd += ["--tools", ",".join(builtin_tools(allowed_tools)), "--strict-mcp-config",
                "--disable-slash-commands", "--setting-sources", "project"]
    if AGENT_MAX_BUDGET_USD:
        cmd += ["--max-budget-usd", AGENT_MAX_BUDGET_USD]
    return cmd + model_args({"model": model, "effort": effort})


def usage_fields(backend: str, payload: dict, text: str) -> dict:
    """호출 1회의 토큰·비용. claude는 json 응답의 usage, codex는 출력 끝의 'tokens used'."""
    if backend == "claude":
        u = payload.get("usage") or {}
        return {"input_tokens": u.get("input_tokens"), "cache_write_tokens": u.get("cache_creation_input_tokens"),
                "cache_read_tokens": u.get("cache_read_input_tokens"), "output_tokens": u.get("output_tokens"),
                "cost_usd": payload.get("total_cost_usd")}
    m = re.findall(r"tokens used\s*([\d,]+)", text)
    return {"total_tokens": int(m[-1].replace(",", ""))} if m else {}


def codex_command(model: str | None, effort: str | None, out_file: Path) -> list[str]:
    """codex exec 명령. 프롬프트는 마지막 `-`로 stdin에서 읽는다.
    승인 프롬프트는 끄고 쓰기는 작업 폴더 샌드박스 안으로 제한한다. 사용자 config는 읽는다:
    --ignore-user-config를 주면 Windows에서 샌드박스가 read-only로 떨어져 docs/에도 못 쓴다(실측).
    config의 notify 훅만 비운다."""
    # Windows의 npm 설치본은 codex.cmd라서 전체 경로로 풀어 줘야 subprocess가 찾는다
    cmd = [shutil.which("codex") or "codex", "exec", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
           "-s", "workspace-write", "-c", 'approval_policy="never"', "-c", "notify=[]", "-o", str(out_file)]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    return cmd + ["-"]


def run_agent(root: Path, name: str, prompt_file: str, allowed_tools: list[str], cycle: int,
              accept_edits: bool = False, model: str | None = None, effort: str | None = None,
              backend: str = "claude") -> None:
    """claude -p 또는 codex exec를 한 번 실행한다. 프롬프트는 stdin으로 넘긴다(Windows 명령행 길이 제한 회피).
    실패(비정상 종료·타임아웃·is_error)면 AgentFailed — 카운터를 저장하지 않아 같은 사이클부터 재개된다.
    codex는 allowed_tools를 쓰지 않는다(도구 허용 목록이 없고 샌드박스만 있음)."""
    prompt_text = build_prompt(prompt_file)
    out_file = None
    if backend == "codex":
        fd, tmp = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
        os.close(fd)
        out_file = Path(tmp)
        cmd = codex_command(model, effort, out_file)
    else:
        cmd = claude_command(allowed_tools, accept_edits, model, effort)
    meta = {"cycle": cycle, "agent": name, "backend": backend, "model": model, "effort": effort,
            "prompt_sha": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]}

    log(f"--- {name} 시작 (backend={backend}, model={model or 'default'}, effort={effort or 'default'}) ---")
    started = time.monotonic()
    env = None
    if backend == "codex":
        # 샌드박스는 uv 기본 캐시(AppData\Local\uv)에 못 쓰므로 `uv run`이 실패한다(실측). 쓰기 가능한 임시 폴더로 돌린다.
        env = {**os.environ}
        env.setdefault("UV_CACHE_DIR", str(Path(tempfile.gettempdir()) / "harness-uv-cache"))
    try:
        result = subprocess.run(cmd, cwd=root, input=prompt_text, capture_output=True, text=True,
                                encoding="utf-8", timeout=AGENT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        record_run(root, **meta, seconds=round(time.monotonic() - started), exit="timeout")
        log(f"{name} 타임아웃({AGENT_TIMEOUT}초)")
        raise AgentFailed(name, 124)
    except FileNotFoundError:
        log(f"{name}: '{cmd[0]}' 실행 파일을 찾을 수 없습니다. {backend} CLI가 설치돼 있는지 확인하세요.")
        raise AgentFailed(name, 127)
    finally:
        last_message = ""
        if out_file is not None:
            last_message = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
            out_file.unlink(missing_ok=True)

    seconds = round(time.monotonic() - started)
    payload: dict = {}
    if backend == "claude":
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            pass
        print(payload.get("result", result.stdout), flush=True)
    else:
        print(last_message or result.stdout[-2000:], flush=True)  # codex stdout은 진행 로그라 마지막 메시지만
    if result.stderr.strip():
        print(result.stderr.strip()[-2000:], file=sys.stderr, flush=True)
    failed = result.returncode != 0 or bool(payload.get("is_error"))
    record_run(root, **meta, seconds=seconds, exit=result.returncode, is_error=bool(payload.get("is_error")),
               turns=payload.get("num_turns"), **usage_fields(backend, payload, f"{result.stdout}\n{result.stderr}"))
    log(f"--- {name} 종료 (exit={result.returncode}, {seconds}초) ---")
    if failed:
        log(f"{name} 실패로 중단합니다.")
        raise AgentFailed(name, result.returncode or 1)


# ---------------------------------------------------------------- git 체크포인트 · touch 범위
def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")


def in_git_repo(root: Path) -> bool:
    try:
        return git(root, "rev-parse", "--is-inside-work-tree").returncode == 0
    except FileNotFoundError:
        return False


def changed_files(root: Path) -> set[str]:
    """root 기준 상대 경로로, 커밋되지 않은 변경·삭제·신규 파일 목록 (.gitignore 제외)."""
    out = git(root, "ls-files", "-z", "-m", "-d", "-o", "--exclude-standard").stdout
    return {p for p in out.split("\0") if p and not any(part in IGNORED_PARTS for part in Path(p).parts)}


def checkpoint(root: Path, message: str) -> None:
    """root 아래 변경만 커밋한다. 사이클 중 어느 에이전트의 작업이든 git으로 되돌릴 수 있게 한다."""
    if not GIT_CHECKPOINT or not in_git_repo(root):
        return
    git(root, "add", "-A", "--", ".")
    if git(root, "diff", "--cached", "--quiet", "--", ".").returncode == 0:
        return  # 바뀐 게 없음
    done = git(root, "commit", "-q", "-m", message, "--", ".")
    log(f"git 체크포인트: {message}" if done.returncode == 0 else f"git 체크포인트 실패: {done.stderr.strip()}")


def current_task_touch(tasks_text: str) -> list[str] | None:
    """TASKS.md에서 이번 작업(▶ 표시, 없으면 첫 미완료)의 touch 경로 목록. 못 찾으면 None."""
    items: list[list[str]] = []
    for line in tasks_text.splitlines():
        # "- [ ] ..."뿐 아니라 Planner가 실제로 쓰는 "[ ] T1. ..." · "▶ [ ] T1. ..." 형식도 항목 머리로 본다
        if line.startswith("- ") or TASK_HEAD_RE.match(line):
            items.append([line])
        elif items:
            items[-1].append(line)
    open_items = [i for i in items if "[ ]" in i[0]]
    chosen = next((i for i in open_items if "▶" in i[0]), open_items[0] if open_items else None)
    if chosen is None:
        return None
    paths: list[str] = []
    for k, line in enumerate(chosen):
        # "touch:"로 시작하는 줄(또는 머리 줄의 touch:)만 본다. 메모 문장 속 'touch'와 코드 식별자를 경로로 잡지 않는다
        if re.match(r"^\s*(?:-\s*)?touch\s*[:：]", line) or (k == 0 and "touch:" in line):
            paths += re.findall(r"`([^`]+)`", line.split("touch", 1)[1])
    return paths or None


def check_docs_only(root: Path, name: str, cfg: dict[str, str], before: set[str] | None) -> None:
    """codex로 도는 planner/evaluator는 도구 허용 목록이 없으므로, docs/ 밖을 바꿨는지 사후에 검사한다.
    claude 쪽은 도구 패턴(Edit(docs/**))이 막으므로 건너뛴다. 위반이면 AgentFailed(3) — 변경은 git으로 되돌린다."""
    if cfg.get("backend") != "codex" or name not in DOCS_ONLY_ROLES:
        return
    if before is None:
        log(f"경고: {name}(codex)이 docs/ 밖을 건드렸는지 검사할 수 없습니다 (git 저장소가 아님).")
        return
    stray = sorted(p for p in changed_files(root) - before if not p.startswith("docs/") and p != "uv.lock")
    if stray:
        log(f"{name}(codex)이 docs/ 밖 파일을 변경했습니다: {stray}. 중단합니다. (git diff로 확인 후 되돌리세요)")
        raise AgentFailed(name, 3)


def touch_violations(changed: set[str], touch: list[str]) -> list[str]:
    def allowed(path: str) -> bool:
        if path.startswith(ALWAYS_ALLOWED) or path in ALWAYS_ALLOWED:
            return True
        for t in touch:
            t = t.strip().replace("\\", "/")
            if path == t or fnmatch.fnmatch(path, t) or path.startswith(t.rstrip("/") + "/"):
                return True
        return False
    return sorted(p for p in changed if not allowed(p))


# ---------------------------------------------------------------- 외부 검증 (CHECKS.md)
def run_checks(root: Path, cycle: int, violations: list[str] | None = None) -> bool | None:
    """pytest·ruff(Python) 또는 npm test·build(package.json)를 run.py가 직접 돌려 docs/CHECKS.md에 쓴다.
    에이전트 보고가 아니라 종료코드다. 둘 다 아니면 None, 전부 통과면 True."""
    if (root / "pyproject.toml").exists():
        commands = CHECK_COMMANDS
    elif (root / "package.json").exists():
        commands = NPM_CHECK_COMMANDS
    else:
        return None
    rows, tails, ok = [], [], True
    for name, cmd in commands:
        try:
            r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8",
                               timeout=CHECK_TIMEOUT)
            code, output = r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            code, output = 124, f"타임아웃({CHECK_TIMEOUT}초)"
        except FileNotFoundError as e:
            code, output = 127, str(e)
        ok &= code == 0
        rows.append(f"- {name}: {'PASS' if code == 0 else 'FAIL'} (exit {code}) — `{' '.join(cmd)}`")
        tails.append(f"### {name}\n```\n" + "\n".join(output.splitlines()[-15:]) + "\n```")
    touch_line = ("- touch 범위 위반: 없음" if not violations else
                  "- touch 범위 위반: " + ", ".join(f"`{v}`" for v in violations))
    text = (f"# CHECKS (run.py가 직접 실행한 결과 — 에이전트 보고 아님)\n\n"
            f"- 사이클: {cycle}\n- 시각: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            + "\n".join(rows) + f"\n{touch_line}\n\n" + "\n\n".join(tails) + "\n")
    (root / "docs" / "CHECKS.md").write_text(text, encoding="utf-8")
    log("외부 검증: " + ", ".join(r.split(" (")[0].lstrip("- ") for r in rows)
        + ("" if not violations else f" / touch 위반 {len(violations)}건"))
    return ok


# ---------------------------------------------------------------- REVIEW 게이트
def enforce_review_gate(root: Path, checks_ok: bool | None, violations: list[str]) -> None:
    """프롬프트의 합격 기준을 코드로 한 번 더 강제한다.
    사양 충족 2점 미만이거나 외부 검증이 실패했는데 PASS 계열이면 FAIL로 정정한다."""
    review = root / "docs" / "REVIEW.md"
    text = review.read_text(encoding="utf-8")
    spec = re.search(r"사양 충족\s*\|\s*(\d)\s*/\s*3", text)
    result = re.search(r"^(PASS|조건부 PASS|FAIL)(\s*—.*)$", text, re.M)
    if not result or result.group(1) == "FAIL":
        return
    reasons = []
    if spec and int(spec.group(1)) < 2:
        reasons.append(f"사양 충족 {spec.group(1)}/3 < 2")
    if checks_ok is False:
        reasons.append("외부 검증(CHECKS.md) 실패")
    if not reasons:
        return
    fixed = f"FAIL{result.group(2)} [run.py 정정: {', '.join(reasons)}; 원래 판정 {result.group(1)}]"
    review.write_text(text[:result.start()] + fixed + text[result.end():], encoding="utf-8")
    log(f"합격 게이트: {result.group(1)} → FAIL ({', '.join(reasons)})")


# ---------------------------------------------------------------- 결정 합의
def read_choice(path: Path) -> str | None:
    """선택 파일 첫 줄의 "선택: {라벨}"에서 라벨(대문자 한 글자)을 읽는다. 형식 오류면 None."""
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    m = LABEL_RE.match(lines[0]) if lines else None
    return m.group(1) if m else None


def resolve_decisions(root: Path) -> None:
    """블라인드로 기록된 두 선택을 비교해 합의, 재시도, HALT를 판정한다."""
    decisions, docs = root / "docs" / "decisions", root / "docs"
    if not decisions.exists():
        return
    for topic in sorted(decisions.glob("*.md")):
        if topic.name.endswith((".planner.md", ".evaluator.md")):
            continue
        stem = topic.name[:-3]
        p_file, e_file = decisions / f"{stem}.planner.md", decisions / f"{stem}.evaluator.md"
        p_choice, e_choice = read_choice(p_file), read_choice(e_file)
        text = topic.read_text(encoding="utf-8")
        if "합의:" in text:
            continue  # 이미 확정된 주제
        # 파일은 있는데 라벨 형식이 어긋난 쪽은 지워서 다시 쓰게 한다 (영원히 안 맞는 교착 방지)
        for f, choice in ((p_file, p_choice), (e_file, e_choice)):
            if f.exists() and choice is None:
                f.unlink()
                topic.write_text(text + f"\n형식 오류: {f.name}의 첫 줄은 `선택: A`처럼 대문자 한 글자여야 한다. 다시 기록하라.\n",
                                 encoding="utf-8")
                text = topic.read_text(encoding="utf-8")
                log(f"결정 파일 형식 오류: {f.name} 삭제")
        if p_choice is None or e_choice is None:
            continue
        tries = text.count("합의 불발") + 1
        if p_choice == e_choice:
            topic.write_text(text + f"\n합의: {p_choice} (시도 {tries}회)\n", encoding="utf-8")
            log(f"결정 합의: {stem} → {p_choice}")
        elif tries >= MAX_DECISION_TRIES:
            (docs / "HALT").write_text(
                f"결정 합의 {MAX_DECISION_TRIES}회 불발: {stem}\n\n"
                f"[planner]\n{p_file.read_text(encoding='utf-8')}\n\n"
                f"[evaluator]\n{e_file.read_text(encoding='utf-8')}\n", encoding="utf-8")
            log(f"결정 합의 불발로 HALT: {stem}")
        else:
            topic.write_text(text + "\n합의 불발. 후보를 처음부터 다시 나열하고 장단점을 비교한 뒤 결정하라.\n",
                             encoding="utf-8")
            log(f"결정 합의 불발({tries}회): {stem} — 재시도")
        p_file.unlink()
        e_file.unlink()  # 선택 파일을 지워 다음 시도도 블라인드로 시작


def archive_resolved(root: Path) -> None:
    """Planner가 합의를 PRD에 반영한 뒤(Planner는 파일을 지울 수 없다) 확정된 주제를 resolved/로 치운다."""
    decisions = root / "docs" / "decisions"
    if not decisions.exists():
        return
    for topic in decisions.glob("*.md"):
        if topic.name.endswith((".planner.md", ".evaluator.md")):
            continue
        if "합의:" in topic.read_text(encoding="utf-8"):
            (decisions / "resolved").mkdir(exist_ok=True)
            topic.replace(decisions / "resolved" / topic.name)
            log(f"확정된 결정을 보관: {topic.name}")


# ---------------------------------------------------------------- Planner 생략 (FAIL 재작업)
REWORK_MARK = "재작업(run.py)"
MAX_SKIPPED_REWORKS = 2  # 같은 TASK를 이만큼 되열었는데도 FAIL이면 Planner가 다시 판단한다 (BLOCKED·분할 대비)


def reopen_failed_task(root: Path) -> str | None:
    """직전 평가가 FAIL이고 ▶ 항목이 [x]이면 [ ]로 되열고 재작업 줄을 붙인다. 되열었으면 TASK ID, 아니면 None.
    결정 합의가 대기 중이거나 같은 TASK를 이미 여러 번 되열었으면 Planner에게 맡긴다."""
    docs = root / "docs"
    tasks, review = docs / "TASKS.md", docs / "REVIEW.md"
    if not tasks.exists() or not review.exists():
        return None
    m = re.search(r"^(PASS|조건부 PASS|FAIL)(\s*—.*)?$", review.read_text(encoding="utf-8"), re.M)
    if not m or m.group(1) != "FAIL":
        return None
    if any(docs.glob("decisions/*.md")):
        return None  # 합의 대기 중인 결정이 있다 — Planner가 반영해야 한다
    lines = tasks.read_text(encoding="utf-8").splitlines()
    for a, b in _split_items(lines):
        head = ITEM_RE.match(lines[a])
        if "▶" in lines[a] and head.group(3) in "xX":
            if sum(REWORK_MARK in l for l in lines[a:b]) >= MAX_SKIPPED_REWORKS:
                return None
            lines[a] = lines[a].replace("[x]", "[ ]", 1).replace("[X]", "[ ]", 1)
            lines.insert(b, f"  - {REWORK_MARK}: docs/REVIEW.md의 FAIL 지적을 먼저 해소한다. (Planner 생략)")
            tasks.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
            return head.group(4).rstrip(".")
    return None


# ---------------------------------------------------------------- 사이클
def run_cycle(root: Path, iteration: int, agents: dict[str, dict[str, str]]) -> str:
    """한 사이클. 'ok' | 'done' | 'halt' 를 돌려준다. 에이전트 실패는 AgentFailed."""
    docs = root / "docs"
    clear_done_at_cycle_start(root)

    if DOC_DIET:
        moved = slim_docs(root)
        if any(moved.values()):
            log(f"문서 정리: TASKS {moved['tasks']}항목·PLAN {moved['plan']}줄·JOURNAL {moved['journal']}줄을 docs/archive/로 옮김")
    reopened = reopen_failed_task(root) if SKIP_PLANNER_ON_FAIL and iteration > 1 else None
    if reopened:
        log(f"Planner 생략: 직전 평가가 FAIL이라 {reopened}을(를) 다시 엽니다 (SKIP_PLANNER_ON_FAIL=1)")
        checkpoint(root, f"harness: cycle {iteration} planner skipped ({reopened} rework)")
    else:
        before = changed_files(root) if in_git_repo(root) else None
        run_agent(root, "planner", "planner.md", PLANNER_TOOLS, iteration, **agents["planner"])
        check_docs_only(root, "planner", agents["planner"], before)
        archive_resolved(root)
        checkpoint(root, f"harness: cycle {iteration} planner")

    if (docs / "DONE").exists():
        ok = run_checks(root, iteration)  # DONE은 LLM 선언이 아니라 외부 검증이 통과해야 인정
        if ok is False:
            (docs / "DONE").unlink()
            log("DONE 거부: pytest/ruff가 통과하지 않습니다. 사이클을 이어갑니다.")
        else:
            log("PLAN의 모든 항목이 완료되었습니다. 종료.")
            return "done"
    if (docs / "HALT").exists():
        log("사람의 개입이 필요합니다. docs/HALT를 확인하세요.")
        return "halt"

    before = changed_files(root) if in_git_repo(root) else None
    run_agent(root, "generator", "generator.md", GENERATOR_TOOLS + extra_tools("GENERATOR_EXTRA_TOOLS"),
              iteration, accept_edits=True, **agents["generator"])
    violations: list[str] = []
    if before is not None:
        touch = current_task_touch((docs / "TASKS.md").read_text(encoding="utf-8")) if (docs / "TASKS.md").exists() else None
        if touch:
            violations = touch_violations(changed_files(root) - before, touch)
            if violations:
                log(f"touch 범위 위반: {violations}")
    checkpoint(root, f"harness: cycle {iteration} generator")
    checks_ok = run_checks(root, iteration, violations)

    review = docs / "REVIEW.md"
    started = time.time()
    before = changed_files(root) if in_git_repo(root) else None
    run_agent(root, "evaluator", "evaluator.md", EVALUATOR_TOOLS + extra_tools("EVALUATOR_EXTRA_TOOLS"),
              iteration, **agents["evaluator"])
    check_docs_only(root, "evaluator", agents["evaluator"], before)
    if not review.exists() or review.stat().st_mtime < started - 1:
        log("REVIEW.md가 이번 사이클에 갱신되지 않았습니다. 중단합니다.")
        raise AgentFailed("evaluator", 1)
    enforce_review_gate(root, checks_ok, violations)

    resolve_decisions(root)
    checkpoint(root, f"harness: cycle {iteration} evaluator")
    if (docs / "HALT").exists():
        log("사람의 개입이 필요합니다. docs/HALT를 확인하세요.")
        return "halt"
    return "ok"


def main(root: Path) -> int:
    global _log_file
    check_root(root)
    agents = load_agents()
    (root / "docs").mkdir(exist_ok=True)
    _log_file = root / "docs" / "harness.log"
    log(f"작업 디렉터리: {root}")
    for role in ("planner", "generator", "evaluator"):
        a = agents[role]
        log(f"{role}: backend={a.get('backend', 'claude')}, model={a.get('model', 'default')}, "
            f"effort={a.get('effort', 'default')}")
    if all(agents["planner"].get(k, d) == agents["evaluator"].get(k, d)
           for k, d in (("backend", "claude"), ("model", None))):
        log(f"경고: Planner와 Evaluator가 같은 모델({agents['planner'].get('model', 'default')})입니다. "
            "결정 합의의 독립성이 낮습니다 (agents.toml의 [evaluator] 수정).")
    bootstrap_prd(root, agents["bootstrap"])

    start = load_cycle_count(root)
    if start > 0:
        log(f"이전 실행에서 {start} 사이클까지 진행되었습니다. 이어서 시작합니다.")
    for i in range(MAX_ITERATIONS):
        iteration = start + i + 1
        if usage_exceeded():
            log("사용량 중단 기준에 도달했습니다. 종료.")
            return 0
        log(f"=== 사이클 {iteration} 시작 ===")
        try:
            status = run_cycle(root, iteration, agents)
        except AgentFailed as e:
            return e.code  # 카운터 미저장 → 같은 사이클부터 재개
        save_cycle_count(root, iteration)
        if status == "done":
            return 0
        if status == "halt":
            return 1
        log(f"=== 사이클 {iteration} 종료 ===")
        time.sleep(3)
    log("최대 반복 횟수에 도달했습니다. 종료.")
    return 0


def cli() -> None:
    # Windows 콘솔(cp949)이나 파일로 리다이렉트된 stdout은 codex 출력의 '—' 같은 문자에서 UnicodeEncodeError로 죽는다
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(parse_args()))


if __name__ == "__main__":
    cli()
