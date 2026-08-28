#!/bin/bash
export HSA_ENABLE_SDMA=0
export HSA_XNACK=1

echo "Starting Qwen3.5-9B-MTP on port 8080 (worker)..."
tmux new-session -d -s qwen35-9b \
  "~/llama.cpp/build/bin/llama-server \
    -m ~/models/qwen35-9b-mtp/Qwen3.5-9B-UD-Q4_K_XL.gguf \
    -ngl 99 -c 262144 \
    --flash-attn on --jinja \
    --parallel 2 --threads 16 \
    --reasoning off -fit off --no-ui \
    --spec-type draft-mtp \
    --spec-draft-n-max 6 \
    --host 0.0.0.0 --port 8080"

echo "Starting Hermes3.6-35B-A3B on port 8081 (orchestrator)..."
tmux new-session -d -s hermes-35b \
  "~/llama.cpp/build/bin/llama-server \
    -m ~/models/hermes-35b/Hermes3.6-35B-A3B-Uncensored-Genesis-V10-MTP-APEX-Compact.gguf \
    -ngl 99 -c 262144 \
    --flash-attn on --jinja \
    --chat-template-file ~/models/hermes-35b/chat_template.jinja \
    --chat-template-kwargs '{\"tool_call_format\": \"json\"}' \
    --parallel 2 --threads 16 \
    --reasoning off -fit off --no-ui \
    --spec-type draft-mtp \
    --spec-draft-n-max 1 \
    --host 0.0.0.0 --port 8081"

echo "Starting Qwen3.6-27B on port 8082 (strategist)..."
tmux new-session -d -s qwen36-27b \
  "~/llama.cpp/build/bin/llama-server \
    -m ~/models/qwen38-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf \
    -ngl 99 -c 262144 \
    --flash-attn on --jinja \
    --chat-template-file ~/scripts/templates/qwen-fixed/3_8-27B/chat_template.jinja \
    --parallel 2 --threads 16 \
    --reasoning off -fit off --no-ui \
    --spec-type draft-mtp,ngram-mod \
    --spec-draft-n-max 1 --spec-ngram-mod-n-min 24 \
    --host 0.0.0.0 --port 8082"

echo "Done. Attach with:"
echo "  tmux attach -t qwen35-9b"
echo "  tmux attach -t hermes-35b"
echo "  tmux attach -t qwen36-27b"
