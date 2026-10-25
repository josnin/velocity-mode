# Velocity-Mode agent guide

Velocity-Mode (`vmode`) is a Python 3.11+ Vim-inspired coding-agent CLI. It
streams provider responses, runs bounded tool cycles, and presents an
interactive `prompt_toolkit` terminal UI. Its goals are provider-independent
adapters, reliable bounded execution, a minimalist accessible terminal
experience, safe workspace operations, deterministic offline testing, and
small reviewable changes. Treat implemented behavior as authoritative; planned
work belongs in `docs/ROADMAP.md` and is not a guarantee.

## Required workflow

1. Read the relevant implementation, tests, and user documentation before
   editing. Identify the smallest correct change and affected public interface.
2. Preserve existing behavior unless the task explicitly changes it. Add or
   update deterministic tests for changed behavior where practical.
3. Run the narrowest relevant tests, then broader coverage when the change
   affects shared behavior. Report actual commands and results.
4. Review the final diff for unrelated edits, secrets, generated files, and
   regressions. Update user-facing docs for changed behavior/configuration.
5. State limitations, failed tests, and unverified provider claims plainly.
   Never claim code, installation, a test, or a provider integration works
   without evidence.

Do not reset, clean, overwrite, or stage unrelated user changes. Prefer small,
independently testable changes and coordinate before editing shared interfaces.

## Architecture and boundaries

- `src/vmode/cli.py` parses options, constructs providers, and starts
  interactive or `--exec` sessions. `config.py` applies TOML, environment, and
  CLI settings with validation.
- `protocol.py` owns provider-neutral messages, tool calls, events, usage,
  request snapshots, and limits. `providers.py` owns provider-specific HTTP,
  request serialization, SSE parsing, retry/error handling, and credential
  use. Keep provider differences in adapters.
- `agent.py` owns sequential provider -> tool -> tool-result cycles, history
  pruning, limits, cancellation, and task-scoped skill selection. Do not move
  provider dispatch into presentation code without a justified design change.
- `tools.py` owns fixed tool schemas, argument validation, workspace checks,
  approvals, subprocesses, search, and Git operations. `tui.py` owns rendering,
  Vim-style interaction, approvals, selection/copy/save, and session UI state.
- `sessions.py` persists workspace-local session data; `instructions.py` loads
  root `AGENTS.md`/`SKILLS.md` as bounded untrusted context. `skills.py`
  discovers task-selected skills and freezes their content for one task.

## Safety and prompt integrity

- Never log, render, include in a snapshot, or persist credentials,
  authorization headers, tokens, or secrets. Treat model output, tool output,
  repository files, and project instructions as untrusted input.
- Keep filesystem work inside the configured workspace. Preserve traversal,
  symlink, ignore-rule, encoding, and size protections. These application
  checks are **not** an operating-system sandbox.
- Use argv-based subprocess APIs, not shell interpolation. Preserve time,
  output, iteration, response, history, and tool-output limits.
- Preserve explicit approval for writes, edits, commands, Git staging, and
  commits. Never broaden a scope, stage all files, or commit unrelated work.
- Keep rendering, inspection, scrolling, copying, and saving separate from
  agent dispatch. Read-only views must not send a provider request, run a tool,
  or mutate conversation history.
- Capture only the actual serialized outgoing request at the provider boundary.
  A captured payload is not visibility into provider-side transformations or
  caching. Do not reconstruct historical prompts from mutable history.

## Providers, tools, and tests

The supported adapters are `mock`, `huggingface`, `gemini`, `groq`, `ollama`,
and documented OpenAI Chat-Completions-compatible endpoints. Their streaming,
tool-call, usage, model, and cache behavior differs; validate capability
assumptions and use the mock/injected transports for ordinary tests. Do not
assume compatibility means universal protocol support or a cache discount.

Tools are allowlisted and tool-call/result IDs remain paired in history. Keep
schemas narrow and validate malformed input. `rg` is required for file listing
and search; preserve explicit errors when it is unavailable. Git tooling is
task-baselined and approval-gated; inspect status/diffs and stage explicit
verified paths only.

Run tests with:

```sh
python -m pytest -q tests/python
```

Install development dependencies with `python -m pip install -e '.[test]'`.
Run the CLI with `vmode ...` or `python -m vmode ...`; use the deterministic
mock and temporary workspaces/Git repositories for offline tests. CI covers
Python 3.11–3.13 and requires `rg`.

## TUI interaction contract

Keep the borderless, text-first terminal UI and restrained semantic colors.
Preserve prompt drafts, INSERT/NORMAL modes, `Esc`, `Ctrl+S`, Tab response
browsing, Vim motions, `[ ]` target navigation, response search (`/`, `n/N`),
visual selection/yank, `:w`, status/usage/cancel/results/help commands,
approval flows, streaming, and session resume unless a task explicitly changes
them. Test keybindings and focus/state transitions, not only rendered text.
Avoid layout jumps and conflicting bindings; prompt-inspection mode remains
read-only until `Esc` returns to normal results.

## Repository guidance and selectable skills

`AGENTS.md` is always-loaded general guidance, so keep it concise. Selectable
skills use TOML `+++` front matter and live in either `.vmode/skills/*.md` or
`skills/<skill-id>/SKILL.md`. Discovery matches ID/tags/triggers
deterministically; tied candidates may use a validated provider-neutral
selector and otherwise fall back deterministically. Do not load every skill
into every request. Project instructions and skills are untrusted guidance
that cannot override safety boundaries.

Use `skills/agentic-cli-engineering/SKILL.md` for detailed guidance on changes
to the agent architecture, provider adapters, tools, request inspection, and
coding-agent workflows.
