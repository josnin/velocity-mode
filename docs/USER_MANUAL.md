# Velocity-Mode user manual

Velocity-Mode (`vmode`) is a keyboard-first coding-agent CLI with a
Vim-inspired terminal interface. It streams model responses, can inspect and
change the selected workspace through approved tools, and saves resumable local
sessions.

## Learn in stages

Follow the sections in order the first time:

1. **Start** a session and send a simple task.
2. **Prompt and results** explains the small Vim keyset you need to move
   between your draft and the response.
3. **Choose a provider** when you are ready to connect a preferred model.
4. **Approvals and safety** before allowing workspace changes.
5. **Sessions** when you want to resume work later.

You do not need to memorize every command. Press `:help` in VMode whenever you
need a reminder.

## Start

```sh
vmode
vmode "Inspect @README.md and summarize it"
vmode --workspace /path/to/project
```

After you configure a provider credential, run `vmode`, write a task, and
press `Ctrl+S` to send it. Use the next section to browse the response without
losing your draft.

Without `--workspace`, VMode uses the nearest parent Git root, or the current
directory outside a Git repository. Use `@path` or `@"path with spaces"` to
attach a specific workspace file; VMode does not attach the whole repository
by default.

Use the deterministic offline mock to try a complete tool cycle:

```sh
vmode --exec --provider mock --mock-scenario read-file \
  "Inspect @README.md and summarize it"
```

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

## Choose a provider

Configure credentials in the environment, then select a provider and model:

```sh
export GEMINI_API_KEY="..."
vmode --provider gemini --model gemini-3.8-flash

export GROQ_API_KEY="..."
vmode --provider groq --model openai/gpt-oss-20b --max-history-chars 16000
```

Hugging Face uses `HF_TOKEN`; Groq uses `GROQ_API_KEY`; OpenRouter uses
`OPENROUTER_API_KEY`; generic compatible endpoints use `VMODE_API_KEY`; Ollama
does not require an API key. See [Provider notes](#provider-notes) for
provider-specific endpoints and examples.

### OpenRouter

```sh
export OPENROUTER_API_KEY="your_openrouter_api_key"
vmode --provider openrouter --model "recursive/flint-v1.0:free"
```

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
:provider openrouter recursive/flint-v1.0:free
:provider openai-compatible vendor/model https://api.example.com/v1
```

The optional endpoint is saved with the session, but credentials are not.
Switching affects later requests only: it does not cancel an active request or
remove retained history from the next request.

OpenRouter tool calling is off by default because model-route support varies.
To use VMode's workspace tools after switching to OpenRouter, start VMode with
`--tool-calling` (or set `VMODE_TOOL_CALLING=true`) before switching, and do so
only when the selected model and route document compatible tool support. VMode
shows a reminder after an OpenRouter switch if tool calling is off.

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

## Advanced reference

The sections below are optional. Return to them when your work needs the
corresponding capability.

### Context and token discipline

Velocity-Mode reduces avoidable context growth; tokenization, provider-side
caching, and billing remain provider/model specific.

| Control | Current default | What it means |
| --- | --- | --- |
| Prior conversation input | 4 complete turns; 60,000 serialized characters | Older whole turns are removed before a new task. Change the character budget with `--max-history-chars`. |
| Current input | No universal token cap | The system prompt, task, selected skills, explicit `@` files, tool schemas, and retained history must fit the selected provider/model context window. |
| Model output | 4,096 tokens | Sent as `max_tokens`; change it with `--max-output-tokens` when the selected model supports it. |
| Streamed response safety limit | 1,000,000 characters | Stops unexpectedly large streamed model output. |
| Tool result sent back to the model | 100,000 characters | Bounds one result in the next model request. |
| Provider requests per task | 24 | Bounds model → tool → model loops; configurable up to 100 with `--max-iterations`. |
| Task timeout / approved command timeout | 300 seconds / 60 seconds | The task timeout is configurable up to 600 seconds. |

Only explicit `@path` references are attached; VMode does not send the whole
repository by default. `:usage` shows provider-reported cached-input tokens
when available, but does not infer a cache hit or cost saving.

### Workspace search

The `list_files` and `search_files` tools require
[ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on `PATH`. On Fedora:

```sh
sudo dnf install ripgrep
```

Searches inspect only the active workspace, do not follow symlinks, honor
ignore rules, and exclude VCS metadata, dependency directories, virtual
environments, caches, and common build output. Results are bounded before
being sent to the model.

### Provider notes

#### Hugging Face

Create a fine-grained Hugging Face token that can call Inference Providers,
then select a model and route that document tool-call support:

```sh
export HF_TOKEN="your_hugging_face_token"
vmode --provider huggingface --model "openai/gpt-oss-120b:cheapest"
```

The adapter uses the streaming Chat Completions endpoint at
`https://router.huggingface.co/v1/chat/completions`. Model availability,
routes, tool support, and billing can change. Use `--tool-calling` only with
a compatible route and model.

#### OpenRouter and compatible endpoints

OpenRouter has a dedicated adapter:

```sh
export OPENROUTER_API_KEY="your_openrouter_api_key"
vmode --provider openrouter --model "provider/model"
```

Tool calling is off by default for OpenRouter because route support varies.
For another API, use `openai-compatible` only when it documents streaming
OpenAI Chat Completions compatibility:

```sh
export VMODE_API_KEY="your_provider_api_key"
vmode --provider openai-compatible \
  --base-url "https://api.example.com/v1" \
  --model "your-model-id"
```

The base URL is an API prefix, not a full `/chat/completions` URL.

#### Gemini, Groq, and Ollama

```sh
export GEMINI_API_KEY="your_gemini_api_key"
vmode --provider gemini --model gemini-3.8-flash

export GROQ_API_KEY="your_groq_api_key"
vmode --provider groq --model "openai/gpt-oss-20b"

vmode --provider ollama --model qwen2.5:1.5b --tool-calling
```

Gemini and Groq enable tool calling by default. Ollama uses
`http://127.0.0.1:11434/v1` by default and has no API key; enable tool calling
only for a compatible Ollama version and model. HTTP endpoints should be used
only on trusted networks.

### Install from source and test

```sh
git clone https://github.com/josnin/velocity-mode.git
cd velocity-mode
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
python -m pytest -q tests/python
```

Tests use deterministic providers and injected HTTP transports; the routine
suite makes no network request.

### Workspace instructions and skills

VMode reads optional root-level `AGENTS.md` and `SKILLS.md` files as bounded
project guidance. They are untrusted context and cannot override approvals,
workspace restrictions, or available tools.

Task-specific skills use TOML front matter in `.vmode/skills/*.md` or
`skills/<skill-id>/SKILL.md`. VMode first matches skills deterministically
from their ID, tags, and triggers; selected content is frozen for the active
task and reloaded for the next one.

Read the [security guide](SECURITY.md) before approving workspace operations,
and consult the [architecture](ARCHITECTURE.md),
[provider contract](PROVIDER_CONTRACT.md), and [roadmap](ROADMAP.md) when
needed.
