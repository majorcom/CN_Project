# AGENTS

## Purpose

Persistent instructions for AI coding agents working in this repository.

## Local Environment (Always Assume)

- OS: Windows 10 (`win32`).
- Shell: PowerShell.
- Project path contains spaces and non-ASCII characters, so always quote paths in shell commands.
- Use `uv` for dependency management and command execution.

## Project Context

- Stack: Python 3.8+, Flask.
- Package manager: `uv` (`pyproject.toml` + `uv.lock`).
- Entry point: `server.py`.
- Main app package: `app/`.
- API layers: `app/api/v1/` and `app/api/cms/`.
- Architecture: API -> service -> DAO -> models.

## Build, Run, and Test Commands

Run from repository root:

- Install dependencies: `uv sync`
- Start server (default): `uv run python server.py run`
- Start server (custom host/port): `uv run python server.py run -h 0.0.0.0 -p 8080`
- Run all tests: `uv run pytest`
- Run one test module: `uv run pytest tests/test_v1_product.py`
- Run one test case: `uv run pytest tests/test_v1_product.py -k <pattern>`

## Local DB Test Setup (Windows + Docker)

Use this flow when tests fail due to MySQL connection/auth/token issues.

1. Ensure Python 3.11 venv is used:
   - `uv python install 3.11`
   - `uv venv --python 3.11 --clear`
   - `uv sync`
2. Start local MySQL container:
   - `docker run -d --name mini-shop-mysql -e MYSQL_ROOT_PASSWORD=159951 -e MYSQL_DATABASE=zerd -p 3306:3306 mysql:8.0`
3. Ensure PyMySQL-compatible auth plugin:
   - `docker exec mini-shop-mysql mysql -uroot -p159951 -e "ALTER USER 'root'@'%' IDENTIFIED WITH mysql_native_password BY '159951'; FLUSH PRIVILEGES;"`
4. Ensure required databases exist:
   - `zerd` (app default DB)
   - `test` (used by `tests/test_v1_product.py`)
5. Seed minimal test data if needed and ensure `token.json` exists in repo root before running all tests.
6. Run tests:
   - `uv run pytest`

Notes:
- Current tests are integration-style and depend on a running MySQL instance.
- If `token.json` is missing, CMS/V1 auth tests can fail during collection.
- Do not commit generated `token.json`.

## Workflow for Lab Tasks

1. First, preserve baseline behavior: run existing tests before feature changes.
2. Explore solution options in plan/research mode before major code edits.
3. Implement the selected approach in small, reviewable steps.
4. Run tests for changed functionality and fix regressions.
5. Add/update tests for new functionality if missing.
6. Keep commits granular so it is easy to roll back to a known good point.

## Core Rules (Always Apply)

1. Make focused, minimal changes that solve the requested task.
2. Do not refactor unrelated modules without explicit request.
3. Preserve existing public API behavior unless user explicitly requests change.
4. Follow existing code style and naming conventions in touched files.
5. Keep business logic in service/DAO/model layers, not in route handlers.
6. Avoid introducing new dependencies unless necessary.
7. Never hardcode secrets, tokens, passwords, or environment-specific credentials.
8. Prefer configuration-based values from existing config modules.
9. Update docs when behavior, setup steps, or commands change.
10. If uncertain about destructive actions or data/schema changes, ask first.

## Coding and Documentation Guidelines

- Keep imports tidy and remove dead code introduced by edits.
- Add brief comments/docstrings only for non-obvious logic.
- Reuse existing helpers/utilities before creating new abstractions.
- Keep functions cohesive; avoid large monolithic handlers.
- Maintain compatibility with existing test patterns in `tests/`.
- For user-visible or API behavior changes, update `README.md` or relevant docs.

## Validation Checklist Before Handover

- Run targeted tests for changed areas (or explain why tests could not run).
- Ensure no obvious lint/format issues in modified files.
- Ensure request/response contracts remain expected.
- Ensure errors follow existing error-handling and error-code patterns.

## Non-Goals

- No broad formatting-only rewrites.
- No silent breaking changes.
- No speculative architecture migration without user request.
