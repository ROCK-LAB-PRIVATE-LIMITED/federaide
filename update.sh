#!/bin/bash
# ==============================================================================
#            FEDERaiDE Unified Cross-Platform Updater Script
# ==============================================================================
# Supported Platforms:
# - macOS (Intel & Apple Silicon)
# - Linux (x86_64 & ARM)
# - Android (Termux environment)
# - Windows (WSL & native environments via Git Bash/MSYS/Cygwin)
# ==============================================================================
set -e

echo "======================================================================"
echo "          FEDERaiDE Universal uv-Based Updater                          "
echo "======================================================================"

# 1. Platform and Shell Detection
OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
IS_TERMUX=false
IS_WINDOWS_BASH=false
IS_ARM=false

# Detect Termux (Android)
if [ -d "/data/data/com.termux" ]; then
    IS_TERMUX=true
fi

# Detect Windows Bash Environment (Git Bash, MSYS, Cygwin)
case "$OS_NAME" in
    *MINGW*|*MSYS*|*CYGWIN*)
        IS_WINDOWS_BASH=true
        ;;
esac

# Detect ARM Architecture (for Linux ARM checks)
case "$ARCH_NAME" in
    arm*|aarch64*|arm64*)
        IS_ARM=true
        ;;
esac

# Ensure standard uv local bin paths are on the PATH for the updater
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# 2. Windows Delegation Routing
if [ "$IS_WINDOWS_BASH" = true ]; then
    echo "[*] Windows Bash environment detected (Git Bash / MSYS / Cygwin)."
    echo "[*] Transitioning execution context to native Windows PowerShell..."
    
    # Hand off execution to native Windows PowerShell
    powershell.exe -ExecutionPolicy Bypass -File "./update.ps1" "$@"
    exit 0
fi

# 3. Command Line Arguments Parsing
FORCE=false
for arg in "$@"; do
    if [ "$arg" = "--force" ] || [ "$arg" = "-f" ]; then
        FORCE=true
    fi
done

# 4. Check for uv installation
if ! command -v uv &> /dev/null; then
    echo "[!] 'uv' command not detected on your environment PATH."
    echo "[*] Please run the installer script (install.sh) first to initialize uv and FEDERaiDE."
    exit 1
fi

# 5. Determine installed version
INSTALLED_VER=$(python3 -c "import importlib.metadata; print(importlib.metadata.version('federaide'))" 2>/dev/null || \
                python3 -c "import pkg_resources; print(pkg_resources.get_distribution('federaide').version)" 2>/dev/null || \
                uv tool list 2>/dev/null | grep -E '^federaide ' | awk '{print $2}' | tr -d 'v' || echo "")

if [ -n "$INSTALLED_VER" ]; then
    echo "[*] Installed Version: v$INSTALLED_VER"
else
    echo "[*] Installed Version: Not Detected"
fi

# 6. Query PyPI twice to prevent CDN cache miss
echo "[*] Querying PyPI for the latest version..."
LATEST_VER=$(python3 -c "
import urllib.request, json, time, random
def get_pypi_version():
    try:
        url = f'https://pypi.org/pypi/federaide/json?cb={random.randint(1, 1000000)}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data['info']['version']
    except Exception:
        return None

v1 = get_pypi_version()
time.sleep(1.0)
v2 = get_pypi_version()

if v1 and v2:
    try:
        p1 = tuple(map(int, [x for x in v1.split('.') if x.isdigit()]))
        p2 = tuple(map(int, [x for x in v2.split('.') if x.isdigit()]))
        print(v2 if p2 >= p1 else v1)
    except Exception:
        print(v2)
elif v2:
    print(v2)
elif v1:
    print(v1)
else:
    print('')
" 2>/dev/null || echo "")

if [ -n "$LATEST_VER" ]; then
    echo "[*] Latest PyPI Version: v$LATEST_VER"
else
    echo "[!] Warning: Could not retrieve latest version from PyPI automatically."
fi

# 7. Compare versions
UP_TO_DATE=false
if [ -n "$INSTALLED_VER" ] && [ -n "$LATEST_VER" ]; then
    if [ "$INSTALLED_VER" = "$LATEST_VER" ]; then
        UP_TO_DATE=true
    fi
fi

if [ "$UP_TO_DATE" = true ] && [ "$FORCE" = false ]; then
    echo "======================================================================"
    echo " ℹ️ FEDERaiDE is already up-to-date (v$INSTALLED_VER)."
    echo " If you want to force-reinstall or refresh the installation, please run:"
    echo "     ./update.sh --force"
    echo "======================================================================"
    exit 0
fi

# 8. Cleanup legacy fedserve binary shims and tool environments
uv tool uninstall fedserve >/dev/null 2>&1 || true
rm -f "$HOME/.local/bin/fedserve" \
      "$HOME/.cargo/bin/fedserve" \
      "/usr/local/bin/fedserve" \
      "/usr/bin/fedserve" 2>/dev/null || true

# 9. Execute update based on platform
if [ "$IS_TERMUX" = true ]; then
    echo "[*] Android (Termux) environment detected."
    echo "[*] Ensuring required system packages are installed..."
    pkg install -y pango gobject-introspection libffi pkg-config tree-sitter-python tree-sitter-go tree-sitter-rust tree-sitter-c tree-sitter-bash libjpeg-turbo libtiff libpng openjpeg

    # Define a safe working directory in Termux home space for dummy builds
    BUILD_DIR="$HOME/.tmp_sqlite_vec_build"
    rm -rf "$BUILD_DIR"
    mkdir -p "$BUILD_DIR/sqlite_vec"

    echo "    [*] Creating dummy sqlite-vec package structures to bypass compilation..."
    touch "$BUILD_DIR/README.md"
    touch "$BUILD_DIR/sqlite_vec/__init__.py"
    cat << 'EOF' > "$BUILD_DIR/pyproject.toml"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "sqlite-vec"
version = "0.1.9"
description = "Dummy package to trick the Android environment resolver"
readme = "README.md"
requires-python = ">=3.8"
EOF

    echo "    [*] Building platform-agnostic universal wheel for sqlite-vec..."
    (cd "$BUILD_DIR" && uv build --wheel)
    
    TYRES_DIR="${TMPDIR:-/tmp}/federaide_tyres"
    rm -rf "$TYRES_DIR" && mkdir -p "$TYRES_DIR"
    cp "$BUILD_DIR/dist/"*.whl "$TYRES_DIR/" 2>/dev/null || true

    export ANDROID_API_LEVEL=19

    echo "[*] Upgrading FEDERaiDE on Python 3.13..."
    uv tool install --force --upgrade --refresh --python 3.13 \
        --find-links "$TYRES_DIR" \
        --find-links "https://geoarkadeep.github.io/Tyres/" \
        --with pycryptodome \
        --with tree-sitter \
        --with keyrings.alt \
        --with weasyprint \
        "federaide"

    unset UV_FIND_LINKS
    rm -rf "$BUILD_DIR"
    rm -rf "$TYRES_DIR"

elif [ "$OS_NAME" = "Linux" ] && [ "$IS_ARM" = true ]; then
    echo "[*] Linux ARM64 (aarch64) environment detected."
    TYRES_DIR="${TMPDIR:-/tmp}/federaide_tyres"
    rm -rf "$TYRES_DIR" && mkdir -p "$TYRES_DIR"
    
    REPO_OWNER="ROCK-LAB-PRIVATE-LIMITED"
    REPO_NAME="FEDERaiDE"
    BRANCH="main"
    RAW_URL="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${BRANCH}/tyres"
    
    WHEELS=(
        "numpy-2.4.4-cp312-cp312-manylinux2014_aarch64.whl"
    )

    DOWNLOAD_SUCCESS=false
    for wheel in "${WHEELS[@]}"; do
        echo "    [*] Downloading: $wheel"
        if curl -LsSf "$RAW_URL/$wheel" -o "$TYRES_DIR/$wheel"; then
            echo "        [+] Success: Local binary cached."
            DOWNLOAD_SUCCESS=true
        else
            echo "        [!] Warning: Precompiled binary not found."
        fi
    done

    if [ "$DOWNLOAD_SUCCESS" = true ]; then
        echo "[*] Upgrading FEDERaiDE with full extras [all] using pre-compiled wheels on Python 3.13..."
        uv tool install --force --upgrade --refresh --python 3.13 --find-links "$TYRES_DIR" "federaide[all]"
    else
        echo "[!] Pre-compiled wheels not found."
        echo "[!] Falling back to basic upgrade to prevent compilation hangs."
        uv tool install --force --upgrade --refresh --python 3.13 federaide
    fi
    rm -rf "$TYRES_DIR"

else
    # macOS, WSL, or standard Linux (x86_64) - Standardized on Python 3.13
    echo "[*] Desktop/Server environment detected."
    if [ "$OS_NAME" = "Darwin" ]; then
        echo "[*] macOS environment detected. Checking for Homebrew..."
        if ! command -v brew &> /dev/null; then
            echo "[*] Homebrew not found. Attempting to install Homebrew..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/null || true
            if [ -f /opt/homebrew/bin/brew ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [ -f /usr/local/bin/brew ]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
        fi
        
        if command -v brew &> /dev/null; then
            echo "[*] Installing/Upgrading Pango, Cairo, Glib, and Gobject-Introspection via Homebrew..."
            brew install pango cairo glib gobject-introspection
            
            BREW_LIB_DIR="$(brew --prefix)/lib"
            export DYLD_FALLBACK_LIBRARY_PATH="$BREW_LIB_DIR:$DYLD_FALLBACK_LIBRARY_PATH"
            
            for profile in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
                if [ -f "$profile" ] || [ "${profile##*/}" = ".zshrc" -a "$SHELL" = "/bin/zsh" ] || [ "${profile##*/}" = ".bash_profile" -a "$SHELL" = "/bin/bash" ]; then
                    touch "$profile"
                    if ! grep -q "DYLD_FALLBACK_LIBRARY_PATH" "$profile"; then
                        echo "" >> "$profile"
                        echo "# FEDERaiDE.AI WeasyPrint library path" >> "$profile"
                        echo "export DYLD_FALLBACK_LIBRARY_PATH=\"$BREW_LIB_DIR:\$DYLD_FALLBACK_LIBRARY_PATH\"" >> "$profile"
                        echo "[*] Configured DYLD_FALLBACK_LIBRARY_PATH in $profile"
                    fi
                fi
            done
        else
            echo "[!] Homebrew could not be verified. Please install Homebrew manually and run:"
            echo "    brew install pango cairo glib gobject-introspection"
        fi
    fi
    echo "[*] Upgrading FEDERaiDE on Python 3.13..."
    uv tool install --force --upgrade --refresh --python 3.13 "federaide[all]"
fi

echo "======================================================================"
echo " 🎉 FEDERaiDE has been successfully updated!"
echo "======================================================================"