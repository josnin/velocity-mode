# Roadmap

## MVP delivered

- Python 3.11+ package and `vmode` CLI with help/version, interactive and
  scripted modes.
- Async provider protocol, deterministic mock, Hugging Face streaming Chat
  Completions adapter, usage reporting, and normalized errors.
- Bounded structured agent/tool/result/model loop with file references.
- Allowlisted file/search/list/write/command tools with workspace checks and
  approval before writes and commands.
- `prompt_toolkit` Vi editing, multiline prompt, command/file completion,
  explicit Normal-mode motions, Ctrl+S submission with `:s` fallback,
  prompt-scoped Ctrl+D exit with results half-page scrolling, a simple
  speaker-labeled transcript, selected-response saving,
  response browsing, Visual-mode yanking, asynchronous work, and approvals.
- Renamed package and executable to `vmode` under the Velocity-Mode brand;
  legacy Vima configuration paths and environment variables remain as fallback.
- Stable system/tool prefixes, bounded prior conversation context, and optional
  cached-input usage reporting when the provider returns it.
- Offline provider/tool/CLI tests and Linux CI.

## Follow-up

- Exercise more of the interactive UI and approval path in automated PTY tests
  across supported terminals.
- Add additional providers only behind the same provider protocol and offline
  contract tests.
- Add reliable provider/model capability metadata before enabling optional
  features by model.
- Add broader `.gitignore` semantics and stronger OS-level process isolation
  only when the platform support and threat model are defined.
- Consider persistent conversation history and richer usage display after
  defining storage, privacy, and retention behavior.
- Implement Responses API support only as an independent adapter with its own
  request/response tests.

No follow-up is a claim that tool execution is sandboxed or that all Hugging
Face models support function calling, streaming, or a particular billing tier.
