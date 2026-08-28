#!/bin/bash
# Qwen3.8-Flash-Next (qwen4exp arch) on the EngramHalo.cpp fork — strategist slot (:8082).
#
# Requires:
#   - ~/llama-engramhalo built for gfx1151 (docs/llama-cpp-build.md)
#   - ~/models/qwen38-flash-next/ populated (3-part UD-IQ4_XS + MTP sidecar, docs/models.md)
#   - 64 GB swapfile active (docs/swap-setup.md) — 131K context will page to swap
#   - Port 8082 free: kill qwen36-27b / fable-27b sessions first (mutually exclusive).
#
# Every flag explained in docs/launch-flags.md.
export HSA_ENABLE_SDMA=0          # no SDMA copy engines (Strix Halo hang fix)
export HSA_XNACK=1                # unified-memory paging semantics (Strix Halo)
export ROCBLAS_USE_HIPBLASLT=1    # hipBLASLt GEMM path (tuned gfx1151 tiles)

echo "Starting Qwen3.8-Flash-Next on port 8082 (strategist)..."
tmux new-session -d -s flash-next \
  "~/llama-engramhalo/build/bin/llama-server \
    -m ~/models/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf \
    -md ~/models/qwen38-flash-next/mtp-Qwen3.8-Flash-Next-Q8_0.gguf \
    -ngl 999 \
    -fa on \
    -ctk q8_0 -ctv q8_0 \
    -c 131072 \
    -ub 2048 -t 4 \
    --parallel 1 \
    --jinja --no-ui \
    --chat-template-file ~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja \
    --spec-type draft-mtp,ngram-mod \
    --spec-draft-n-max 4 --spec-draft-p-min 0.75 \
    --host 0.0.0.0 --port 8082"

echo "Done. Load takes several minutes (88 GB weights + swap)."
echo "Watch:  watch -n 2 'free -h && rocm-smi --showmeminfo vram'"
echo "Attach: tmux attach -t flash-next"
