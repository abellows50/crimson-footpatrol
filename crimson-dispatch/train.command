#!/bin/bash
# Fine-tune Whisper on the crew's corrected clips (see train_whisper.py). Quit the dispatch server first.
cd "$(dirname "$0")"
if pgrep -f "crimson-dispatch.*server.py|[s]erver.py --" >/dev/null 2>&1 || lsof -i :8080 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "The dispatch server is still running. Quit it first (training needs the GPU), then run this again."
  read -r -p "Press Enter to close. " _; exit 1
fi
if ! .venv/bin/python -c "import torch, transformers, peft" 2>/dev/null; then
  echo "First run: installing PyTorch, transformers and peft (a few minutes)..."
  .venv/bin/python -m pip install --quiet torch transformers peft || { read -r -p "Install failed. Press Enter to close. " _; exit 1; }
fi
.venv/bin/python train_whisper.py "$@"
read -r -p "Done. Press Enter to close. " _
