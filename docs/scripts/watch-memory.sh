#!/bin/bash
tmux new-session -d -s memory-watch "watch -n 2 'free -h && rocm-smi --showmeminfo vram'"
echo "Done. Attach with: tmux attach -t memory-watch"
