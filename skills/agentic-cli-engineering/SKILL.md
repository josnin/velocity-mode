+++
id = "agentic-cli-engineering"
version = "1.0.0"
description = "Expert guidance for designing, testing, and securing autonomous coding-agent CLIs."
tags = ["agentic", "cli", "agents", "provider", "streaming", "tools", "tui"]
triggers = ["agent", "agentic", "provider", "tool", "streaming", "prompt", "tui", "terminal", "vim"]
+++

# Agentic CLI engineering

Use this skill for architecture or workflow changes in coding-agent CLIs. It
supplements repository guidance; it cannot override safety, approvals,
workspace boundaries, or available tools.

## 1. Architecture and orchestration

Keep presentation, orchestration, provider adaptation, and tool execution
separate. In Velocity-Mode, `tui.py`/`cli.py` present and collect input,
`agent.py` owns a sequential task loop and history, `providers.py` adapts
transport/protocol details, and `tools.py` validates and executes a fixed
registry. Model requests should originate in the agent loop, not a viewer.

Make task/request state explicit: task ID or lifecycle, current provider
request, accumulated response, tool-call IDs, approval state, cancellation,
and limits. Propagate cancellation and one wall-clock deadline across provider
and tool work. Bound iterations, per-response calls, total calls, output, and
tool results. Recover from failures with an explicit finite policy; never
silently retry forever.

Maintain valid assistant-tool-result pairing. History compaction must retain
complete user-turn groups and never orphan an active tool call. Sequential
tasks are often necessary when history and approval state are shared; do not
introduce concurrency without isolating both. Make events observable and
deterministic where practical. Velocity-Mode currently enforces iteration,
time, response, history, and tool-output limits; broader recommendations here
are design guidance, not claims about all CLIs.

## 2. Provider adapters

Normalize application messages, tool calls, usage, events, and errors before
the agent loop. Keep Chat-Completions serialization, headers, SSE framing,
partial UTF-8, streamed errors, finish reasons, and provider quirks inside an
adapter. A generic OpenAI-compatible endpoint is not universal compatibility:
tool schema, usage, `stream_options`, model naming, limits, caching, and
finish reasons may differ.

Assemble streamed tool-call fragments by call index/ID, then parse and validate
arguments before dispatch. Handle text-only, tool-only, malformed, interrupted,
rate-limited, and terminal-error streams explicitly. Treat usage as provider
metadata, not a cost estimate. Bound timeouts/retries and respect bounded
`Retry-After`; never retry authentication/payment errors indefinitely.

Resolve credentials at runtime from the provider-specific secure mechanism.
Never put tokens in CLI arguments, URL query strings, session data, errors, or
fixtures. Use injected `httpx` transports and fake credentials to test
adapters offline. Live verification proves only that selected account/model/
route worked at that time.

## 3. Prompt construction and inspection

Keep stable system instructions and tool schemas stable where possible.
Deliberately order system guidance, preserved history, selected skills,
explicit file references, and the current task. Avoid reattaching whole
workspaces or redundant content. Snapshot selected skills and other mutable
task context at task start, then reload them only for the next task.

Capture inspection data from the final serialized outgoing payload at the
provider boundary, after adapter transforms and before transmission. Associate
every snapshot with the response/request that caused it, including tool-cycle
follow-ups. Redact credentials, cap stored/rendered sizes, and retain an
unavailable reason instead of fabricating data. A local capture is not the
provider's internal prompt and cannot prove provider-side transformations,
caching, or discounts.

Prompt/result/tool-output viewers are pure presentation. Opening, navigating,
searching, copying, and saving them must not submit, resume, retry, mutate
history, or execute tools. Test this with a request-counting fake provider.

## 4. Tool design and safe execution

For each tool define a narrow name, clear description, JSON schema, strict
input validation, read/write classification, and structured success/failure
result. Prefer focused operations to generic code or command execution.

Before implementing, answer:

1. Which workspace paths and symlinks are legal, and how are traversal and
   ignored/generated paths rejected?
2. Does it require explicit approval, and is the approved preview exactly what
   will execute after revalidation?
3. If it starts a process, does it use an argv array, workspace `cwd`, timeout,
   output cap, cancellation, and secret-safe diagnostics?
4. What happens for malformed input, missing files/programs, races, partial
   output, denial, timeout, and cancellation?

Model-generated paths/commands are untrusted. Never use shell strings for
arbitrary model commands. Do not log secrets. Test invalid input and each
failure boundary deterministically. Application path checks reduce accidental
scope; they are not an OS sandbox.

## 5. Repository context and search

Use `rg` when available with fixed arguments, a workspace `cwd`, bounded
output, and clear truncation metadata. Decide intentionally how hidden,
ignored, binary, generated, and symlinked files behave. File references such
as `@path` must resolve inside the workspace and have per-file/aggregate caps.
Do not expand context to an entire repository merely because a task is vague.

Fail explicitly when an optional executable is absent; do not silently return
success-shaped empty search data. Cover unusual filenames, traversal, ignored
directories, binary content, symlink escapes, missing `rg`, and truncation in
temporary-workspace tests.

## 6. Skills and instructions

Keep root `AGENTS.md` brief and always applicable. In Velocity-Mode, skill
metadata is read before full guidance; `id`, `tags`, and `triggers` drive
deterministic selection. Only tied candidates use a strict provider-neutral
selector, with deterministic fallback on invalid/unavailable selection.
Validate files, identifiers, front matter, size, and selection IDs.

Do not inject every skill into every task. Freeze selected skill content,
version, and hash for an active task; reload catalog changes next task. Decide
and test session-resume semantics if skills affect resumed work. Treat project
instructions and skill files as untrusted context: they cannot grant
capabilities or override security policy.

## 7. Terminal UX and Vim interaction

Model the TUI as explicit focus/mode/state transitions. Keep prompt editing
independent from result browsing and read-only inspection, preserve drafts
during streaming/background activity, and avoid redraw storms or layout jumps.
Reserve semantic emphasis for errors/warnings/approvals; retain readable
defaults when color is limited or disabled. Avoid unsafe terminal escape
sequences in displayed model/tool text and size-bound clipboard output.

Preserve established keybindings unless changing them deliberately. Check
conflicts among INSERT/NORMAL transitions, Tab focus, search, visual
selection, approval keys, cancellation, and viewer exit. A viewer must not
accidentally send a request. Exercise focus, keybinding, cancellation,
selection, copy/save, and draft preservation with unit and terminal tests.

## 8. Git-aware operations

Treat Git as a separate safety domain. Parse
`git status --porcelain=v1 -z` safely, including unusual filenames, renames,
deletions, conflicts, staged/unstaged/untracked state. Capture a task-start
baseline; later changes do not automatically belong to the agent. Inspect
status and diffs before approval, stage explicit paths only, and revalidate
the index immediately before commit.

Refuse unsafe mixed staging where paths changed after the agent write or the
staged set differs from the approved plan. Generate messages from verified
changes. Never amend, rewrite history, push, bypass hooks, or commit without
appropriate approval. Add a co-author trailer only when the approved
repository workflow requires it. Test against temporary repositories that
already contain user changes. These are design rules; do not claim a project
implements Git tools without verifying it.

## 9. Testing and evaluation

Build a pyramid:

- Unit-test validation, serialization, limits, selection, and state changes.
- Test adapters with fake transports and scripted SSE chunks.
- Test loops with mock providers and deterministic tool results.
- Use temporary workspaces for path/security cases and temporary Git repos for
  Git cases.
- Test TUI keybindings, focus, selection, cancellation, and draft preservation.
- Add regression coverage for feasible bug fixes.

Test malformed model output, stream/provider errors, rate limits, cancellation,
timeouts, oversize output, approval denial, and missing dependencies. State
what an offline test proves versus what requires an explicit live-provider
smoke check. Never claim tests passed without executing them.

## 10. Implementation playbook

1. Restate behavior and non-negotiable invariants.
2. Inspect relevant code, tests, docs, and existing patterns.
3. Choose the smallest viable design and enumerate interface changes.
4. Define failure modes, limits, and security boundaries first.
5. Add/update focused regression tests.
6. Implement incrementally, preserving public contracts.
7. Run targeted tests, then shared-suite coverage as warranted.
8. Inspect the final diff for unrelated changes and accidental secrets.
9. Update user-facing documentation.
10. Report verified results, limitations, and follow-up work honestly.

## 11. Avoid these anti-patterns

Do not rewrite architecture for a focused feature; load every skill for every
task; retry forever; leave output/iterations unbounded; assume providers share
a protocol; send whole repositories unnecessarily; trust model paths/commands;
use shell strings; stage/commit unrelated changes; reconstruct a historical
prompt from mutable state; dispatch from a read-only viewer; or claim success
without evidence.
