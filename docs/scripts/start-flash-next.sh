#!/bin/bash
export HSA_ENABLE_SDMA=0
export HSA_XNACK=1
export ROCBLAS_USE_HIPBLASLT=1

# Ensure swap is active — required for Flash-Next at 131K context
if ! swapon --show | grep -q /swapfile; then
    echo "Activating swap (required for Flash-Next)..."
    sudo swapon /swapfile
fi
echo "Swap active: $(free -h | grep Swap)"

# Start memory monitor if not already running
if tmux has-session -t memory-watch 2>/dev/null; then
    echo "Memory monitor already running (tmux: memory-watch)"
else
    echo "Starting memory monitor..."
    tmux new-session -d -s memory-watch "watch -n 2 'free -h && rocm-smi --showmeminfo vram'"
fi

# Start Flash-Next if not already running
if tmux has-session -t flash-next 2>/dev/null; then
    echo "Flash-Next already running (tmux: flash-next)"
else
    echo "Starting Qwen3.8-Flash-Next on port 8082 (strategist)..."
    tmux new-session -d -s flash-next \
      "HSA_ENABLE_SDMA=0 HSA_XNACK=1 ROCBLAS_USE_HIPBLASLT=1 \
      ~/llama-engramhalo/build/bin/llama-server \
        -m ~/models/qwen38-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf \
        -md ~/models/qwen38-flash-next/mtp-Qwen3.8-Flash-Next-Q8_0.gguf \
        -ngl 999 -fa on -ctk q8_0 -ctv q8_0 \
        -c 131072 -ub 2048 -t 4 --parallel 1 \
        --jinja --no-ui \
        --chat-template-file ~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja \
        --spec-type draft-mtp,ngram-mod \
        --spec-draft-n-max 4 --spec-draft-p-min 0.75 \
        --host 0.0.0.0 --port 8082"
fi

echo "Done. Attach with:"
echo "  tmux attach -t flash-next"
echo "  tmux attach -t memory-watch"
