# obs-automation AGENTS.md

## Agent Guidelines

Python CLI tool for headless package bumping and automation in OBS. Uses: Python, osc, httpx, tenacity, typer, ruff, ty.

### Constraints

- `tasks/`: Read/write for planning. Never run git operations on this directory.
- Never run git clean or any command that deletes unversioned files. Ask for confirmation.
- Commit message format: 50/80 rule, 80-char limit, wrap in single quotes.
- **Style Exclusions**: NEVER suppress linter, type-checker, or coverage errors just to bypass them; you MUST refactor instead. Exclusions are ONLY permitted when dictated by external library interfaces.

### Build & Test Commands

- `make tidy`: Format code with ruff.
- `make checkstyle`: Run full linting (ruff + vulture).
- `make check-types`: Type checking with ty.
- `make test`: Run all tests with full coverage.

### Coverage Verification

- Always verify 100% statement AND branch coverage for all files using `make test`.
- Do NOT just look at the "passed" test count. You MUST check the exit code of `make test`. If it exits with an error (e.g. `Error 2` or `Coverage failure`), you must fix the coverage before committing.
- If an `elif` or `else` branch is logically unreachable (e.g. due to prior filtering), refactor the code to eliminate the unreachable branch to satisfy the coverage tool.

### Test Guidelines

- Prefer `@pytest.mark.parametrize` over individual single-assertion test functions for data-driven coverage. Use tuple form for argument names: `("arg1", "arg2")` not `"arg1,arg2"` (ruff PT006).
- Prefer adding test coverage in existing test files. Only when a completely new feature is implemented new test files should be considered.
