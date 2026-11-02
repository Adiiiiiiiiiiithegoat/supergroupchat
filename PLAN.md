# supergroupchat v1

## Setup
- [x] git init + initial commit of spec
- [x] Verify claude CLI flags against `claude --help`

## Backend
- [x] sqlite schema (runs, threads, messages)
- [x] claude subprocess runner (stream-json parse, taskkill tree)
- [x] Loop driver: discuss → start → executor/planner rounds, auto-commit, diff stat
- [x] STATUS parsing, round cap, pause / stop / resume
- [x] Branch threads with forked planner session
- [x] REST + websocket API

## Frontend
- [x] Timeline, live streaming, top bar controls, composer
- [x] Branch side panel, phone-width layout

## Ship
- [x] run.bat + README notes on flag differences
- [x] Smoke test: real round, pause, branch

## Follow-ups
- [ ] Click-test branch panel + live streaming in a real browser (Chrome extension was down during smoke test)

## Review fixes
- [x] Planner errors posted as system so the executor report stays pending (+ test)
- [x] Pause on usage/rate limit; limited executor round not counted or committed
- [x] Default .gitignore for new workspaces
- [x] Keep user ~/.claude/CLAUDE.md out of agent sessions (claudeMdExcludes)
- [x] Reset 'running' runs to 'paused' on startup

## Hardening
- [x] Startup self-check for the CLAUDE.md exclusion (banner + console, SKIP_STARTUP_CHECK)
- [ ] Auto-resume after a usage limit (scheduled, cancellable, survives restart)
