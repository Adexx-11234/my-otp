#!/bin/bash
# Install Chrome if not present
if ! command -v google-chrome &> /dev/null; then
    apt-get update -qq
    apt-get install -y -qq google-chrome-stable 2>/dev/null || \
    apt-get install -y -qq chromium-browser 2>/dev/null || \
    echo "Chrome install attempted"
fi
python main.py
