# Client Setup (Laptop)

Everything here lives on the laptop (not the EVO-X2): the `.bashrc` functions
that launch Claude Code through the router, and the Qwen Code CLI config.
The router itself is documented in [router-setup.md](router-setup.md).

## Claude Code via `.bashrc` functions

From `~/.bashrc` (verbatim — see the file for the third variant,
`orchesterator()`):

```bash
_ensure_ai_router() {
  tmux has-session -t ai-router 2>/dev/null ||
    tmux new-session -d -s ai-router \
      "python3 ~/projects/ai-tools/router/router.py -v"
}

strategist() {
  _ensure_ai_router
  ANTHROPIC_BASE_URL=http://localhost:8090 \
    ANTHROPIC_AUTH_TOKEN=local \
    ANTHROPIC_MODEL=strategist \
    ANTHROPIC_SMALL_FAST_MODEL=worker \
    ANTHROPIC_DEFAULT_OPUS_MODEL=strategist \
    ANTHROPIC_DEFAULT_SONNET_MODEL=worker \
    ANTHROPIC_DEFAULT_HAIKU_MODEL=worker \
    CLAUDE_CODE_SKIP_PROMPT_HISTORY=1 \
    CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
    claude "$@"
}

worker() {
  _ensure_ai_router
  ANTHROPIC_BASE_URL=http://localhost:8090 \
    ANTHROPIC_AUTH_TOKEN=local \
    ANTHROPIC_MODEL=worker \
    ... same shape, all tiers -> worker ...
    claude "$@"
}
```

`_ensure_ai_router` starts the router in a detached tmux session `ai-router`
if it isn't already running, so `strategist` just works from a cold boot.

### The environment variables

| Var | Value | Effect |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `http://localhost:8090` | Claude Code talks to **the router**, not Anthropic. The router forwards `/v1/messages` to the EVO-X2 over the mesh. |
| `ANTHROPIC_AUTH_TOKEN` | `local` | Any non-empty string — the router ignores auth; the variable only needs to exist so Claude Code doesn't prompt for an API key. |
| `ANTHROPIC_MODEL` | `strategist` / `orchestrator` / `worker` | The main model name for the session — becomes the `model` field the router routes on. |
| `ANTHROPIC_SMALL_FAST_MODEL` | `worker` | Claude Code's background/small model (title generation, topic detection, haiku-tier work) always goes to the fast 9B. |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `strategist` | When Claude Code internally requests "opus", it gets the **strategist** backend (27B/Flash-Next). |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `worker` | "sonnet" → the 9B worker. |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `worker` | "haiku" → worker too, in all three functions. |
| `CLAUDE_CODE_SKIP_PROMPT_HISTORY` | `1` | Don't read/write `~/.claude` prompt history — keeps sessions isolated and the history files from bloating on a local box. |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `1` | Disables Claude Code's auto-memory features (matches the "no memory files" policy in `~/CLAUDE.md`). |

Net effect: three launch tiers —

| Command | Main model | Opus/Sonnet/Haiku map to |
|---|---|---|
| `strategist` | 27B-class on :8082 | strategist / worker / worker |
| `orchesterator` | orchestrator (Hermes :8081) | orchestrator / worker / worker |
| `worker` | 9B on :8080 | all worker |

### Starting / stopping the router

```bash
strategist                 # auto-starts router in tmux session "ai-router"
tmux attach -t ai-router   # watch router logs   (detach: Ctrl-b d)
stop-ai-router             # bash function: tmux kill-session -t ai-router
```

`stop-ai-router` is just:

```bash
stop-ai-router() { tmux kill-session -t ai-router 2>/dev/null && echo "Router stopped"; }
```

## Qwen Code CLI (`~/.qwen/settings.json`)

Qwen Code speaks OpenAI-format to the router. Full working config:

```json
{
  "env": {
    "QWEN_CUSTOM_API_KEY_OPENAI_HTTP_LOCALHOST_8090_V1_0F7FAF7E26DA": "local"
  },
  "modelProviders": {
    "openai": [
      { "id": "orchestrator", "name": "orchestrator",
        "baseUrl": "http://localhost:8090/v1",
        "envKey": "QWEN_CUSTOM_API_KEY_OPENAI_HTTP_LOCALHOST_8090_V1_0F7FAF7E26DA" },
      { "id": "worker", "name": "worker",
        "baseUrl": "http://localhost:8090/v1",
        "envKey": "QWEN_CUSTOM_API_KEY_OPENAI_HTTP_LOCALHOST_8090_V1_0F7FAF7E26DA" },
      { "id": "strategist", "name": "strategist",
        "baseUrl": "http://localhost:8090/v1",
        "envKey": "QWEN_CUSTOM_API_KEY_OPENAI_HTTP_LOCALHOST_8090_V1_0F7FAF7E26DA" }
    ]
  },
  "security": { "auth": { "selectedType": "openai" } },
  "model": { "name": "strategist", "baseUrl": "http://localhost:8090/v1" },
  "experimental": { "agentTeam": true },
  "tools": {
    "workflowsEnabled": true,
    "visible": ["agent", "list_agents", "send_message"]
  }
}
```

Point-by-point:

- **`OPENAI_BASE_URL` → router.** The `baseUrl` on every provider (and
  `model.baseUrl`) is `http://localhost:8090/v1` — the router's OpenAI-format
  endpoint. Qwen Code itself never sees llama.cpp; it thinks it's talking to
  an OpenAI-compatible API with three model names. (Equivalently: export
  `OPENAI_BASE_URL=http://localhost:8090/v1` when running qwen ad-hoc.)
- **`modelProviders.openai`: orchestrator / worker / strategist.** One entry
  per router model name; all three hit the same base URL because the router
  routes by the `model` field. `/model` in Qwen Code switches between them.
- **`envKey` + `env`.** Qwen Code wants an API key per provider; the `env`
  block defines the key `QWEN_CUSTOM_API_KEY_...` with value `local` — a
  placeholder, same role as `ANTHROPIC_AUTH_TOKEN=local` (the router ignores
  it; it exists to satisfy Qwen Code's config validation).
- **`model.name: "strategist"`.** Default agent model; the orchestrator/worker
  roles are used by the agent-team machinery below.
- **`experimental.agentTeam: true`.** Enables Qwen Code's agent-team mode —
  the main model (strategist) can spawn a team of sub-agents. With one 9B
  slot on the router this is the point of the whole three-tier design: the
  orchestrator/strategist decomposes work, teammates execute.
- **`tools.visible: ["agent", "list_agents", "send_message"]`.** Restricts the
  model-visible tool surface to exactly the **agent-team tools**: spawn agents
  (`agent`), discover running ones (`list_agents`), message running agents
  (`send_message`). Why only these: Qwen Code's full tool set (shell, edit,
  web…) would be executed by the *worker-class model on the other end of the
  network*, and letting a 9B/35B freely call file/shell tools unsupervised is
  neither wanted nor safe; the team tools are the coordination primitives the
  team actually needs.
- **`tools.workflowsEnabled: true`.** Enables Qwen Code's workflow feature
  (multi-step scripted runs), which drives the same routed models.
- **`permissions.allow: ["Bash(xargs *)"]`.** Small allow-list so common
  pipelines don't prompt; extend to taste.

### The fixed Jinja template (client-side relevance)

Qwen Code talks to llama-server's OpenAI endpoint, and **the server applies
its `--chat-template-file`** to every request
(`~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja` for the
27B-class models — see [models.md](models.md#chat-template-fix)). This
matters to the client because Qwen Code, like Claude Code, injects
mid-conversation system messages (tool-use reminders, team-status notices);
if the server ran the stock Qwen template those turns would be mangled and
the model would derail mid-task. So: nothing to configure in
`settings.json` for this — but **don't remove `--chat-template-file` from the
server scripts** ([scripts/](scripts/)) when reproducing.

## Test matrix

```bash
curl -s http://localhost:8090/health                                   # ok
curl -s http://localhost:8090/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"worker","messages":[{"role":"user","content":"say OK"}],"max_tokens":8}'
strategist -p "say hello"          # Claude Code → :8082 (via router)
worker     -p "say hello"          # Claude Code → :8080
qwen -p "say hello"                # Qwen Code → model from settings.json
```
