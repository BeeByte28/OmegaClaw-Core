# Reference — Python and Prolog Bridges

MeTTa handles reasoning and control flow; bridges handle everything that needs a library ecosystem.

## `src/logger.py`

Centralised logging setup. Called once at startup from `lib_llm_ext.py`; all other Python modules obtain a logger through `get_logger` rather than calling `logging.getLogger` directly.

| Function | Purpose |
|---|---|
| `setup_logging()` | Configures the root logger with a stdout handler and a `TimedRotatingFileHandler` that rotates at midnight and keeps 7 days of backups. Idempotent — safe to import from multiple modules. Falls back to stdout-only if the log directory is not writable. |
| `get_logger(name)` | Returns `logging.getLogger(name)`. Use this instead of calling `logging.getLogger` directly so the relationship to the shared setup is explicit. |
| `log_debug(msg, module)` | MeTTa bridge — write a DEBUG entry under logger `module`. |
| `log_info(msg, module)` | MeTTa bridge — write an INFO entry under logger `module`. |
| `log_warning(msg, module)` | MeTTa bridge — write a WARNING entry under logger `module`. |
| `log_error(msg, module)` | MeTTa bridge — write an ERROR entry under logger `module`. |

The MeTTa bridge functions are invoked from `.metta` files via `py-call`, passing the source filename as `module` so log lines are attributed correctly:

```metta
(py-call (logger.log_info "Initializing memory" "memory"))
```

**Log format** — every line follows:

```
YYYY-MM-DD HH:MM:SS | LEVEL    | module | message
```

**Log level** — controlled by the `LOG_LEVEL` environment variable (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). Defaults to `INFO`. Invalid values are ignored and `INFO` is used.

**Log file** — written to `logs/omegaclaw.log` relative to the repository root, rotated nightly. The `logs/` directory must be writable by the process user. When running in Docker the `entrypoint.sh` sets ownership of `logs/` to `nobody` (uid 65534) before the agent starts. If you mount a host directory over `logs/`, ensure it is writable by the same user:

```bash
mkdir -p ./logs && chown 65534:65534 ./logs
```

## `lib_llm_ext.py`

LLM and embedding bridges.

| Function | Purpose |
|---|---|
| `useClaude(prompt)` | Call an Anthropic Claude model. Used when `provider = Anthropic`. |
| `useMiniMax(prompt)` | Call MiniMax. Used when `provider = ASICloud` (or similar routing). |
| `useAsi1(prompt)` | Call ASI1. Used when `provider = ASIOne`. |
| `useLocalEmbedding(str)` | Compute an embedding with a locally loaded model. Used when `embeddingprovider = Local`. |
| `initLocalEmbedding()` | Load the local embedding model once at startup. |

OpenAI calls go through MeTTa-side helpers (`useGPT`, `useGPTEmbedding`) that are defined elsewhere in the library but use the same LLM call pattern.

## `src/agentverse.py`

Remote agent bridge.

| Function | Purpose |
|---|---|
| `tavily_search(query)` | Forward a query to the remote Tavily search agent. |
| `technical_analysis(ticker)` | Forward a ticker to the remote technical analysis agent. |

Both use a fixed Agentverse address and return the remote agent's reply as a string. Add your own function following the same pattern — see [tutorial-06-remote-agentverse-skills.md](./tutorial-06-remote-agentverse-skills.md).

## `src/helper.py`

String and time utilities used by the loop.

| Function | Purpose |
|---|---|
| `balance_parentheses(str)` | Attempt to repair mismatched parentheses in LLM output before `sread` parses it. |
| `normalize_string(obj)` | Render a skill return value into a string safe to embed in the next prompt. |
| `around_time(ts, n)` | Backs `(episodes ts)` — returns `n` lines of `memory/history.metta` around `ts`. |

## `src/skills.pl`

Prolog helpers imported via `import_prolog_functions_from_file`.

| Predicate | Purpose |
|---|---|
| `shell/2` | Run a shell command and capture stdout. Rejects apostrophes. |
| `first_char/2` | Return the first character of a string — used by the loop to detect whether the LLM produced a valid s-expression. |

## Calling conventions

- MeTTa to Python: `(py-call (module.function arg1 arg2 ...))`.
- MeTTa to Prolog: `(translatePredicate (predicate ...))` for side-effecting predicates, or `!(import_prolog_function name)` to lift a Prolog function into MeTTa.

## See also

- [reference-internals-loop.md](./reference-internals-loop.md) — where these bridges are invoked.
- [reference-internals-extension-points.md](./reference-internals-extension-points.md) — where to add new bridges.
