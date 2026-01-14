#!/bin/bash

REBUILD=false

# Parse arguments
if [ "$1" == "--rebuild" ]; then
    REBUILD=true
    echo "🔄 Rebuild mode enabled - will recreate all venvs"
fi

process_directory() {
    OUTPUT="$1"

    echo "-------------------------------------"
    echo "Updating $OUTPUT"
    echo "-------------------------------------"
    pushd "./$OUTPUT" >/dev/null

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

    popd >/dev/null
}

export -f process_directory
export REBUILD

num_cores=$(nproc)
echo num_cores: $num_cores

# Target service + all lambda subdirectories.
TARGET_DIRS=()
if [ -d "service" ]; then
    TARGET_DIRS+=("service")
fi
for dir in lambda_functions/*; do
    if [ -d "$dir" ]; then
        TARGET_DIRS+=("$dir")
    fi
done

printf '%s\n' "${TARGET_DIRS[@]}" | xargs -I{} -n1 -P "$num_cores" bash -c 'process_directory "$@"' _ {}
