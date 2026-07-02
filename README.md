# RefactorQ

RefactorQ is a Python/TypeScript refactoring orchestrator.

It scans a repository, turns refactoring opportunities into a structured Candidate IR, plans a bounded batch, applies deterministic or guarded changes, verifies the result, and rolls back on failure.

## Current scope

- Python adapter with structural candidate detection
- TypeScript worker-backed adapter
- `scan -> plan -> apply -> verify -> report -> run` CLI pipeline
- bounded Codex guarded execution for selected candidate kinds
- mixed-language boundary awareness
- verification/readiness/proof reporting
- optimizer backend slice in progress:
  - shared optimizer problem model
  - Greedy baseline backend
  - `QuboLocalSearchSolver`
  - solver proposal + planner revalidation metadata
  - canonical `optimizer_rejected_no_batch` run-path state

## Core architecture

### 1. Candidate IR
Each candidate carries structured planning data instead of raw text suggestions:

- `estimatedBenefit`
- `estimatedRisk`
- `estimatedDiff`
- `boundaryImpact`
- `requiredChecks`
- `applyModeHint`
- `dependencies` / `conflicts`

### 2. Planning
Planning lives under `refactorq/core/planning/`.

Current planner responsibilities:
- mode-specific filtering (`safe`, `balanced`, `report`)
- conflict / dependency / synergy edge construction
- bounded batch selection
- solver proposal normalization
- optimizer problem generation for Greedy and QUBO-local-search backends
- planner-side proposal revalidation metadata

Authority boundary:
- optimizer backends only propose candidate subsets
- RefactorQ planner remains authoritative
- dependency admission, boundary proof/readiness, and required checks stay in planner/verification authority

### 3. Execution
Execution lives under `refactorq/core/execution/`.

- low-risk deterministic application for supported auto candidates
- guarded Codex execution for bounded candidate scopes
- candidate-id / touched-file / same-file diff guardrails
- repair flow for guarded failures
- rollback on verification or git-finalization failure
- fail-closed `optimizer_rejected_no_batch` path for rejected optimizer proposals

### 4. Verification
Verification lives under `refactorq/core/verification/`.

- Python parse / lint / typecheck / tests
- TypeScript checks and build scripts
- boundary contract and integration validation
- proof/readiness summaries for boundary-sensitive execution

## Optimizer backend slice

The current optimizer work is intentionally bounded.

In this slice:
- the optimizer is **selection-only**
- it operates on language-agnostic Candidate IR
- hard constraints inside the optimizer are limited to:
  - conflict
  - file budget
  - mode budget
- planner revalidation remains authoritative
- full optimizer rejection is represented as `optimizer_rejected_no_batch`
- no same-attempt heuristic fallback is performed in that rejection branch

Artifacts produced during development include:
- `artifacts/optimizer-comparison-report.json`
- `artifacts/optimizer-comparison-summary.txt`
- `artifacts/optimizer-comparison-rationale.json`
- `artifacts/optimizer-real-run-artifact.json`
- `artifacts/optimizer-run-path-artifact.json`
- `artifacts/optimizer-slice-test-report.txt`

## CLI

```powershell
refactorq scan <repo>
refactorq plan <repo> --mode safe
refactorq apply <repo> --mode safe
refactorq verify <repo>
refactorq report <repo> --mode report
refactorq run <repo> --mode balanced
```

## Local setup

```powershell
git clone https://github.com/shinyeonjun/RefactorQ.git
cd RefactorQ
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .[dev]
npm install
npm run ts:build
```

## Verification

```powershell
python -m pytest -q
python -m mypy refactorq tests
npm run ts:check
npm run ts:build
```

## Repository map

- `refactorq/cli/main.py` - CLI entrypoints
- `refactorq/core/service.py` - top-level orchestration
- `refactorq/core/planning/` - planning and optimizer seams
- `refactorq/core/execution/` - application / rollback pipeline
- `refactorq/core/verification/` - repo verification and proof summaries
- `refactorq/agents/codex/` - guarded Codex backend
- `workers/ts-adapter/` - TypeScript worker
- `tests/` - regression coverage

## Positioning

RefactorQ is not a free-form coding agent wrapper.

It is an orchestrator that keeps final authority over:
- which candidates are admissible
- which batch is authoritative
- which execution path is allowed
- which verification evidence is required
- when rollback is mandatory
