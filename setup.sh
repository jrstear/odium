#!/usr/bin/env bash
# Setup the odium conda environment and dependencies.
set -e

ENV_NAME="odium"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- System dependencies ---
echo "Checking system dependencies..."

# Tesseract OCR (needed for scanned PDFs like control sheets)
if ! command -v tesseract &>/dev/null; then
    echo "Installing tesseract OCR..."
    if [[ "$(uname)" == "Darwin" ]]; then
        brew install tesseract
    elif command -v apt-get &>/dev/null; then
        sudo apt-get install -y tesseract-ocr
    else
        echo "ERROR: Please install tesseract manually: https://github.com/tesseract-ocr/tesseract"
        exit 1
    fi
else
    echo "  tesseract: $(tesseract --version 2>&1 | head -1)"
fi

# --- Conda environment ---
if conda info --envs | grep -q "^${ENV_NAME} "; then
    echo "Conda env '${ENV_NAME}' already exists — updating packages..."
else
    echo "Creating conda env '${ENV_NAME}' (Python 3.11)..."
    conda create -n "$ENV_NAME" python=3.11 -y
fi

echo "Installing Python dependencies..."
conda run -n "$ENV_NAME" pip install -q -r "$SCRIPT_DIR/requirements.txt"

# --- Verify ---
echo ""
echo "Verifying installation..."
conda run -n "$ENV_NAME" python -c "
import anthropic, fitz, pytesseract, dotenv
print('  All Python packages OK')
"

echo ""
echo "Setup complete. To run odium:"
echo "  conda run -n $ENV_NAME python agent.py"
echo ""
echo "Make sure you have a .env file with your API key:"
echo "  ANTHROPIC_API_KEY=sk-ant-..."
