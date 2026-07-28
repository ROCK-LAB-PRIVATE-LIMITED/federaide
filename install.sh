#!/bin/bash
# ==============================================================================
#            FEDERaiDE Unified Cross-Platform Installer Script
# ==============================================================================
# Supported Platforms:
# - macOS (Intel x86_64 & Apple Silicon ARM64)
# - Linux (x86_64 & ARM64 / aarch64)
# - Android (Termux environment)
# - Windows (WSL & native environments via Git Bash/MSYS/Cygwin)
# ==============================================================================
set -e

echo "======================================================================"
echo "          FEDERaiDE Universal uv-Based Installer Bootstrapper        "
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

echo "[*] System Architecture: $ARCH_NAME"

# 2. Windows Delegation Routing
if [ "$IS_WINDOWS_BASH" = true ]; then
    echo "[*] Windows Bash environment detected (Git Bash / MSYS / Cygwin)."
    echo "[*] Transitioning execution context to native Windows PowerShell..."
    
    # Hand off execution to native Windows PowerShell to bootstrap native Windows binaries on Python 3.13
    powershell.exe -ExecutionPolicy Bypass -Command "
        Write-Host '[*] Bootstrapping uv on Windows via PowerShell...' -ForegroundColor Cyan
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            irm https://astral.sh/uv/install.ps1 | iex
            \$env:PATH = [System.Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
        }
        Write-Host '[*] Installing FEDERaiDE on standardized Python 3.13 environment...' -ForegroundColor Cyan
        uv tool install --refresh --python 3.13 'federaide[all]'
    "
    echo "======================================================================"
    echo " 🎉 Windows installation complete!"
    echo " Please restart your terminal/shell to ensure paths are updated."
    echo "======================================================================"
    exit 0
fi

# 3. Unix-Based Setup (Linux, macOS, Termux, WSL)
if [ "$IS_TERMUX" = true ]; then
    echo "[*] Platform: Android (Termux)"
elif [ "$OS_NAME" = "Darwin" ]; then
    echo "[*] Platform: macOS ($OS_NAME)"
elif [ "$OS_NAME" = "Linux" ]; then
    # Check if running under WSL
    if grep -qE "(Microsoft|Microsoft|wsl)" /proc/version 2>/dev/null; then
        echo "[*] Platform: Linux (WSL)"
    else
        echo "[*] Platform: Linux (Native)"
    fi
else
    echo "[*] Platform: POSIX-compliant Unix ($OS_NAME)"
fi

# 4. Ensure uv is Installed on Unix-like Systems
ensure_uv_unix() {
    if command -v uv &> /dev/null; then
        echo "[*] uv is already available on your PATH."
        return 0
    fi

    echo "[*] uv not detected. Commencing installer..."

    if [ "$IS_TERMUX" = true ]; then
        echo "[*] Installing native Termux packages for uv and weasyprint dependencies..."
        pkg update -y
        pkg install -y uv pango gobject-introspection libffi pkg-config
    else
        echo "[*] Running official Astral standalone uv installer..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi

    # Append potential install directories to PATH for immediate use
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if ! command -v uv &> /dev/null; then
        echo "[!] uv installation could not be verified automatically."
        echo "[*] Please install uv manually (https://docs.astral.sh/uv/) and run:"
        echo "    uv tool install --refresh federaide[all]"
        return 1
    fi

    echo "[*] uv successfully configured."
}

# 5. Perform Unix-Based Installation using uv tool
install_federaide_unix() {
    # GitHub repository config parameters for pre-compiled "Tyre" wheels
    REPO_OWNER="ROCK-LAB-PRIVATE-LIMITED"  # <-- CHANGE THIS to your GitHub organization/username
    REPO_NAME="FEDERaiDE"       # <-- CHANGE THIS to your repository name
    BRANCH="main"

    # Use Termux/Android writable temp directory variable if defined, falling back to /tmp
    TYRES_DIR="${TMPDIR:-/tmp}/federaide_tyres"
    rm -rf "$TYRES_DIR" && mkdir -p "$TYRES_DIR"
    RAW_URL="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${BRANCH}/tyres"

    if [ "$IS_TERMUX" = true ]; then
        echo "[*] Android (Termux) environment detected."
        
        # Clear out any lingering environment variables from previous terminal runs
        unset UV_FIND_LINKS

        # Ensure weasyprint system dependencies are present even if uv was already installed
        echo "[*] Ensuring required system packages are installed..."
        pkg install -y pango gobject-introspection libffi pkg-config tree-sitter-python tree-sitter-go tree-sitter-rust tree-sitter-c tree-sitter-bash libjpeg-turbo libtiff libpng

        # 1. Define a safe working directory in Termux home space for dummy builds
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
        
        # Copy the dummy wheel to TYRES_DIR so we only need one find-links directory
        cp "$BUILD_DIR/dist/"*.whl "$TYRES_DIR/" 2>/dev/null || true

        echo "    [*] Downloading precompiled Python 3.13 wheels from tyres folder..."

        DOWNLOAD_SUCCESS=true
        
        export ANDROID_API_LEVEL=19
        if [ "$DOWNLOAD_SUCCESS" = true ]; then
            echo "[*] Installing FEDERaiDE with full extras [all] using pre-compiled wheels on Python 3.13..."
            uv tool install --refresh --python 3.13 \
                --find-links "$TYRES_DIR" \
                --find-links "https://geoarkadeep.github.io/Tyres/" \
                --with pycryptodome \
                --with tree-sitter \
                --with keyrings.alt \
                --with weasyprint \
                "federaide"
        else
            echo "[!] Pre-compiled wheels not found."
            echo "[!] Falling back to basic installation (no extras) to prevent compilation hangs."
            uv tool install --refresh --python 3.13 \
                --with pycryptodome \
                --with tree-sitter \
                --with tree-sitter-python \
                --with tree-sitter-go \
                --with tree-sitter-c \
                --with keyrings.alt \
                --with weasyprint \
                federaide
        fi
        
        # Ensure the executable directory is added to the Termux path permanently
        grep -qF ".local/bin" ~/.bashrc 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
        
        unset UV_FIND_LINKS
        rm -rf "$BUILD_DIR"

    elif [ "$OS_NAME" = "Linux" ] && [ "$IS_ARM" = true ]; then
        echo "[*] Linux ARM64 (aarch64) environment detected."
        echo "[*] Downloading precompiled Python 3.13 wheels from tyres folder..."

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
            echo "[*] Installing FEDERaiDE with full extras [all] using pre-compiled wheels on Python 3.13..."
            uv tool install --refresh --python 3.13 --find-links "$TYRES_DIR" "federaide[all]"
        else
            echo "[!] Pre-compiled wheels not found."
            echo "[!] Falling back to basic installation (no extras) to prevent compilation hangs."
            uv tool install --refresh --python 3.13 federaide
        fi

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
                echo "[*] Installing Pango, Cairo, Glib, and Gobject-Introspection via Homebrew..."
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
time.sleep(0.5)
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

        echo "[*] Installing FEDERaiDE with all features on standardized Python 3.13 environment..."
        if [ -n "$LATEST_VER" ]; then
            echo "[*] Target version resolved: v$LATEST_VER"
            if ! uv tool install --refresh --python 3.13 "federaide[all]==$LATEST_VER"; then
                echo "[!] Explicit installation of v$LATEST_VER failed. Falling back to standard resolution..."
                uv tool install --refresh --python 3.13 "federaide[all]"
            fi
        else
            uv tool install --refresh --python 3.13 "federaide[all]"
        fi
    fi

    # Clean up cached binaries
    rm -rf "$TYRES_DIR"
}

# Execute Unix sequence
if ensure_uv_unix; then
    install_federaide_unix
    echo "======================================================================"
    echo " 🎉 Installation finished successfully!"
    echo "======================================================================"
    echo " To ensure both 'uv' and 'federaide' are on your shell's PATH, "
    echo " please restart your terminal or run:"
    echo "     source \$HOME/.local/bin/env"
    echo "     source ~/.bashrc  (or ~/.zshrc if using Zsh)"
    echo ""
    echo " To run the application:"
    echo "     federaide"
    echo "======================================================================"
else
    echo "[!] Installation could not be completed."
    exit 1
fi