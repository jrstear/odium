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

# --- Environment file ---
ENV_FILE="$HOME/.odium/env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo ""
    echo "Creating ~/.odium/env (secrets + config)..."
    mkdir -p "$HOME/.odium"
    cat > "$ENV_FILE" <<'ENVEOF'
# odium environment — single source of truth for all secrets and config

# Anthropic
ANTHROPIC_API_KEY=

# AWS (credentials stay in ~/.aws/credentials; this sets the profile)
AWS_PROFILE=default

# EC2 / ODM
ODM_SSH_KEY=~/.ssh/geo-odm-ec2.pem
ODM_NOTIFY_EMAIL=

# Grafana Cloud (optional — for ODM telemetry)
GRAFANA_API_KEY=
GRAFANA_SA_KEY=
GRAFANA_STACK_URL=
GRAFANA_PROM_URL=
GRAFANA_PROM_USER=
GRAFANA_LOKI_URL=
GRAFANA_LOKI_USER=

# odium agent
ODIUM_MODEL=claude-haiku-4-5
ODIUM_DISPLAY=500
ENVEOF
    chmod 600 "$ENV_FILE"
    echo "  Created $ENV_FILE — edit it to add your API keys"
else
    echo "  ~/.odium/env exists"
fi

echo ""
echo "Setup complete. To run odium:"
echo "  conda run -n $ENV_NAME python agent.py"
echo ""
echo "Edit ~/.odium/env to configure API keys and secrets."
