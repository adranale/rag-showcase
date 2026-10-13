#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"
source .venv/Scripts/activate

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

python -m streamlit run chat_ui.py \
  --server.address 127.0.0.1 \
  --server.port 8501