# Monitoring

Day-to-day commands for watching the EVO-X2 stack. Run the first block from
anywhere over SSH; tmux/log commands run on the machine that owns the process
(servers → EVO-X2, router → laptop).

## Live memory monitoring

The command you'll have open in a spare terminal all day:

```bash
watch -n 2 'free -h && rocm-smi --showmeminfo vram'
```

Refreshes every 2 s, both numbers that matter:

- **`free -h`** — system RAM (~30 GiB total after carve-out). Watch for
  `available` approaching zero during model loads; sustained near-zero
  available + swap climbing = you're about to OOM or thrash.
- **`rocm-smi --showmeminfo vram`** — VRAM. Expected steady states (of
  103,079,215,104 B ≈ 96 GiB total):
  - idle with 9B+Hermes+27B loaded: ~70 GiB used (74,975,137,792 B on the
    live box)
  - Flash-Next loaded as strategist: significantly higher; if it climbs to
    ~96 GiB and the server dies, see [swap-setup.md](swap-setup.md) and the
    `-ctk/-ctv` notes in [launch-flags.md](launch-flags.md).

One-shot VRAM snapshot:

```bash
rocm-smi --showmeminfo vram
# GPU[0] : VRAM Total Memory (B): 103079215104
# GPU[0] : VRAM Total Used Memory (B): 74975137792
```

Bytes, not MiB — divide by 2^30 for GiB. Also useful: `rocm-smi` bare
(g_utilization %, VRAM %, clocks, power), `watch -n 1 rocm-smi` during
prefill to confirm the GPU is actually working.

## llama-server processes

```bash
ps aux | grep llama-server
```

What to check in the output:

- **One process per serving port** (8080/8081/8082) — duplicates mean an old
  server never died and is eating VRAM.
- **Which binary**: `~/llama.cpp/build/bin/llama-server` vs
  `~/llama-engramhalo/build/bin/llama-server` — Flash-Next must be the
  engramhalo binary.
- **RSS** (RES column): Flash-Next idles around ~27 GB resident; the 27B
  tiers in the tens of GB. A process at 85–90% of RAM is *loading*, which is
  normal during startup but should settle.

## tmux sessions (the servers' home)

Every server runs in a detached tmux session — that's its console:

```bash
tmux ls
# flash-next   1 windows (created Thu Aug 28 ...)
# hermes-35b   1 windows ...
# qwen35-9b    1 windows ...

tmux attach -t flash-next     # watch live logs; detach: Ctrl-b d
tmux kill-session -t flash-next                # stop a server
```

Session names: `qwen35-9b`, `hermes-35b`, `qwen36-27b`, `fable-27b`,
`flash-next` (port 8082 ones are mutually exclusive), plus `ai-router` on the
laptop.

## Router log

```bash
tail -f ~/ai-tools/router/responses.log      # on the laptop
```

First ~4 KB of every response body, tagged `[N] HH:MM:SS model=… path=…`.
Use it to see what a model *actually* answered when a client misbehaves
(malformed tool calls, refusals, truncation). The router's own stderr (in the
`ai-router` tmux session) has the request/route lines:

```
13:24:44 INFO router [7] POST /v1/messages  model=strategist  ->  http://evo-x2:8082  (48213 bytes)
13:24:51 INFO router [7] /v1/messages done  status=200  streamed=18234 bytes  elapsed=6841ms
```

## Reading server logs

Attach to a server's tmux session and watch the periodic `slot print_timing`
lines (emitted every few seconds while generating):

```
slot print_timing: id  0 | task 1234 | generated 1247 tokens, total = 31.28 s, tg = 39.87 / s, tg_3s = 41.02 / s
```

| Token | Meaning |
|---|---|
| `tg = 41.23 / s` | **T**oken**g**eneration speed: average decode tokens/sec for this slot since generation began. |
| `tg_3s` | Same rate measured over just the **last 3 seconds** — the live number. Compare `tg` vs `tg_3s`: a falling `tg_3s` means the GPU is throttling, another process is competing for VRAM, or KV-cache growth is biting. |
| `pp` / `prompt eval` lines | Prefill (prompt processing) speed — expect much higher numbers than decode (thousands of t/s on 9B, hundreds on 27B). |

### Draft acceptance (speculative decoding)

With `--spec-type draft-mtp,ngram-mod` you'll see lines like:

```
diff draft acceptance: 874/1180 = 74.9%, mean 0.75, ...
```

Fraction of drafted (MTP/n-gram) tokens the target model accepted. On
Flash-Next expect roughly **70–85%** with `--spec-draft-p-min 0.75`. If it
drops toward 0, speculation is pure overhead — either lower `p_min`, reduce
`n_max`, or drop `draft-mtp` from the spec type for that workload.

### `graphs reused`

```
graph_reuse: reused 1 graph (no rebuild)     # ~"graphs reused" lines
```

With `GGML_HIP_GRAPHS=ON`, the ~5k-node decode kernel graph is **captured
once and replayed** every token instead of being rebuilt. You want to see
reuse every step; repeated graph *rebuilds* per token (e.g. every decode line
mentions a rebuild) mean something is changing the shape each step (batch
size jitter, KV defrag) and cost 10–15% decode speed. The EngramHalo fork's
"reuse decode graphs" patch is exactly this behavior being fixed for qwen4exp.

## Quick health sweep

```bash
ssh codemonkey@EVO-X2 '
  echo "== ports:";  ss -tln | grep -E "808[0-2]"
  echo "== free:";  free -h | head -2
  echo "== vram:";  rocm-smi --showmeminfo vram | grep Used
  echo "== procs:"; pgrep -af llama-server | cut -c1-120'

curl -s http://localhost:8090/health    # router (laptop) → ok
```
