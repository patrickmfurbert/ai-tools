#!/bin/bash
# Alternative strategist slot (:8082): Fable-27B — DavidAU's Fable-Fusion 711
# abliterated merge of Qwen3.6-27B, AMD-specific MTP build at IQ4_XS.
# Mutually exclusive with start-qwen-27b.sh / start-flash-next.sh (all use :8082).
# Kill the current 8082 session first:  tmux kill-session -t qwen36-27b
# Flags explained in docs/launch-flags.md.
export HSA_ENABLE_SDMA=0
export HSA_XNACK=1

echo "Starting Fable-27B (abliterated) on port 8082 (strategist)..."
tmux new-session -d -s fable-27b \
  "~/llama.cpp/build/bin/llama-server \
    -m ~/models/fable-27b/Qwen3.6-27B-Fable-Fus-711-UnHeretic-NM-DAU-NEO-MAX-NEO-AMD-MTP-IQ4_XS.gguf \
    -ngl 999 -c 262144 \
    --fa on --jinja \
    --chat-template-file ~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja \
    --parallel 2 --threads 16 \
    --reasoning off -fit off --no-ui \
    --spec-type draft-mtp \
    --spec-draft-n-max 1 \
    --host 0.0.0.0 --port 8082"

echo "Done. Attach with: tmux attach -t fable-27b"
