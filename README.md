# Velocity-Mode

Velocity-Mode (`vmode`) is a Python 3.11+ coding-agent CLI with a Vim-inspired
terminal editor. It streams model responses and supports bounded tool calls.
It includes Hugging Face and generic OpenAI Chat Completions-compatible
providers, plus a deterministic mock for offline development and tests.

## Install

On Linux, install the latest version from the public GitHub repository with:

```sh
curl -fsSL https://raw.githubusercontent.com/josnin/velocity-mode/main/install.sh \
  -o install.sh
less install.sh
sh install.sh
```

The installer uses Python 3.11 or newer, creates an isolated virtual
environment under `$XDG_DATA_HOME/vmode/venv` (or
`~/.local/share/vmode/venv` when `XDG_DATA_HOME` is unset), and places a
`vmode` launcher in `~/.local/bin`. It does not use `sudo` or modify shell
startup files. If `~/.local/bin` is not on your `PATH`, add it using your
shell's normal configuration. Re-run the installer to update. The installer
requires Git when installing from the GitHub URL; when run from a Git
checkout, it installs that checkout instead.

## Run

```sh
vmode --help
vmode --version
vmode
vmode --list-sessions
vmode --resume
vmode "Inspect @README.md and summarize it"
vmode --exec --provider mock --mock-scenario read-file \
  "Inspect @README.md and summarize it"
```

The final command exercises the complete offline model → `read_file` tool →
tool-result message → second model response cycle. The default mock only
returns a deterministic response; it never accesses files or processes.

## Install from source

For development or to install from a local checkout:

```sh
git clone https://github.com/josnin/velocity-mode.git
cd velocity-mode
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
```

Interactive sessions are automatically saved locally as you type and as model
responses arrive. If the TUI closes unexpectedly, run `vmode --resume` to
continue the most recently updated session in the current workspace, or use
`vmode --list-sessions` and `vmode --resume <session-id>` to choose one.
Session provider/model and its effective tool-calling setting are restored
automatically; configure credentials and any custom endpoint as usual. Remove a
session with `vmode --delete-session <session-id>`. Sessions are
workspace-specific and the newest 25 are retained.

Session files are stored under `${XDG_STATE_HOME:-~/.local/state}/vmode`,
with private directory/file permissions. They contain prompts, responses,
tool messages/results, and any explicitly referenced file contents; they do
not contain provider credentials. Treat the local state directory as
potentially sensitive, and delete sessions you no longer need.

## Hugging Face

Create a fine-grained Hugging Face access token with permission to call
[Inference Providers](https://huggingface.co/docs/inference-providers/en/index),
then export it for the current shell:

```sh
export HF_TOKEN="your_hugging_face_token"
vmode --provider huggingface --model "openai/gpt-oss-120b:cheapest"
```

For one-shot use:

```sh
vmode --exec --provider huggingface --model "openai/gpt-oss-120b:fastest" \
  "Explain the selected source"
```

For an interactive coding task, start Velocity-Mode without `--exec` so it can
ask you to approve file writes and commands. Tool calling is enabled by default
for the Hugging Face provider; use a model/provider route that supports tool calls.
The confirmed Phi-3 route is available for text inference, but tool-call
support is not guaranteed. For a coding task, prefer a model documented to
support tool calling, such as `openai/gpt-oss-120b`. An existing TOML setting
or `VMODE_TOOL_CALLING=false` can still disable tools; check `:status` in the
interactive UI or pass `--tool-calling` to override it. The header also shows
`tools:on` or `tools:off`.

Example interactive task:

```sh
vmode --provider huggingface --model "openai/gpt-oss-120b:cheapest" \
  --tool-calling \
  "Create a minimal hello-world CLI in this workspace. Inspect the project, create the needed file, and run it. Ask my approval before writing or running commands."
```

Do not add `--exec` to this command: noninteractive mode deliberately denies
file writes and commands rather than approving them automatically.

If the router returns `model not found`, check that the model currently has an
Inference Providers route. When a provider is listed, pin it explicitly rather
than relying on `:cheapest`. For example, Hugging Face currently lists
`microsoft/Phi-3-mini-4k-instruct` on Featherless AI:

```sh
vmode --exec --provider huggingface \
  --model "microsoft/Phi-3-mini-4k-instruct:featherless-ai" \
  "Reply with OK"
```

The Hugging Face adapter calls the official router's streaming Chat
Completions endpoint, `https://router.huggingface.co/v1/chat/completions`.
Configure a custom provider-compatible HTTPS base URL with `--base-url` or
TOML. Model IDs, provider routing suffixes, tool support, availability,
context limits, and streaming behavior vary. The example model IDs are not
availability or pricing guarantees. Hugging Face inference may consume
credits or require payment. Velocity-Mode does not estimate costs.

An optional live smoke request requires explicit opt-in, a token, and a model:

```sh
HF_TOKEN="your_hugging_face_token" \
HF_MODEL="openai/gpt-oss-120b:cheapest" \
  vmode --live-smoke
```

It can use paid credits and fail for account, model-routing, or service reasons.
It is not part of the test suite.

## Other OpenAI-compatible providers

Use `openai-compatible` for providers that implement the streaming OpenAI Chat
Completions API. Set their API key in `VMODE_API_KEY` and pass the provider's
HTTPS API base URL and model. For example:

```sh
export VMODE_API_KEY="your_provider_api_key"
vmode --provider openai-compatible \
  --base-url "https://api.example.com/v1" \
  --model "your-model-id"
```

The endpoint base URL is the API prefix (typically ending in `/v1`);
Velocity-Mode appends `/chat/completions`. The adapter sends a bearer
authorization header, requests SSE streaming, and parses common OpenAI Chat
Completions message, tool-call, and usage shapes. Provider-specific endpoints
can differ: use only documented compatible endpoints, and disable tool calling
unless the selected model and route explicitly support it. The generic
provider does not assume function calling; enable it with `--tool-calling` or
`tool_calling = true` only when the endpoint supports the tool schema.
Endpoint-specific model names, request limits, usage reporting, stream options,
tool-call behavior,
availability, and billing are controlled by the provider. This adapter is
not a universal client for every model API and has not been live-tested
against every vendor.

```toml
[provider]
name = "openai-compatible"
model = "your-model-id"
base_url = "https://api.example.com/v1"
timeout = 300
max_output_tokens = 4096
tool_calling = false
```

## Configuration

Configuration is read from `~/.config/vmode/config.toml` or `VMODE_CONFIG`.
CLI values override environment values, which override TOML and built-in
defaults:

```toml
[provider]
name = "huggingface"
model = "openai/gpt-oss-120b:cheapest"
base_url = "https://router.huggingface.co/v1"
timeout = 300
max_output_tokens = 4096
tool_calling = true

[agent]
max_iterations = 24
```

The agent allows up to 24 provider request/response iterations per task by
default (maximum 100). Tool calls that need follow-up model work consume
iterations; tasks with many sequential tool steps may need more. Increase it
with `--max-iterations 40` or `[agent] max_iterations = 40`. The run still
obeys the configured overall timeout and tool-call limits. If the limit keeps
being reached, check the transcript for repeated tool calls or a model that
does not produce a final answer; raising the limit alone may just allow an
unproductive loop to continue.

An entire task has a 300-second timeout by default. Repository reviews and
other larger tasks can take longer; increase the budget up to 600 seconds with
`--timeout 600`, `[provider] timeout = 600`, or `VMODE_TIMEOUT=600`. This
setting covers the whole agent run and each individual provider request. It
does not increase the separate 60-second limit for an approved command.

Each provider response is capped at 4096 generated tokens by default to bound
response size and potential usage. This is separate from the input/context
window and the overall task timeout. If a response reaches the cap, Velocity-Mode
keeps the partial response and reports that it may be incomplete. For a model
that supports a larger output, raise the cap with
`--max-output-tokens 8192`, `VMODE_MAX_OUTPUT_TOKENS=8192`, or
`[provider] max_output_tokens = 8192`. Provider/model output limits vary, so use
a value supported by the selected endpoint; a larger cap permits more generated
tokens and may increase usage. For very large reviews, you can also ask for
findings in focused sections.

Supported environment overrides are `VMODE_PROVIDER`, `VMODE_MODEL`,
`VMODE_BASE_URL`, `VMODE_TIMEOUT`, `VMODE_MAX_OUTPUT_TOKENS`,
`VMODE_TOOL_CALLING`, and `VMODE_CONFIG`. Hugging Face uses `HF_TOKEN`;
OpenAI-compatible endpoints use `VMODE_API_KEY`. Credentials are only read
from the environment at runtime. Tool calling defaults on for Hugging Face,
off for OpenAI-compatible endpoints and the default mock, and on for the mock
`read-file` scenario.
Disable it with `--no-tool-calling`, `VMODE_TOOL_CALLING=false`, or `tool_calling = false` if the selected
model/provider does not support tools.
`HF_TOKEN` is read at runtime and is never accepted in TOML or CLI arguments.
The token is sent only as an HTTPS Bearer authorization header. Redirects are
disabled, TLS verification remains enabled, and authorization headers are not
logged. Do not place credentials in project files or commit them.
The old `~/.config/vima/config.toml` location and `VIMA_*` environment
variables remain as migration fallbacks; `VMODE_*` values take precedence.

## Interactive keymap

The screen uses a simple conversation transcript with `You` and
`Velocity-Mode` labels, a persistent prompt, and a live INSERT/NORMAL
indicator. The header shows the full workspace path. Press **Ctrl+S** to submit.
**Ctrl+D** exits only when the prompt has focus; while browsing results it
performs Vim's half-page-down motion. During a task, you can keep drafting the
next prompt, browse existing results, and use
`:status`, `:usage`, or `:cancel`; a second submission is refused without
discarding the draft. Tasks run sequentially because they share conversation
history and approval state. Cancel the active task with **:cancel** or
**Ctrl+C** before exiting. An empty **Enter** adds a newline; **:s** remains a
submit-command fallback.

In the prompt, **Esc** enters Normal mode; **h/j/k/l**, **^/$**, **0**, and
**gg/G** navigate. Normal-mode **/** opens the command palette in the prompt;
in the response browser it starts a case-insensitive Vim-style search, with
**n/N** repeating forward/backward and wrapping through the transcript.
**:w <file>** saves the selected completed response to a workspace-relative
file. **:** opens commands
such as **:status**, **:usage**, **:cancel**, **:results**, and **:help**.
Insert-mode **@** completes workspace files. Enter accepts a completion;
Escape dismisses it.

Press **Tab** in Normal mode to browse responses. Use **h/j/k/l** to scroll,
**Ctrl+D** for half a page down, **[** for the older response and **]** for the
newer response targeted by save/copy. The current target is shown beside the
mode indicator and its response heading is highlighted in the transcript;
changing the target scrolls to that response. Use **v** or **V** then **y** to
copy a selection; plain **y** copies the selected response. Tab returns to the
prompt.
Clipboard copying uses terminal OSC 52 support. From the response browser,
**:w <file>** saves the selected response (not the prompt); paths must stay
inside the workspace and target an existing directory.

Velocity-Mode keeps a stable system/tool prefix and the latest four
conversation turns, up to 60,000 serialized characters of older turns. Old
turns are removed as whole groups so tool-call/result pairs stay valid; the current task and its
tool cycle are not truncated by this history limit. Hugging Face/provider
prefix caching is automatic and provider-dependent; Velocity-Mode does not claim a
cache hit or cost reduction. `:usage` reports cached-input tokens only when
the provider returns that metadata. Explicit `@file` context and the current
task are still sent as requested.

The model receives only files explicitly referenced as `@path` or
`@"path with spaces"`. References are resolved inside the selected workspace;
traversal, symlink escapes, excluded paths, binary files, and oversized
context are rejected. This mechanism does not attach the whole repository.

Enabling tool calling sends the fixed registry's tool definitions, but does
not guarantee the selected model supports or reliably follows them. Mock
`read-file` mode enables tools deterministically for offline end-to-end tests.

## Tools and safety

The tool registry is allowlisted: `read_file`, `list_files`, `search_files`,
`write_file`, and `run_command`. Writes and commands require interactive
approval. Noninteractive `--exec` refuses those operations and exits with an
error. Commands use argument arrays (never a shell string), run from the
workspace, and have time/output bounds. File paths are confined to the
workspace. These checks are not an operating-system sandbox; do not approve
commands or modifications you do not understand.

The agent limits iterations, wall-clock time, streamed response characters,
and tool output. Tool call IDs and argument objects are validated, and tool
results are sent in dedicated `tool` messages rather than flattened into user
messages.

## Workspace instruction files

When starting an interactive or `--exec` task, Velocity-Mode reads optional
root-level `AGENTS.md` and `SKILLS.md` files in the selected workspace and
adds their UTF-8 contents to the system instruction context. This lets
projects provide their normal contribution guidance and skill notes without
copying them into every prompt.
Each file is limited to 32 KB and the combined content to 64 KB. Missing files
are ignored; symlinks, non-regular files, invalid UTF-8, and over-limit files
are rejected. These files are project-provided context, not trusted policy:
they cannot override approval requirements, workspace restrictions, available
tools, or higher-priority safety rules. Review their contents as you would
other repository instructions.

## Selectable workspace skills

Optional task-specific skills live in `.vmode/skills/*.md`. A skill uses TOML
front matter followed by its guidance:

```toml
+++
id = "python-tests"
version = "1.0.0"
description = "Run and interpret the Python test suite."
tags = ["python", "testing"]
triggers = ["pytest", "test"]
+++
Use the existing test helpers and run the focused suite first.
```

Velocity-Mode safely loads regular UTF-8 files only and first matches skills
deterministically from their ID, tags, and triggers. Tied candidates use a
validated provider-neutral JSON selection request; invalid selector output
falls back to the tied deterministic candidates. Selected skill contents,
declared versions, and content hashes are frozen for the entire task, then
the catalog is reloaded for the next task. Skills are appended after the
user task, preserving the stable system prompt and tool-schema prefix used by
provider prefix caches. `SKILLS.md` remains general workspace guidance rather
than a selectable skill.

## Tests and development

```sh
python -m pytest -q tests/python
```

Tests use deterministic providers, injected HTTP transports, and fake
credentials. The routine suite makes no network request and requires no
Hugging Face account or inference credits. The package provides the `vmode`
console entry point and can also be run as `python -m vmode`. Existing
installations of the old `vima` distribution should migrate with:

```sh
python -m pip uninstall vima
python -m pip install -e '.[test]'
```

See [architecture](docs/ARCHITECTURE.md), [provider contract](docs/PROVIDER_CONTRACT.md),
[security](docs/SECURITY.md), and [roadmap](docs/ROADMAP.md).
