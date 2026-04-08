#!/bin/bash

REBUILD=false

# Parse arguments
if [ "$1" == "--rebuild" ]; then
    REBUILD=true
    echo "🔄 Rebuild mode enabled - will recreate the venv"
fi

echo "-------------------------------------"
echo "Updating current directory"
echo "-------------------------------------"

# Rebuild mode: remove existing venv
if [ "$REBUILD" = true ] && [ -d "./venv" ]; then
    echo "🗑️  Removing existing venv..."
    rm -rf ./venv
fi

# Check if ./venv exists
if [ -d "./venv" ]; then
    source venv/bin/activate
    echo "Virtual environment exists, upgrading requirements..."
    echo "Upgrade Pip..."
    pip install --upgrade pip
    if [ -f "requirements.txt" ] && [ -s "requirements.txt" ]; then
        echo "Upgrade requirements..."
        pip install -r requirements.txt --upgrade --no-cache-dir
    fi
    if [ -f "requirements-dev.txt" ]; then
        pip install -r requirements-dev.txt --upgrade --no-cache-dir
    fi
else
    echo "Virtual environment does not exist, creating one..."
    python3.13 -m venv venv
    source venv/bin/activate
    echo "Install Pip..."
    python3.13 -m pip install -U pip wheel setuptools
    if [ -f "requirements.txt" ] && [ -s "requirements.txt" ]; then
        echo "Install requirements..."
        pip install -r requirements.txt --no-cache-dir
    fi
    if [ -f "requirements-dev.txt" ]; then
        pip install -r requirements-dev.txt --no-cache-dir
    fi
fi
