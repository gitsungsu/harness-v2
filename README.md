# harness v2

`../harness`의 개선판입니다. 원본은 그대로 두고 새로 만들었습니다. Planner → Generator → Evaluator 사이클로 `claude -p`를 돌려 프로젝트를 만드는 하네스이고, 프롬프트에 글로만 있던 규칙을 코드로 강제하는 데 초점을 맞췄습니다.

## 실행

```bash
# venv를 Google Drive 밖에 두세요 (파일 수천 개가 동기화됩니다)
export UV_PROJECT_ENVIRONMENT=~/.venvs/harness-v2     # PowerShell: $env:UV_PROJECT_ENVIRONMENT = "C:\venvs\harness-v2"

uv run python -m harness.run ../projects/rss-wiki      # 또는 uv run harness <프로젝트>
uv run pytest                                          # 하네스 자체 테스트
uv run python -m harness.bench                         # 평가셋 케이스 목록 (--run을 붙여야 실제 실행)
```

## 지적 사항 → 변경

| # | 지적 | 변경 | 위치 |
|---|---|---|---|
| 1 | Generator/Evaluator의 Bash가 통째로 열림 | 맨 `Bash` 제거. Generator는 `uv run/add/sync`만, Evaluator는 `uv run`과 읽기 전용 `git status/diff/log`만. 부족하면 `GENERATOR_EXTRA_TOOLS`, `EVALUATOR_EXTRA_TOOLS`(쉼표 구분)로 추가 | `run.py` `*_TOOLS` |
| 2 | 완료 판정을 전부 LLM이 함 | Generator 직후 `run.py`가 pytest·ruff를 직접 실행해 `docs/CHECKS.md`에 기록. Planner가 `DONE`을 만들어도 검사가 실패하면 `DONE`을 지우고 사이클을 이어감 | `run_checks`, `run_cycle` |
| 3 | git 체크포인트·diff 없음 | 에이전트마다 해당 프로젝트 폴더 변경만 커밋(`harness: cycle N planner/generator/evaluator`). Generator 변경 파일을 TASKS의 `▶` 항목 `touch`와 대조해 위반 목록을 `CHECKS.md`에 기록. 프롬프트 해시는 `runs.jsonl`에 기록. 끄려면 `GIT_CHECKPOINT=0` | `checkpoint`, `touch_violations` |
| 4 | 하네스 import가 곧 실행이라 테스트 불가 | 인자 파싱·실행은 `main()`/`cli()` 안으로. 함수가 `root`를 인자로 받음. 테스트 다수 | `run.py`, `tests/` |
| 5 | 사용량 가드가 조용히 꺼짐 | 캐시를 못 읽거나 30분 넘게 낡았으면 경고 로그. `USAGE_STRICT=1`이면 멈춤. `limitStatus`가 `rejected` 등 차단 상태면 멈춤(`allowed_warning`은 통과) | `usage_exceeded` |
| 6 | 반복 50, timeout 없음, REVIEW 갱신 미확인 | `MAX_ITERATIONS` 기본 8, 에이전트 호출 `AGENT_TIMEOUT`(기본 1800초), pytest/ruff `CHECK_TIMEOUT`(600초). 이번 사이클에 `REVIEW.md`가 갱신되지 않으면 중단 | `run_agent`, `run_cycle` |
| 7 | 관측 부족 | `docs/harness.log`(콘솔 로그 사본), `docs/runs.jsonl`(에이전트 호출마다 backend·모델·effort·소요 시간·턴 수·프롬프트 해시). 비용은 기록하지 않음 | `record_run` |
| 8 | `[IS08601]` 오타 | `{ISO8601}`로 수정 | `evaluator.md` |
| 9 | 합의 독립성·모델 별칭·라벨 | 모델을 고정 ID로 `agents.toml`에 지정. Planner와 Evaluator가 같으면 경고. 라벨은 대문자 한 글자만 인정(`A안`은 `A`로 정규화), 형식이 어긋난 선택 파일은 지워서 다시 쓰게 함 | `read_choice`, `resolve_decisions` |
| 10 | 합격 필수 게이트 없음 | 사양 충족 2점 미만 또는 `CHECKS.md` 실패면 합계와 무관하게 FAIL. 프롬프트에 규칙을 쓰고, `run.py`가 REVIEW.md를 읽어 어긋나면 FAIL로 정정(원래 판정을 남김) | `enforce_review_gate`, `evaluator.md` |
| 낮음 | 진입점, python 버전, 프롬프트 이름 중복 등 | 아래 참고 | |

낮음 항목의 처리:

- **진입점**: `harness = "harness.run:cli"`로 수정. `description` 채움. `requires-python >=3.11`.
- **프롬프트 정리**: 세 프롬프트가 중복하던 "사용자 질문 정책"과 `{ISO8601}`·라벨 규칙을 `prompts/common.md`로 모으고 `run.py`가 앞에 붙임. `Worker/Coder` → `Generator`.
- **TASKS 선정**: Planner는 정확히 1개를 골라 `▶`를 붙이고, Generator는 `▶` 항목(없으면 첫 미완료)을 구현.
- **합의 후 정리**: Planner는 파일을 지울 수 없으므로, PRD에 반영된 `합의:` 주제 파일은 `run.py`가 `decisions/resolved/`로 옮김. Evaluator는 `합의:`가 있는 주제를 다시 평가하지 않음.
- **긴 프롬프트**: 명령행 인자 대신 stdin으로 전달(Windows 명령행 길이 제한 회피).
- **사내 경로 가드**: 작업 디렉터리 경로에 `OneDrive - `가 있으면 거부. `HARNESS_DENY_PATHS`(쉼표 구분)로 바꾸고 `HARNESS_ALLOW_ROOT=1`로 해제.
- **분석 작업 기준**: Evaluator가 단위·샘플 수·측정 조건이 빠진 분석 결과를 사양 충족 0점(FAIL)으로 처리.
- **평가셋**: `evals/`에 정답이 정해진 작은 PRD 3개(`slugify`, `wordcount`, `rpn`)와 숨은 채점 테스트. `harness.bench --run`이 임시 폴더에서 하네스를 돌리고 사이클 수·DONE/HALT·소요 시간·채점 결과를 `evals/results.jsonl`에 프롬프트 해시와 함께 기록.

## 모델과 effort 설정

에이전트별 모델과 추론 강도는 [src/harness/agents.toml](src/harness/agents.toml) 한 파일에서만 관리합니다.

```toml
[planner]
backend = "claude"     # 생략하면 claude. "codex"도 가능
model = "claude-opus-5-5"
effort = "high"        # low | medium | high | xhigh | max (codex는 ultra도)
```

- 섹션: `bootstrap`(PRD 대화형 작성), `planner`, `generator`, `evaluator`. 네 개 모두 있어야 합니다.
- `backend`, `model`, `effort`는 각각 생략할 수 있고, 생략하면 CLI 기본값을 씁니다. 모델은 별칭 대신 고정 ID를 권장합니다.
- 오타(알 수 없는 섹션·키, 잘못된 effort 값)는 첫 호출 전에 시작 단계에서 오류로 막습니다.
- 시작할 때 역할별 설정을 로그에 찍고, `docs/runs.jsonl`에도 호출마다 `model`, `effort`를 기록합니다.
- 다른 설정으로 비교 실험을 하려면 `AGENTS_CONFIG=다른파일.toml uv run python -m harness.bench --run`.

### Codex 에이전트

`backend = "codex"`를 주면 그 역할을 `claude -p` 대신 `codex exec`로 실행합니다. 지금은 evaluator가 `gpt-6.1-sol`, effort `high`입니다. Generator(Claude)와 다른 계열이라 자기 코드를 후하게 채점하는 편향이 가장 작습니다. `bootstrap`은 대화형 claude 세션이라 codex로 둘 수 없습니다.

- **codex CLI 버전**: 모델 ID는 CLI 버전의 카탈로그에 올라와 있어야 합니다. 0.153.4에서는 `gpt-6.1-sol`이 "지원하지 않는다"며 거부됐고, 0.159.3으로 올리자 동작했습니다. 같은 오류가 나면 `npm i -g @openai/codex@latest`부터 하세요. 쓸 수 있는 모델 목록은 `~/.codex/models_cache.json`에 있습니다.
- **쓰기 범위**: `-s workspace-write` 샌드박스라 작업 폴더(와 임시 폴더)에만 쓸 수 있고 승인 프롬프트는 꺼 둡니다. `--ignore-user-config`는 쓰지 않습니다. Windows에서 이 플래그를 주면 샌드박스가 read-only로 떨어져 `docs/REVIEW.md`도 못 쓰는 것을 실측했습니다. 대신 `notify` 훅만 비웁니다.
- **도구 제한이 없습니다**: Claude의 `Edit(docs/**)`, `Bash(uv run *)` 같은 도구별 허용 목록이 codex에는 없습니다. 그래서 planner/evaluator가 codex일 때 `run.py`가 호출 뒤에 git 변경 목록을 보고, `docs/`(와 `uv.lock`) 밖 파일이 바뀌었으면 중단합니다(종료 코드 3). git 저장소가 아니면 검사하지 못하고 경고만 냅니다.
- **uv 캐시**: 샌드박스가 uv 기본 캐시 경로에 쓰지 못해 `uv run`이 실패하는 것을 실측했습니다. codex 호출에서만 `UV_CACHE_DIR`을 임시 폴더로 돌립니다(이미 설정돼 있으면 그대로 둡니다). `UV_PROJECT_ENVIRONMENT`로 venv를 작업 폴더 밖에 뒀다면 codex는 거기에 쓰지 못하므로, 환경이 최신일 때만 `uv run`이 됩니다.
- **네트워크**: `workspace-write`에서는 네트워크가 막힐 수 있어, codex가 `uv run`으로 새 의존성을 받아야 하면 실패할 수 있습니다. evaluator는 보통 이미 설치된 환경에서 테스트만 돌리므로 영향이 작습니다.
- **로그**: codex의 stdout은 진행 로그라서 마지막 메시지(`-o`)만 출력하고, `runs.jsonl`에는 `backend`가 함께 기록됩니다.

## 환경변수

| 이름 | 기본값 | 의미 |
|---|---|---|
| `MAX_ITERATIONS` | 8 | 한 번 실행에서 돌 최대 사이클 |
| `AGENT_TIMEOUT` / `CHECK_TIMEOUT` | 1800 / 600 | 에이전트 호출 / 검사 1회 상한(초) |
| `AGENTS_CONFIG` | `src/harness/agents.toml` | 모델·effort 설정 파일 경로 (아래 참고) |
| `USAGE_STOP` / `USAGE_MAX_AGE` / `USAGE_STRICT` | 0(꺼짐) / 1800 / 0 | 사용량 중단 비율(0이면 비율 기준 없음) / 캐시 허용 나이(초) / 캐시 불량 시 중단 |
| `GIT_CHECKPOINT` | 1 | 에이전트마다 git 커밋 |
| `HARNESS_DENY_PATHS`, `HARNESS_ALLOW_ROOT` | `OneDrive - `, – | 작업 디렉터리 거부 경로와 해제 |
| `GENERATOR_EXTRA_TOOLS`, `EVALUATOR_EXTRA_TOOLS` | – | 추가로 허용할 도구 패턴 (쉼표 구분) |
| `BENCH_MAX_ITERATIONS` | 10 | 평가셋 케이스당 최대 사이클 |

## 한계 (알고 쓰세요)

- 도구 패턴은 실수 방지용이지 샌드박스가 아닙니다. `uv run python -c "..."`처럼 허용된 명령으로 임의 코드를 돌릴 수 있습니다. 진짜 격리가 필요하면 WSL2나 컨테이너 안에서 돌리세요.
- 권한 패턴 문법(`Bash(uv run *)`)은 Claude Code 버전마다 다를 수 있습니다. 첫 실행 때 에이전트가 필요한 명령을 거부당하는지 `docs/harness.log`와 출력에서 확인하고, 필요하면 `*_EXTRA_TOOLS`로 추가하세요.
- git 체크포인트는 프로젝트 폴더가 git 저장소 안에 있을 때만 동작합니다. 저장소가 아니면 건너뛰고(touch 검사도 같이 건너뜀) 로그만 남깁니다.
- 사용량 가드는 VS Code 확장이 갱신하는 캐시에 의존합니다. VS Code가 닫혀 있으면 낡은 값이 되므로 경고가 뜹니다.
- `touch` 검사는 TASKS의 `touch:` 줄에 백틱으로 감싼 경로가 있어야 동작합니다. 없으면 검사를 건너뜁니다.
