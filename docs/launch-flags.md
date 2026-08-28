# llama-server Launch Flags

Every flag used by the scripts in [scripts/](scripts/), what it actually
does, and why this stack sets it the way it does. Reference command (live
Flash-Next process on the box):

```bash
~/llama-engramhalo/build/bin/llama-server \
  -m ~/models/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf \
  -md ~/models/qwen38-flash-next/mtp-Qwen3.8-Flash-Next-Q8_0.gguf \
  -ngl 999 -fa on -ctk q8_0 -ctv q8_0 \
  -c 131072 -ub 2048 -t 4 \
  --parallel 1 \
  --jinja --no-ui \
  --chat-template-file ~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja \
  --spec-type draft-mtp,ngram-mod --spec-draft-n-max 4 --spec-draft-p-min 0.75 \
  --host 0.0.0.0 --port 8082
```

## Environment variables

These are set in the shell *before* `llama-server` starts (all scripts export
them):

| Var | What it does | Why |
|---|---|---|
| `HSA_ENABLE_SDMA=0` | Tells the ROCm runtime (HSA runtime → KFD) not to use **SDMA** (System DMA) copy engines for host↔device transfers; everything goes through GPU-side copy kernels instead. | The SDMA engines on Strix Halo have caused transfer stalls/hangs with the huge scattered copies llama.cpp does (KV defrag, mmap'd tensor paging). Kernel copies are marginally slower per byte but don't hang. This is the classic "llama.cpp hangs mid-request on Strix Halo" fix. |
| `HSA_XNACK=1` | Enables **XNACK** (paged/unified memory) semantics for HIP kernels — page-fault-based page migration instead of explicit copies. | Strix Halo is a true unified-memory part; ROCm 7.x ships xnack-enabled kernels, and ROCm 7 on this platform expects `HSA_XNACK=1` so kernel code objects match the runtime memory model. Without it you get "no kernel image available" style failures or explicit-copy errors on unified pages. |
| `ROCBLAS_USE_HIPBLASLT=1` | Makes rocBLAS calls route to **hipBLASLt** (the Tensile-tuned Lt API) instead of the legacy GEMM path. | hipBLASLt has tuned tiles for gfx1151 and is measurably faster on the quantized/dequantized GEMM shapes llama.cpp hits. Used especially for the Flash-Next/EngramHalo launch. |

## Model / offload

| Flag | Meaning | Notes |
|---|---|---|
| `-ngl 999` | Offload **all** transformer layers to the GPU ("999" = "as many as exist", a sentinel — same effect as `-ngl 99` on models with fewer layers). | With a 96 GiB carve-out everything *should* fit; if a load log shows "offloaded 63/64 layers", something else owns the VRAM — see [monitoring.md](monitoring.md). All scripts in [scripts/](scripts/) use `-ngl 999`; `-ngl 99` on the live box means exactly the same thing for these layer counts. |
| `-fa on` / `--flash-attn on` | Flash attention fused attention kernels (identical flags: `-fa` is the short form of `--flash-attn`). | Non-negotiable at long context: keeps attention working on tile memory instead of materializing the full N×N score matrix. Required for the QSA path on Flash-Next and for the q8_0 KV cache below to actually be used efficiently. |
| `-ctk q8_0 -ctv q8_0` | Quantize the K-cache and V-cache to **q8_0** (8-bit blocks). | See "why not bf16" below. |
| `-c <N>` | Context (slot) size in tokens. | Memory math: KV bytes ≈ `2 × layers × kv_heads × head_dim × N × bytes_per_token` per slot. At 27B-class geometry, a 131072-token q8_0 slot costs tens of GB — that's what the 96 GiB carve-out is paying for. Bigger `-c` than you need costs VRAM at every request even when idle; the three-tier scripts run 262144 because orchestrator/strategist sessions legitimately hit 100K+; the 9B worker gets the same for headroom. On Flash-Next, 131072 is the sweet spot — MTP was validated by the fork authors up to a 164K slot, and 256K slot + MTP is unverified. |
| `-ub 2048` | Micro-batch size: how many tokens of the prompt are processed per GEMM during prefill (`-b`/batch is chunked into `-ub` micro-batches). | Prefill throughput is GEMM-bound; 2048 keeps the matmuls large enough to saturate the 8060S on this box (default 512 leaves performance on the table). Tradeoff: more prefill memory headroom needed alongside the KV cache. |
| `-t 4` | CPU thread count for the (small) CPU-side work. | On a 16-core box with 96 GiB carved out, 16 threads oversubscribe and steal cycles from the GPU feed; 4 is enough for sampling/tokenization. The three-tier scripts use `--threads 16` since those models leave more CPU idle. |

## Parallelism and slots

| Flag | Meaning | Why |
|---|---|---|
| `--parallel 1` | Single request slot (no multi-slot serving) — required on Flash-Next. | The QSA **gather** path (true sparse-KV gather, see [llama-cpp-build.md](llama-cpp-build.md)) currently processes multi-sequence ubatches through the gather kernel too, and that combination is **not validated on HIP** — it has produced wrong-output/crash behavior. Until the upstream multi-sequence gate lands, Flash-Next runs `--parallel 1`. (The non-QSA models — 9B, Hermes, 27B — run `--parallel 2` safely; if you must multi-slot Flash-Next, set `LLAMA_QSA_GATHER=0` to force the old dense-mask path.) |

## Speculative decoding

| Flag | Meaning | Notes |
|---|---|---|
| `--spec-type draft-mtp,ngram-mod` | Comma-separated list of draft mechanisms. `draft-mtp`: use the model's **M**ulti-**T**oken **P**rediction head (loaded via `-md`, or built into the checkpoint) as the speculative drafter. `ngram-mod`: **n-gram** drafting in *modulated* mode — draft from repeated n-grams found in the context, with acceptance tuned so it kicks in where repetition exists. | Chaining them means: try the MTP head first; where MTP peters out (low confidence), the n-gram drafter still catches verbatim repetitions (code boilerplate, tool-output echoes) for free. On the 27B stock build: `draft-mtp,ngram-mod` with `--spec-ngram-mod-n-min 24` (only propose n-grams of length ≥ 24). |
| `--spec-draft-n-max 4` | Draft at most **4** speculative tokens per step. | More draft tokens = more accepted per step *if* the drafter is confident, but each extra token that fails verification wastes a full forward pass. 4 is the tuned sweet spot for the Flash-Next MTP head; the 27B/Hermes tiers use `1` (their MTP heads accept ~1 token per step reliably; deeper drafts mostly wasted). |
| `--spec-draft-p-min 0.75` | Accept a drafted token only if the target model gives it ≥ **0.75** probability. | Higher = fewer wasted verification passes but fewer accepted tokens; 0.75 measured best on this model/box (the fork's benchmark matrix has the sweep). |

## Chat template

| Flag | Meaning | Notes |
|---|---|---|
| `--jinja` | Render chat with the chat template embedded in the GGUF (`tokenizer.chat_template`), instead of llama.cpp's built-in fallback template. | Always on. Without it, tool calling is broken (the fallback template doesn't emit tool-call grammar the way these Qwen checkpoints expect). |
| `--chat-template-file <path>` | Override the GGUF-embedded template with a file (implies Jinja rendering). | Points at `~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja` for all Qwen 27B-class launches. Needed because Claude Code sends **system messages mid-conversation**; the stock template mishandles them ([models.md](models.md#chat-template-fix)). Hermes uses its own bundled template the same way. |

## Memory-mapping / SSD modes

| Flag | Meaning | Verdict on ROCm |
|---|---|---|
| `--tensor-read-lazy on` | SSD mode: don't fault whole tensors into RAM at load; read tensor data lazily from SSD on first use (pairs with `-lm mmap`). The EngramHalo fork's way of keeping the ~27 GB engram table SSD-resident (~1.2 GiB resident instead of 26.8 GB). | **On stock llama.cpp on ROCm this hangs — avoid.** The lazy-read path deadlocks with the HIP backend's async copies (that's why it's flagged here rather than in the launch scripts). The EngramHalo fork carries the fixes that make the lazy path work there; do not copy the flag onto the stock-llama.cpp servers. |
| `-lm none` (RAM mode) | Force all tensors resident, no mmap. | Flash-Next short-context only: with slots ≳ 48K the first request **deadlocks** reproducibly (documented in the fork's repro notes). Long-context Flash-Next uses `-lm mmap --tensor-read-lazy on -c 262144` *on the fork*. |
| `--no-mmap` | Disable mmap entirely. | **Never with lazy-read** — it silently disables the lazy-read path (documented gotcha). |

## Misc

| Flag | Meaning |
|---|---|
| `--reasoning off` | Don't parse/emit a separate reasoning (thinking) stream — these finetunes either don't use think tokens or we want them inline. |
| `-fit off` / `-fit=false` | Disable the automatic "fit params to VRAM" auto-shrink. We size `-c` deliberately; auto-fit would silently shrink context instead of failing loudly when VRAM is short. |
| `--no-ui` / `--no-webui` | Don't serve the built-in web UI (bandwidth + attack surface). |
| `--host 0.0.0.0 --port <p>` | Bind all interfaces. Safe *only* because the box is reachable only via the Tailscale mesh ([README.md](README.md#ssh-access)). |
| `-md <file.gguf>` | Load an MTP draft model sidecar (used by Flash-Next; the 27B keeps MTP inside the main file). |

## Reading a launch

You should see, in the server log (attach via tmux, see
[monitoring.md](monitoring.md)):

```
register model   ← model loaded
... offloaded 64/64 layers to GPU
speculative decoding: type=draft-mtp,ngram-mod, draft_n_max=4, p_min=0.75
server listening on http://0.0.0.0:8082
```

If instead you see `alloc_tensor_range: failed to allocate` or offload
stopping mid-model, VRAM is already occupied (a previous server didn't shut
down — `ps aux | grep llama-server`, [monitoring.md](monitoring.md)).
