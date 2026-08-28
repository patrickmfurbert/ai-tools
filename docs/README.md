# EVO-X2 Local LLM Stack — Setup Guide

Complete, reproducible documentation for the local LLM stack this repo runs
against: a GMKtec EVO-X2 mini-PC running a full ROCm 7.2 stack and llama.cpp,
serving multiple Qwen models behind a tiny name-based HTTP router.

```
 laptop (Claude Code / Qwen Code)                 EVO-X2 (Ryzen AI MAX+ 395)
 ┌───────────────────────────┐     Tailscale    ┌───────────────────────────────────┐
 │ strategist() worker()     │     mesh         │ :8080  llama-server Qwen3.5-9B    │
 │        │                  │                  │          (worker)                 │
 │        ▼                  │                  │ :8081  llama-server Hermes-35B    │
 │ ai-tools router :8090     ├─────────────────►│          (orchestrator)           │
 │                           │                  │ :8082  llama-server 27B /         │
 └───────────────────────────┘                  │          Flash-Next (strategist)  │
                                               └───────────────────────────────────┘
```

## Hardware

| | |
|---|---|
| Machine | GMKtec **EVO-X2** |
| SoC | AMD **Ryzen AI MAX+ 395** ("Strix Halo") |
| GPU | Radeon 8060S iGPU — **gfx1151** (RDNA 3.5, Strix Halo) |
| Memory | **128 GB unified** (LPDDR5x, shared CPU/GPU) |
| VRAM carve-out | **96 GiB** in BIOS (`rocm-smi` reports 103,079,215,104 B ≈ 96 GiB) |
| OS-visible RAM | ~30 GiB after carve-out |
| OS | Ubuntu 24.04.4 LTS, kernel 7.0.0-30-generic |
| ROCm | **7.2.4** (`rocm-core 7.2.4.70204`) |

The big idea: a 96 GiB VRAM carve-out plus q8_0 KV cache lets a 27 GB-class
model run fully GPU-resident with a 131K+ context window, and lets the ~88 GB
Flash-Next model keep its engram table on SSD while everything else stays on
the GPU.

## Quick start

All model serving happens on the EVO-X2; the laptop runs only clients plus the
router.

```bash
# 1. From the laptop, confirm you can reach the box (Tailscale mesh must be up)
ssh codemonkey@EVO-X2 'rocminfo | grep gfx1151'

# 2. On the EVO-X2: build llama.cpp + EngramHalo.cpp   → docs/llama-cpp-build.md
# 3. Pull models                                      → docs/models.md
# 4. Start the servers (tmux sessions)                → docs/scripts/
ssh codemonkey@EVO-X2 '~/scripts/start-llm-servers.sh'

# 5. On the laptop: start the router, then launch Claude Code through it
strategist      # bash function → Claude Code against the 27B on :8082 (via router)
worker          # bash function → Claude Code against the 9B on :8080 (via router)
```

Detailed setup order:

1. [hardware.md](hardware.md) — BIOS carve-out, kernel args, EVO-X2 notes
2. [rocm-setup.md](rocm-setup.md) — ROCm 7.2.4 install, `/dev/kfd`, gfx1151 verification
3. [swap-setup.md](swap-setup.md) — 64 GB swapfile (required for Flash-Next long context)
4. [llama-cpp-build.md](llama-cpp-build.md) — cmake flags, EngramHalo.cpp fork
5. [models.md](models.md) — exact `hf download` commands for every model
6. [launch-flags.md](launch-flags.md) — what every `llama-server` flag does and why
7. [router-setup.md](router-setup.md) — router config, GBNF bug workarounds
8. [client-setup.md](client-setup.md) — laptop `strategist()`/`worker()` functions, Qwen Code
9. [monitoring.md](monitoring.md) — VRAM, tmux, logs, reading server timings

## SSH access

The EVO-X2 sits on the private **Tailscale/Headscale mesh** (tailnet
100.64.0.0/10); `evo-x2` is `100.64.0.6`. No ports are exposed on the LAN.

```bash
ssh codemonkey@EVO-X2            # works from any tailnet member (MagicDNS / hosts entry)
ssh codemonkey@100.64.0.6        # fallback if name resolution fails
```

If the connection fails, check the mesh first — `tailscale status` on any
member should show `evo-x2` without `offline`. The llama-server ports
(8080–8082) bind to `0.0.0.0` but are only reachable through the mesh, which
is why they need no auth token.

## What runs where

| Port | Model | Role | Served by |
|---|---|---|---|
| 8080 | Qwen3.5-9B UD-Q4_K_XL + MTP | worker (fast execution) | `~/llama.cpp` |
| 8081 | Hermes 3.6-35B-A3B + MTP | orchestrator (main Qwen Code model) | `~/llama.cpp` |
| 8082 | Qwen3.8-27B UD-Q4_K_XL + MTP **or** Fable-27B **or** Flash-Next | strategist (deep planning) | `~/llama.cpp` / `~/llama-engramhalo` |

Only one model occupies port 8082 at a time — the scripts in
[scripts/](scripts/) are mutually exclusive choices for the strategist slot.
