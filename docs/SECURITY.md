# Security

Velocity-Mode can read workspace files and, after explicit approval, write files or run
commands. Workspace checks reduce accidental scope but are not an operating
system sandbox.

## Credentials and provider traffic

- Hugging Face credentials come from `HF_TOKEN`; generic OpenAI-compatible
  endpoints use `VMODE_API_KEY`. Credentials are read at runtime only.
  Velocity-Mode does not read tokens from TOML or command-line options, persist
  tokens, or edit shell configuration.
- The token is sent only in an HTTPS Bearer authorization header; it is not
  included in the URL or JSON body. Authorization headers are not logged and
  echoed token text is redacted from streamed output.
- TLS certificate verification remains enabled. Redirects are disabled so
  credentials are not forwarded to a redirect target.
- The default Hugging Face host is `router.huggingface.co`. A configured
  OpenAI-compatible HTTPS base URL also receives the configured bearer key;
  configure only an endpoint you trust.
- Requests have bounded timeouts. Retry is limited to one transient retry;
  authentication and credit/payment errors are not retried. Hugging Face
  inference and other hosted providers may consume credits or require payment.
- Tool calling defaults on for Hugging Face and off for generic
  OpenAI-compatible endpoints. Enable it for the latter only when the selected
  model and route document support. Enabling it sends tool definitions but
  does not establish that a particular model supports or reliably follows
  them.
- Routine tests use fake credentials and injected HTTP transport. They do not
  contact Hugging Face.

## Workspace and tool boundaries

- Only fixed tool names are accepted. Tool arguments must be objects with
  allowed fields and expected types.
- Paths are resolved under the configured workspace; traversal, symlink
  escapes, excluded generated/vendor directories, and matching root
  `.gitignore` patterns are rejected. File references attach only explicitly
  selected files and obey per-file/aggregate limits.
- `write_file` and `run_command` require a user approval callback. The
  interactive UI shows the write diff or argv before approval. Noninteractive
  mode denies these operations and reports failure.
- Interactive `:w <file>` explicitly saves the selected completed model result
  inside the workspace. It never saves the prompt draft; model-requested file
  writes continue to go through the approval callback.
- `run_command` uses an argv list without a shell, sets the working directory
  to the workspace, and applies time/output limits. It inherits the invoking
  process's permissions and environment. Do not approve untrusted commands.
- Workspace discovery and content search invoke `rg` directly with fixed
  arguments and a workspace working directory; model input is never treated
  as a command-line flag or path. Ripgrep configuration files are disabled for
  that invocation. This mechanism is not an OS-level sandbox.
- Agent iterations, elapsed time, response size, command duration, and tool
  output are bounded. Tool output is passed back as a structured tool result.
- The agent is not a sandbox; malicious or incorrect model output remains
  untrusted. Review proposed changes and commands.

The adapters use streaming OpenAI Chat Completions, not the Responses API or
arbitrary Inference Endpoints. Model-specific capabilities and account access
are not assumed. A `400`, `401`, `403`, `404`, `408`, `429`, timeout, or 5xx
error is reported without exposing authorization headers. Check key
permissions, model routing/availability, billing/credits, or `Retry-After` as
indicated.

## Workspace instruction files

Optional root-level `AGENTS.md` and `SKILLS.md` files are included in model
context when present. They must be regular, non-symlink UTF-8 files and are
subject to size limits. Treat their contents as untrusted project guidance;
they cannot override approval, workspace, tool, or other safety boundaries.
