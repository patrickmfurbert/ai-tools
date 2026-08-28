# Models

Everything under `~/models/` on the EVO-X2, with the exact `hf download`
commands used to fetch them (recovered from the box's shell history — these
are the commands that actually ran).

`hf` is the Hugging Face Hub CLI: `pip install hf` / `uvx --from
huggingface-hub hf`. Some repos are gated/restricted and need a read token:

```bash
export HF_TOKEN=$(cat ~/evo-x2-read-token)   # fine-grained read-only token kept on the box
```

## Layout

```
~/models/
├── qwen38-flash-next/           # strategist (EngramHalo only)
│   ├── UD-IQ4_XS/                       ← 3-part split, ~88 GB total
│   │   ├── Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf   (11 MB)
│   │   ├── Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf   (47 GB)
│   │   └── Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf   (41 GB)
│   └── mtp-Qwen3.8-Flash-Next-Q8_0.gguf                       (3.9 GB, MTP sidecar)
├── qwen38-27b/            Qwen3.8-27B-UD-Q4_K_XL.gguf  + MTP/mtp-…-Q4_0.gguf
├── hermes-35b/            Hermes3.6-35B-A3B-…-MTP-APEX-Compact.gguf + template + system prompt
├── qwen36-35b-a3b-mtp/    Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
├── qwen35-9b-mtp/         Qwen3.5-9B-UD-Q4_K_XL.gguf
├── fable-27b/             Qwen3.6-27B-Fable-…-AMD-MTP-IQ4_XS.gguf
├── qwen3-coder-30b-a3b/   Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf   (spare)
└── Qwen_Qwen3.6-27B-Q4_K_M.gguf                                          (spare)
```

For split GGUFs you only ever pass **part 00001** to `-m`; llama.cpp follows
the split metadata to the sibling files.

## Qwen3.8-Flash-Next — UD-IQ4_XS (3-part) + MTP sidecar

The flagship strategist slot. Runs **only** on the EngramHalo fork
([llama-cpp-build.md](llama-cpp-build.md)).

```bash
hf download unsloth/Qwen3.8-Flash-Next-GGUF \
  "UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf" \
  "UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf" \
  "UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf" \
  --local-dir ~/models/qwen38-flash-next/

# MTP speculative-decoding draft head (Strix-Halo-tuned, Q8_0):
hf download EasiiX/Qwen3.8-Flash-Next-MTP-Strix-Halo-GGUF \
  mtp-Qwen3.8-Flash-Next-Q8_0.gguf \
  --local-dir ~/models/qwen38-flash-next/
```

Notes:

- The three parts **must stay in the same directory** and keep their names —
  llama.cpp resolves the split from part 00001's GGUF split metadata.
- UD-IQ4_XS is the size class that fits this box: IQ4_XS quantized weights
  (~88 GB incl. the ~27 GB engram table), of which the engram table stays
  SSD-backed at long context. This is the config measured in the fork's
  benchmarks (78K/156K depth, MTP on).
- The MTP sidecar is loaded with `-md` at launch
  ([scripts/start-flash-next.sh](scripts/start-flash-next.sh)).

## Qwen3.8-27B — UD-Q4_K_XL + MTP

Stock strategist (port 8082).

```bash
hf download unsloth/Qwen3.8-27B-GGUF \
  "Qwen3.8-27B-UD-Q4_K_XL.gguf" \
  "MTP/mtp-Qwen3.8-27B-Q4_0.gguf" \
  --local-dir ~/models/qwen38-27b/
```

- `UD` = Unsloth's "Ultra Dynamic" dynamic quant (more bits where it matters).
- The `MTP/` subfolder file (`mtp-Qwen3.8-27B-Q4_0.gguf`) is the
  multi-token-prediction head used by `--spec-type draft-mtp`.

## Hermes 3.6-35B-A3B (orchestrator, port 8081)

Uncensored Hermes finetune of Qwen3.6-35B-A3B (MoE, 3B active) with MTP —
the main Qwen Code model.

```bash
hf download LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-V10-GGUF \
  Hermes3.6-35B-A3B-Uncensored-Genesis-V10-MTP-APEX-Compact.gguf \
  --local-dir ~/models/hermes-35b/

# Its own chat template + system prompt ship in the same repo:
hf download LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-V10-GGUF \
  chat_template.jinja --local-dir ~/models/hermes-35b/
hf download LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-V10-GGUF \
  System_Prompt_Agent.txt --local-dir ~/models/hermes-35b/
```

The server loads the template with `--chat-template-file` and forces
`tool_call_format: json` via `--chat-template-kwargs` (see
[scripts/start-llm-servers.sh](scripts/start-llm-servers.sh)) — the Hermes
template supports a `tool_call_format` switch and the JSON variant is what our
router/Claude-Code flow expects.

## Qwen3.5-9B — UD-Q4_K_XL (worker, port 8080)

The fast tier: subagent execution, small-fast model for Claude Code.

```bash
hf download unsloth/Qwen3.5-9B-MTP-GGUF \
  Qwen3.5-9B-UD-Q4_K_XL.gguf \
  --local-dir ~/models/qwen35-9b-mtp/
```

(MTP is baked into the checkpoint naming family — `--spec-type draft-mtp`
works without a separate sidecar file.)

## Qwen3.6-35B-A3B — UD-Q4_K_XL (spare orchestrator)

Same A3B MoE base as Hermes, stock weights, kept as a fallback orchestrator:

```bash
hf download unsloth/Qwen3.6-35B-A3B-MTP-GGUF \
  Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
  --local-dir ~/models/qwen36-35b-a3b-mtp/
```

## Fable-27B AMD MTP IQ4_XS (alternative strategist, port 8082)

DavidAU's Fable-Fusion 711 merge (abliterated/uncensored Qwen3.6-27B),
**AMD-specific MTP build** at IQ4_XS:

```bash
hf download DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF \
  Qwen3.6-27B-Fable-Fus-711-UnHeretic-NM-DAU-NEO-MAX-NEO-AMD-MTP-IQ4_XS.gguf \
  --local-dir ~/models/fable-27b/
```

"AMD" in the filename means the MTP head was trained/exported targeting AMD
MFMA kernels; launch it with
[scripts/start-fable-27b.sh](scripts/start-fable-27b.sh) instead of the stock
27B script.

## Spares

```bash
hf download unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF \
  Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf \
  --local-dir ~/models/qwen3-coder-30b-a3b/

HF_TOKEN=$(cat ~/evo-x2-read-token) hf download bartowski/Qwen_Qwen3.6-27B-GGUF \
  Qwen_Qwen3.6-27B-Q4_K_M.gguf --local-dir ~/models/
```

## Chat template fix: `~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja`

```bash
hf download froggeric/Qwen-Fixed-Chat-Templates \
  chat_template.jinja \
  --local-dir ~/scripts/templates/qwen-fixed/3_8-27B/
```

**Why it's needed:** agentic clients (Claude Code foremost) inject **system
messages mid-conversation** — reminders after tool results, safety/context
notices mid-session. The stock Qwen chat template only handles a system
message in the leading position; a system turn appearing mid-history
gets dropped or rendered in a position the model was never trained on, which
causes runaway/garbage generations. The fixed template (community repo
`froggeric/Qwen-Fixed-Chat-Templates`) renders mid-conversation system
messages in the trained-compatible way.

Every 27B-class strategist launch passes
`--chat-template-file ~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja`
(see all three scripts in [scripts/](scripts/)); without it Claude Code
sessions degrade within a few tool calls. The Hermes orchestrator uses its own
bundled template instead.

Next: [launch-flags.md](launch-flags.md) for what all these launch flags mean.
