# Velocity-Mode user manual

Velocity-Mode (`vmode`) is a keyboard-first coding-agent CLI with a
Vim-inspired terminal interface. It streams model responses, can inspect and
change the selected workspace through approved tools, and saves resumable local
sessions.

## Start

```sh
vmode
vmode "Inspect @README.md and summarize it"
vmode --workspace /path/to/project
```

Without `--workspace`, VMode uses the nearest parent Git root, or the current
directory outside a Git repository. Use `@path` or `@"path with spaces"` to
attach a specific workspace file; VMode does not attach the whole repository
by default.

Use the deterministic offline mock to try a complete tool cycle:

```sh
vmode --exec --provider mock --mock-scenario read-file \
  "Inspect @README.md and summarize it"
```

## Choose a provider

Configure credentials in the environment, then select a provider and model:

```sh
export GEMINI_API_KEY="..."
vmode --provider gemini --model gemini-3.8-flash

export GROQ_API_KEY="..."
vmode --provider groq --model openai/gpt-oss-20b --max-history-chars 16000
```

Hugging Face uses `HF_TOKEN`; generic compatible endpoints use
`VMODE_API_KEY`; Ollama does not require an API key. See the provider sections
in the [README](../README.md) for supported endpoints and examples.

### Generic inference APIs

The named providers are convenient defaults, not the only supported hosted
services. Use `openai-compatible` for an API that documents **streaming OpenAI
Chat Completions compatibility**:

```sh
export VMODE_API_KEY="..."
vmode --provider openai-compatible \
  --base-url https://api.example.com/v1 \
  --model vendor/model
```

This is not a universal adapter for every inference API. APIs using another
protocol, including the OpenAI Responses API or a provider-specific endpoint,
are not supported unless they expose the required streaming Chat Completions
interface. Tool calling also depends on the selected endpoint and model.

VMode does not charge for the CLI itself or estimate provider costs. A local
Ollama server may avoid hosted inference charges, while hosted free tiers,
quotas, availability, and billing remain provider/account controlled.

## Configure defaults

Put reusable non-secret settings in `~/.config/vmode/config.toml`, or point to
another file with `VMODE_CONFIG`:

```toml
[provider]
name = "groq"
model = "openai/gpt-oss-20b"
timeout = 300
max_output_tokens = 4096
tool_calling = true

[agent]
max_iterations = 24
max_history_chars = 16000
```

CLI options override environment variables; environment variables override
TOML; TOML overrides built-in defaults. Use `VMODE_PROVIDER`, `VMODE_MODEL`,
`VMODE_BASE_URL`, `VMODE_TIMEOUT`, `VMODE_MAX_OUTPUT_TOKENS`, and
`VMODE_TOOL_CALLING` for shell-level overrides. Keep API keys in their
provider environment variables, never in TOML or on the command line.

While no task is running, change provider/model without losing the current
conversation:

```text
:provider gemini gemini-3.8-flash
:provider groq openai/gpt-oss-20b
:provider openai-compatible vendor/model https://api.example.com/v1
```

The optional endpoint is saved with the session, but credentials are not.
Switching affects later requests only: it does not cancel an active request or
remove retained history from the next request.

## Prompt and results

| Action | Key or command |
| --- | --- |
| Send the draft | `Ctrl+S` or `:s` |
| Add a prompt newline | `Enter` |
| Enter Normal mode | `Esc` |
| Return to Insert mode | `i` |
| Browse results / return to prompt | `Tab` |
| Move or scroll | `h` `j` `k` `l`, `^`, `$`, `0`, `gg`, `G` |
| Half-page down in results | `Ctrl+D` |
| Search results / repeat | `/query`, `n`, `N` |
| Move the selected result | `[` older, `]` newer |
| Select/yank | `v` or `V`, then `y`; plain `y` yanks the selected result |
| Save the selected result | `:w path/to/file` |
| Inspect sent request | `:prompt` |
| Leave request inspection | `Esc` |
| Cancel a task | `:cancel` or `Ctrl+C` |
| Show status or usage | `:status`, `:usage` |
| Show help | `:help` or `/help` |
| Exit | `:q` |

`Tab` preserves the current prompt draft while you inspect results. In request
inspection, `[` and `]` select request snapshots for the chosen result. The
view is read-only: opening, searching, copying, or saving it does not submit a
request or execute a tool.

To understand what VMode sent for a selected response, press `Tab`, select the
response with `[` or `]`, then run `:prompt`. The view displays VMode's locally
captured serialized request and can be copied or saved. It excludes
authorization headers and is not the provider's private/internal prompt or a
claim about provider-side transformations.

During an approved command, the transcript shows one compact command entry:
the argv and `running…` state are replaced by the bounded final exit result.

## Approvals and safety

VMode can read the selected workspace. File writes, focused edits, commands,
Git staging, and commits require interactive approval. Press `y` to allow or
`n` to deny an approval. `--exec` is noninteractive and denies operations that
need approval.

Paths are checked against the workspace, but VMode is not an operating-system
sandbox. Review every proposed command and write. Do not put credentials in
project files, commands, prompts, or sessions.

## Sessions

Interactive sessions are saved under
`${XDG_STATE_HOME:-~/.local/state}/vmode` with private file permissions.

```sh
vmode --list-sessions
vmode --resume
vmode --resume SESSION_ID
vmode --delete-session SESSION_ID
```

Sessions contain prompts, model responses, tool messages/results, and explicit
file context. Treat the local state directory as sensitive and delete sessions
you no longer need.

`:clear` clears the visible transcript only. It does not clear the active
conversation history. Starting a fresh non-resumed VMode process begins with
new in-memory history.

## Limits and common recovery

- Prior history is bounded to four complete turns and 60,000 serialized
  characters by default. Lower it for constrained providers with
  `--max-history-chars`.
- Model output is requested with a 4,096-token default cap. Increase it with
  `--max-output-tokens` only when the chosen provider/model supports it.
- Input has no universal token cap: the system prompt, current task, explicit
  files, selected skills, retained history, and tool schemas must fit the
  selected model context window.
- A task allows 24 provider requests by default and has a 300-second timeout.
  Use `--max-iterations` or `--timeout` only when the task needs more budget.

For a rate-limit, quota, or model-availability error, wait for the provider's
reported retry interval when present, reduce retained history, or use
`:provider` to select a configured alternative. Changing providers does not
guarantee tool support, context capacity, or lower cost.

## More detail

Use the [README](../README.md) for installation, provider configuration,
context/token controls, selectable skills, and development commands. Read the
[security guide](SECURITY.md) before approving workspace operations.
