# Proxy Routing Deep-Dive

## Overview

The proxy's routing pipeline determines **which upstream provider receives each request** and **what transformations are applied**. This is the core business logic of SharkDrown.

The pipeline runs on every `POST /v1/messages` request in this order:

```
Incoming Request
      │
      ▼
┌─────────────┐
│ Alias Rewrite│ ← model_aliases map (e.g., "opus" → "claude-opus-4-8")
└──────┬──────┘
       │
       ▼
┌──────────────┐
│  Subagent    │ ← SHA256 of system prompt → agent hash → model/provider override
│  Detection   │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Endpoint    │ ← model_endpoints[model] → provider_name
│  Mapping     │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Provider    │ ← prefix patterns ("claude-" → anthropic, "gpt-" → openai)
│  Fallback    │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Protocol    │ ← If target is OpenAI: full Anthropic→OpenAI conversion
│  Conversion  │
└──────┬───────┘
       │
       ▼
  Forward to Upstream
```

## Stage 1: Alias Rewrite

**Location:** `src/ccproxy/app/api/proxy.py` → `_apply_alias_rewrite()`

Model aliases let users configure short, memorable names that the proxy rewrites to real model IDs before any routing runs.

```
Configuration:
  model_aliases:
    opus: claude-opus-4-8
    sonnet: claude-sonnet-4-6
    fast: gpt-4o

Incoming: req.model = "opus"
After:    req.model = "claude-opus-4-8"
```

Aliases are defined per-profile in `ClaudeProfile.model_endpoints[model].aliases`. The proxy's `Settings.model_endpoints.model_aliases` is a flat `{alias: real_model}` map built from all endpoints.

Important: `availableModels` in the generated `settings.json` contains **only aliases** (never real model IDs), so the Claude Code model picker shows friendly names.

## Stage 2: Subagent Detection

**Location:** `src/ccproxy/app/router/subagent.py` → `SubagentRouter.determine_route()`

Claude Code spawns specialist subagents (defined in `~/.claude/agents/*.md`) that have distinct system prompts. The proxy detects which subagent is active and can route it to a different model/provider.

### Detection Algorithm

1. Check if the request has exactly 2 system messages
2. First system message must contain `"You are Claude Code"`
3. Extract the "static prompt" from the second system message (everything before `\nNotes:`)
4. Compute `SHA256(static_prompt)[:16]` (first 16 hex chars)
5. Compare against precomputed hashes from `~/.claude/agents/*.md` files
6. If match found → apply the subagent's configured model/provider override

### Hash Computation

```python
def hash_static_prompt(prompt: str) -> str:
    """SHA256[:16] of the static portion of a prompt."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]
```

The 16-char prefix (64 bits) provides sufficient collision resistance for the small agent namespace (~10-50 agents per user).

### Why SHA256 Hashing?

The proxy can't read the agent file's `name` field from the request (Claude Code doesn't send it). Instead, it identifies agents by their system prompt content — the only stable, observable signal. Hashing avoids storing full prompts and makes comparison O(1).

## Stage 3: Endpoint Mapping

**Location:** `src/ccproxy/app/router/subagent.py` → `SubagentRouter.determine_route()`

After alias rewrite and subagent detection, the router looks up the resolved model in the `endpoints` configuration:

```yaml
model_endpoints:
  endpoints:
    claude-opus-4-8: anthropic
    claude-sonnet-4-6: anthropic
    gpt-4o: openai
```

Resolution order:
1. Exact match in `endpoints[model]` → use that provider
2. `default_endpoint` provider (if configured)
3. Fall through to pattern matching

## Stage 4: Provider Pattern Fallback

**Location:** `src/ccproxy/app/router/subagent.py` → `provider_for_model()`

If no explicit endpoint mapping exists, the router uses prefix-based pattern matching:

| Prefix | Provider |
|--------|----------|
| `gpt-` | openai |
| `o1` | openai |
| `o3` | openai |
| `claude-` | anthropic |
| *(default)* | anthropic |

First match wins. Unknown models default to Anthropic (matching the upstream Go router's behavior).

## Stage 5: Protocol Conversion

**Location:** `src/ccproxy/app/providers/openai.py`

When the target provider is OpenAI, the proxy performs full bidirectional protocol conversion:

### Request: Anthropic → OpenAI

| Anthropic Shape | OpenAI Shape |
|-----------------|--------------|
| `system: [{type: "text", text: "..."}]` | Single `{role: "system", content: "..."}` message |
| `messages: [{role, content: [...blocks...]}]` | `messages: [{role, content: "text"}]` |
| `tools: [{name, input_schema}]` | `tools: [{type: "function", function: {name, parameters}}]` |
| `tool_choice: "any"` | `tool_choice: "required"` |
| `tool_result` content blocks | Flattened to `"Tool result for <id>:\n<content>"` text |
| `max_tokens: N` | `max_tokens: N` (or `max_completion_tokens` for o-series) |

### O-Series Quirks (o1, o3)

- `max_completion_tokens` capped at 16384 (API limit)
- `temperature` parameter rejected (o-series doesn't support it)
- Identified by `_O_SERIES_PREFIXES = ("o1", "o3")`

### Response: OpenAI → Anthropic

Non-streaming responses are transformed back into Anthropic format:
- `choices[0].message.content` → `content: [{type: "text", text: "..."}]`
- `choices[0].message.tool_calls` → `content: [{type: "tool_use", ...}]`
- `usage.prompt_tokens` → `usage.input_tokens`
- `usage.completion_tokens` → `usage.output_tokens`

### Streaming: OpenAI SSE → Anthropic SSE

The `_AnthropicStreamState` class maintains state across SSE chunks:

```
OpenAI chunk: {"choices": [{"delta": {"content": "Hello"}}]}
    ↓ transforms to ↓
Anthropic SSE: event: content_block_delta
               data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}
```

The state machine emits the full Anthropic event sequence:
1. `message_start` (with model, usage)
2. `content_block_start` (per text block or tool call)
3. `content_block_delta` (incremental content)
4. `content_block_stop`
5. `message_delta` (final usage update)
6. `message_stop`

## URL Resolution

**Location:** `src/ccproxy/app/api/proxy.py` → `_resolve_request_target()`

Once the provider is determined, the proxy resolves the actual upstream URL:

```
1. endpoints[model] → provider_name
2. providers[provider_name] → (url, api_key)
3. Append /v1/messages if not already present
4. Fallback chain: default_provider → default_endpoint (legacy)
```

Each provider carries its own API key (resolved from `CredentialStore` via `key_ref`). The proxy substitutes this key in the forwarded request's headers.

## Route Decision Output

The router produces a `RouteDecision` object:

```python
class RouteDecision(BaseModel):
    provider: Provider          # The instantiated provider object
    original_model: str         # Model name before any rewriting
    target_model: str           # Model name after alias/subagent rewrite
    path: str                   # "passthrough" | "converted" | "subagent"
    target_provider_name: str   # Provider name for logging/capture
```

The `path` field indicates what happened:
- **passthrough**: Anthropic → Anthropic (no conversion needed)
- **converted**: Anthropic → OpenAI (full protocol translation)
- **subagent**: Routed based on detected subagent identity

## Configuration Sources

Routing configuration comes from multiple sources (highest precedence first):

1. **Profile overrides** (from `ProfileEditorDialog`) — model_endpoints, providers, aliases
2. **`config.yaml`** — YAML settings source for the proxy
3. **Environment variables** — `MODEL_ENDPOINTS`, `SUBAGENT_MAPPINGS`, etc.
4. **`.env` file** — dotenv fallback
5. **Field defaults** — hardcoded Anthropic-only fallback
