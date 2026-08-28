#!/bin/bash
# Stock strategist slot (:8082): Qwen3.8-27B UD-Q4_K_XL with MTP + ngram-mod.
# This is the default 8082 model started by start-llm-servers.sh; this script
# (re)starts just that slot, e.g. after running Fable-27B or Flash-Next.
# Kill the current 8082 server first:
#   tmux kill-session -t fable-27b   # or: tmux kill-session -t flash-next
# Flags explained in docs/launch-flags.md.
export HSA_ENABLE_SDMA=0
export HSA_XNACK=1

echo "Starting Qwen3.6-27B (stock) on port 8082 (strategist)..."
tmux new-session -d -s qwen36-27b \
  "~/llama.cpp/build/bin/llama-server \
    -m ~/models/qwen38-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf \
    -ngl 999 -c 262144 \
    --fa on --jinja \
    --chat-template-file ~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja \
    --parallel 2 --threads 16 \
    --reasoning off -fit off --no-ui \
    --spec-type draft-mtp,ngram-mod \
    --spec-draft-n-max 1 --spec-ngram-mod-n-min 24 \
    --host 0.0.0.0 --port 8082"

echo "Done. Attach with: tmux attach -t qwen36-27b"
