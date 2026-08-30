# Worker model (`worker`)

The Ollama model that Claude Code sub-agents run on: **g14-new-windows-pat**
(RTX 4070 Laptop, 8 GB), base model `qwen3.5:4b`, reached through the router as
the model name `worker`.

## Why this directory exists

`worker` used to be a bare alias — `ollama cp qwen3.5:4b worker` — which copies
the base model with **no `num_ctx`**. Ollama then serves it at its **4096-token
default**, even though `qwen3.5:4b` reports a 262144 native window.

That is not a graceful failure. A prompt larger than the window is truncated to
fit, and the model spends whatever is left of the window producing nothing:

```
$ curl -s http://localhost:8090/v1/chat/completions -d '{"model":"worker", ...}'   # 21.5 KB prompt
reply:                                                          <- empty
usage: {'prompt_tokens': 2050, 'completion_tokens': 2046, 'total_tokens': 4096}
```

`2050 + 2046 = 4096` exactly: the prompt was cut down, and no error was raised
anywhere in the chain. A Claude Code sub-agent on this worker sees a blank turn.

Two things made this hard to notice:

- **The evaluation harness hid it.** `/tmp/worker-eval/harness.py` sent
  `"num_ctx": 8192` in its options, so the reliability verdict was measured at a
  context size production never actually served.
- **The number in CLAUDE.md was true but misleading.** 262k is the model's
  native context length, not the window it was being served.

### Why the fix has to live in the model

The router proxies `/v1/chat/completions` verbatim and never rewrites `model`,
so it cannot inject a context size. Ollama's OpenAI-compatible endpoint does not
accept `num_ctx` per request at all. The only two levers are this Modelfile's
`PARAMETER num_ctx` or the server-wide `OLLAMA_CONTEXT_LENGTH` environment
variable on the `Ollama-Serve` task. This file is the per-model, reviewable
option.

### What the MCP tool schemas cost

Every tool definition is resent on every request, against the same window:

| Tool (websearch MCP) | Schema tokens |
|---|---|
| `web_search` | ~373 |
| `fetch_url` | ~226 |
| `check_health` | ~90 |
| **websearch total** | **~689** |

That is ~17% of the old 4096 window before ntfy, design-viewer, the built-in
tools, or the Claude Code system prompt. `websearch/main.py` caps its *output*
for this reason, but nothing a server does can shrink its own tool definitions —
which is why the fix belongs on the serving side.

## Memory budget

Measured on the 8 GB card (RTX 4070 Laptop, 8188 MiB), linear in context size:

| `num_ctx` | GPU used |
|---|---|
| 8192 | 4791 MiB |
| 16384 | 5063 MiB |
| **32768** | **5607 MiB** |

Roughly **33 MiB per 1k tokens** over ~3.1 GB resident. 32768 leaves ~2.5 GB for
the Windows desktop and a second request. 65536 projects to ~6.7 GB, which fits
on paper but invites Ollama to offload layers to CPU and get slow instead of
failing loudly.

Those figures are for **one slot**. Ollama allocates a KV cache *per parallel
slot*, and `OLLAMA_NUM_PARALLEL` is currently unset on the host, so it
auto-selects 1 or 4 by available memory — a second concurrent request may
therefore cost more than the table suggests. It cannot be pinned in the
Modelfile: Ollama rejects `PARAMETER num_parallel` with
`unknown parameter 'num_parallel'`. If concurrent requests start OOM-ing or
going CPU-bound, set `OLLAMA_NUM_PARALLEL=1` on the `Ollama-Serve` task.

## Deploy

Copy the Modelfile to the worker host and rebuild the `worker` tag. Both steps
run on g14-new-windows-pat:

```
scp -P 22 worker/Modelfile padr3@g14-new-windows-pat:Modelfile
ssh -p 22 padr3@g14-new-windows-pat 'ollama create worker -f Modelfile'
```

No service restart is needed for `ollama create` itself — but a model already
loaded in memory keeps its old window, so unload it:

```
ssh -p 22 padr3@g14-new-windows-pat 'ollama stop worker'
```

If the scheduled task has to be restarted (only for config that is not
model-scoped): `Start-ScheduledTask -TaskName Ollama-Serve`. Do **not** run
`winget upgrade Ollama.Ollama` to get out of a jam — it stops the server and
takes the worker with it.

## Verify

`ollama ps` must report the new context, not 4096:

```
ssh -p 22 padr3@g14-new-windows-pat 'ollama ps'
```

Then prove a real-size prompt survives the round trip through the router — the
answer sits at the *front* of an oversized prompt, so truncation shows up as an
empty reply:

```
python3 - <<'PY'
import json, urllib.request

sentence = "The quick brown fox jumps over the lazy dog while the engineer reviews the pull request. "
prompt = ("The secret passphrase is BANANA-ORCHID-42.\n"
          + sentence * 60
          + "\nQuestion: what is the secret passphrase? Answer with just the phrase.")
req = urllib.request.Request("http://localhost:8090/v1/chat/completions",
    data=json.dumps({"model": "worker", "messages": [{"role": "user", "content": prompt}],
                     "stream": False}).encode(),
    headers={"Content-Type": "application/json"})
r = json.load(urllib.request.urlopen(req, timeout=180))
print("reply:", r["choices"][0]["message"]["content"][:120])
print("usage:", r["usage"])
PY
```

Expected: the reply contains `BANANA-ORCHID-42`, and `total_tokens` exceeds 4096.

## Rollback

```
ssh -p 22 padr3@g14-new-windows-pat 'ollama rm worker && ollama cp qwen3.5:4b worker'
```

That restores the previous bare alias — 4096 tokens and the empty-reply bug
included. There is no reason to want it.
