#!/bin/bash
# Setup script for PID Upload Bot

# Use bash strict mode
set -euo pipefail

# Clean up if exists
rm -fdr pwbvenv $HOME/pywikibot-core

# Create virtual environment
python3 -m venv pwbvenv

# Activate it
source pwbvenv/bin/activate

# clone Pywikibot
git clone --depth 1 --single-branch --recurse-submodules --shallow-submodules --branch stable "https://gerrit.wikimedia.org/r/pywikibot/core" $HOME/pywikibot-core

# Upgrade pip and install wheel
pip install -U pip wheel setuptools

# Install requirements
pip install $HOME/pywikibot-core[mwoauth,mysql]
pip install -r $HOME/requirements.txt

echo "Virtual environment setup complete!"