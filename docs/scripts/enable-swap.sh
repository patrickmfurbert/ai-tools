#!/bin/bash
if swapon --show | grep -q /swapfile; then
    echo "Swap already active:"
    swapon --show
else
    sudo swapon /swapfile
    echo "Swap activated:"
    swapon --show
fi
