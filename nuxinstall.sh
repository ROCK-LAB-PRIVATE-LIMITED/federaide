#!/bin/sh
# ==============================================================================
#            FEDERaiDE Universal POSIX & Linux Installer Script
# ==============================================================================
# Compatible with both /bin/sh (BusyBox/ash/dash) and /bin/bash across:
# - Alpine Linux (apk - musl libc)
# - Debian / Ubuntu / Mint / Pop!_OS / Kali / Raspberry Pi OS (apt)
# - Fedora / RHEL / CentOS / Rocky Linux / AlmaLinux / Amazon Linux (dnf / yum)
# - Arch Linux / Manjaro / EndeavourOS (pacman)
# - openSUSE / SLES (zypper)
# - Void Linux (xbps-install)
# - Gentoo (emerge)
# - Android (Termux)
# ==============================================================================
set -e

echo "======================================================================"
echo "      FEDERaiDE Universal Linux & POSIX Installer Bootstrapper        "
echo "======================================================================"

# 1. Environment & Platform Detection
OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
IS_TERMUX=false
IS_ARM=false

if [ -d "/data/data/com.termux" ] || [ -n "$TERMUX_VERSION" ]; then
    IS_TERMUX=true
fi

case "$ARCH_NAME" in
    arm*|aarch64*|arm64*)
        IS_ARM=true
        ;;
esac

echo "[*] Operating System: $OS_NAME"
echo "[*] System Architecture: $ARCH_NAME"

# 2. Musl libc Detection Helper
is_musl() {
    if [ -f /etc/alpine-release ]; then
        return 0
    fi
    if ldd /bin/sh 2>&1 | grep -qi "musl"; then
        return 0
    fi
    if ls /lib/ld-musl* >/dev/null 2>&1 || ls /lib64/ld-musl* >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# 3. Downloader Helper Functions (Supports both curl and wget)
download_stdout() {
    url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$url"
    else
        echo "[!] Error: Neither 'curl' nor 'wget' is available on this system." >&2
        echo "[!] Please install 'curl' or 'wget' using your package manager and re-run." >&2
        return 1
    fi
}

download_file() {
    url="$1"
    dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$url" -o "$dest"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$dest" "$url"
    else
        echo "[!] Error: Neither 'curl' nor 'wget' is available on this system." >&2
        return 1
    fi
}

# 4. Privilege Escalation Helper
run_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    elif command -v doas >/dev/null 2>&1; then
        doas "$@"
    else
        echo "[!] Warning: Root privileges required for '$*'." >&2
        echo "[!] Neither 'sudo' nor 'doas' was found and not running as root." >&2
        return 1
    fi
}

# 5. System Package Manager Detection & WeasyPrint / Build Dependency Installation
install_system_dependencies() {
    echo "[*] Detecting Linux Distribution and Package Manager for WeasyPrint dependencies..."

    if [ "$IS_TERMUX" = true ]; then
        echo "[*] Environment: Android (Termux)"
        echo "[*] Installing Termux system packages via pkg..."
        pkg update -y || true
        pkg install -y uv pango gobject-introspection libffi pkg-config tree-sitter-python tree-sitter-go tree-sitter-rust tree-sitter-c tree-sitter-bash libjpeg-turbo libtiff libpng openjpeg || true
        return 0
    fi

    if command -v apk >/dev/null 2>&1; then
        echo "[*] Package Manager: apk (Alpine Linux)"
        run_root apk add --no-cache bash pango cairo gdk-pixbuf libffi-dev fontconfig openjpeg gcc musl-dev python3-dev pkgconf || true

    elif command -v apt-get >/dev/null 2>&1; then
        echo "[*] Package Manager: apt-get (Debian/Ubuntu/Mint/Pop!_OS/Raspberry Pi OS)"
        run_root apt-get update -y || true
        run_root apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info fontconfig openjpeg2-tools build-essential python3-dev pkg-config || true

    elif command -v dnf >/dev/null 2>&1; then
        echo "[*] Package Manager: dnf (Fedora/RHEL/CentOS/Rocky/AlmaLinux)"
        run_root dnf install -y pango pango-devel cairo cairo-devel gdk-pixbuf2 gdk-pixbuf2-devel libffi-devel fontconfig openjpeg2 gcc python3-devel pkgconfig || true

    elif command -v yum >/dev/null 2>&1; then
        echo "[*] Package Manager: yum (CentOS/RHEL)"
        run_root yum install -y pango pango-devel cairo cairo-devel gdk-pixbuf2 gdk-pixbuf2-devel libffi-devel fontconfig openjpeg2 gcc python3-devel pkgconfig || true

    elif command -v pacman >/dev/null 2>&1; then
        echo "[*] Package Manager: pacman (Arch Linux/Manjaro/EndeavourOS)"
        run_root pacman -Sy --noconfirm pango cairo gdk-pixbuf2 libffi fontconfig openjpeg2 pkgconf base-devel || true

    elif command -v zypper >/dev/null 2>&1; then
        echo "[*] Package Manager: zypper (openSUSE/SLES)"
        run_root zypper --non-interactive install pango pango-devel cairo cairo-devel gdk-pixbuf-devel libffi-devel fontconfig openjpeg || true

    elif command -v xbps-install >/dev/null 2>&1; then
        echo "[*] Package Manager: xbps-install (Void Linux)"
        run_root xbps-install -Sy pango pango-devel cairo cairo-devel gdk-pixbuf gdk-pixbuf-devel libffi-devel fontconfig openjpeg pkg-config || true

    elif command -v emerge >/dev/null 2>&1; then
        echo "[*] Package Manager: emerge (Gentoo)"
        run_root emerge --ask=n x11-libs/pango x11-libs/cairo x11-libs/gdk-pixbuf dev-libs/libffi media-libs/fontconfig || true

    else
        echo "[!] Warning: Unrecognized or non-standard package manager."
        echo "[!] Please manually ensure Pango, Cairo, GdkPixbuf, libffi, and Fontconfig are installed on your distribution for WeasyPrint support."
    fi
}

install_system_dependencies

# 6. Ensure PATH includes standard local binary locations
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# 7. Ensure uv is Installed
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        echo "[*] uv is already installed and available on PATH."
        return 0
    fi

    echo "[*] uv not detected. Commencing installer..."

    if [ "$IS_TERMUX" = true ]; then
        pkg install -y uv || true
    else
        echo "[*] Installing Astral standalone uv package manager..."
        download_stdout https://astral.sh/uv/install.sh | sh
    fi

    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if ! command -v uv >/dev/null 2>&1; then
        if [ -x "$HOME/.local/bin/uv" ]; then
            alias uv="$HOME/.local/bin/uv"
        elif [ -x "$HOME/.cargo/bin/uv" ]; then
            alias uv="$HOME/.cargo/bin/uv"
        else
            echo "[!] Error: uv installation could not be verified automatically." >&2
            echo "[!] Please install uv manually (https://docs.astral.sh/uv/) and re-run this script." >&2
            return 1
        fi
    fi

    echo "[*] uv successfully configured."
}

ensure_uv

# 8. Dummy sqlite-vec Wheel Generator (Bypasses C compilation crashes on musl/Alpine/Termux)
build_dummy_sqlite_vec() {
    tyres_target_dir="$1"
    echo "    [*] Building dummy sqlite-vec wheel to bypass C-extension compilation on musl/mobile..."

    BUILD_DIR="$HOME/.tmp_sqlite_vec_build"
    rm -rf "$BUILD_DIR"
    mkdir -p "$BUILD_DIR/sqlite_vec"

    touch "$BUILD_DIR/README.md"
    touch "$BUILD_DIR/sqlite_vec/__init__.py"
    cat << 'EOF' > "$BUILD_DIR/pyproject.toml"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "sqlite-vec"
version = "0.1.9"
description = "Dummy package to bypass C-extension build on musl/mobile"
readme = "README.md"
requires-python = ">=3.8"
EOF

    (cd "$BUILD_DIR" && uv build --wheel) >/dev/null 2>&1 || true
    cp "$BUILD_DIR/dist/"*.whl "$tyres_target_dir/" 2>/dev/null || true
    rm -rf "$BUILD_DIR"
}

# 9. Perform Installation via uv tool
install_federaide() {
    REPO_OWNER="ROCK-LAB-PRIVATE-LIMITED"
    REPO_NAME="FEDERaiDE"
    BRANCH="main"

    TYRES_DIR="${TMPDIR:-/tmp}/federaide_tyres"
    rm -rf "$TYRES_DIR" && mkdir -p "$TYRES_DIR"
    RAW_URL="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${BRANCH}/tyres"

    # Always generate dummy sqlite-vec wheel on musl/Alpine or Termux
    if [ "$IS_TERMUX" = true ] || is_musl; then
        build_dummy_sqlite_vec "$TYRES_DIR"
    fi

    # Determine extras based on musl libc compatibility (onnxruntime does not support musl)
    if is_musl; then
        echo "[*] musl-based C library detected (e.g. Alpine). Targeting federaide[ide,vision,pdf] (omitting audio/onnxruntime)..."
        TARGET_EXTRAS="federaide[ide,vision,pdf]"
    else
        TARGET_EXTRAS="federaide[all]"
    fi

    if [ "$IS_TERMUX" = true ]; then
        echo "[*] Configuring Termux installation..."
        unset UV_FIND_LINKS

        export ANDROID_API_LEVEL=19
        echo "[*] Installing FEDERaiDE on Python 3.13..."
        uv tool install --force --refresh --python 3.13 \
            --find-links "$TYRES_DIR" \
            --find-links "https://geoarkadeep.github.io/Tyres/" \
            --with pycryptodome \
            --with tree-sitter \
            --with keyrings.alt \
            --with weasyprint \
            "federaide" || \
        uv tool install --force --refresh --python 3.13 federaide

        grep -qF ".local/bin" ~/.bashrc 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
        unset UV_FIND_LINKS

    elif [ "$OS_NAME" = "Linux" ] && [ "$IS_ARM" = true ]; then
        echo "[*] Configuring Linux ARM (aarch64) installation..."
        WHEELS="numpy-2.4.4-cp312-cp312-manylinux2014_aarch64.whl"

        DOWNLOAD_SUCCESS=false
        for wheel in $WHEELS; do
            echo "    [*] Downloading binary cache: $wheel"
            if download_file "$RAW_URL/$wheel" "$TYRES_DIR/$wheel"; then
                DOWNLOAD_SUCCESS=true
            fi
        done

        if [ "$DOWNLOAD_SUCCESS" = true ]; then
            echo "[*] Installing $TARGET_EXTRAS on Python 3.13 using cached wheels..."
            uv tool install --force --refresh --python 3.13 --find-links "$TYRES_DIR" "$TARGET_EXTRAS"
        else
            echo "[*] Installing FEDERaiDE on Python 3.13..."
            uv tool install --force --refresh --python 3.13 --find-links "$TYRES_DIR" "$TARGET_EXTRAS" || \
            uv tool install --force --refresh --python 3.13 federaide
        fi

    else
        echo "[*] Resolving latest version from PyPI..."
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

        echo "[*] Installing $TARGET_EXTRAS on standardized Python 3.13 environment..."
        if [ -n "$LATEST_VER" ]; then
            echo "[*] Target version resolved: v$LATEST_VER"
            if ! uv tool install --force --refresh --python 3.13 --find-links "$TYRES_DIR" "${TARGET_EXTRAS}==$LATEST_VER"; then
                echo "[!] Explicit version install failed. Falling back to standard resolution..."
                uv tool install --force --refresh --python 3.13 --find-links "$TYRES_DIR" "$TARGET_EXTRAS"
            fi
        else
            uv tool install --force --refresh --python 3.13 --find-links "$TYRES_DIR" "$TARGET_EXTRAS"
        fi
    fi

    rm -rf "$TYRES_DIR"
}

install_federaide

# 10. Multi-Tier PATH Persistence & Universal Executable Symlinking
setup_path_and_symlinks() {
    echo "[*] Setting up universal PATH configurations and executable symlinks..."

    # A. System-wide /etc/profile.d integration
    if [ "$(id -u)" -eq 0 ] || command -v sudo >/dev/null 2>&1 || command -v doas >/dev/null 2>&1; then
        run_root mkdir -p /etc/profile.d 2>/dev/null || true
        
        TMP_PROFILE="${TMPDIR:-/tmp}/federaide_profile.sh"
        cat << 'EOF' > "$TMP_PROFILE"
# FEDERaiDE System-wide PATH configuration
case ":$PATH:" in
    *:"$HOME/.local/bin":*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
EOF
        run_root cp "$TMP_PROFILE" /etc/profile.d/federaide.sh 2>/dev/null || true
        run_root chmod 644 /etc/profile.d/federaide.sh 2>/dev/null || true
        rm -f "$TMP_PROFILE"
    fi

    # B. User-level shell configuration files
    for rc_file in \
        "$HOME/.profile" \
        "$HOME/.bashrc" \
        "$HOME/.zshrc" \
        "$HOME/.bash_profile" \
        "$HOME/.ashrc" \
        "$HOME/.shellrc"
    do
        if [ -f "$rc_file" ] || [ "${rc_file##*/}" = ".profile" ] || [ "${rc_file##*/}" = ".bashrc" ]; then
            touch "$rc_file" 2>/dev/null || true
            if ! grep -q '\.local/bin' "$rc_file" 2>/dev/null; then
                echo '' >> "$rc_file"
                echo '# FEDERaiDE binary path' >> "$rc_file"
                echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc_file"
            fi
        fi
    done

    # Fish shell configuration
    if [ -d "$HOME/.config/fish" ]; then
        fish_config="$HOME/.config/fish/config.fish"
        touch "$fish_config" 2>/dev/null || true
        if ! grep -q '\.local/bin' "$fish_config" 2>/dev/null; then
            echo '' >> "$fish_config"
            echo '# FEDERaiDE binary path' >> "$fish_config"
            echo 'fish_add_path "$HOME/.local/bin"' >> "$fish_config"
        fi
    fi

    # C. System-wide symlinks to /usr/local/bin or /usr/bin (Guarantees immediate execution on ALL distros)
    for bin_name in "federaide" "federate"; do
        TARGET_BIN=""
        if [ -x "$HOME/.local/bin/$bin_name" ]; then
            TARGET_BIN="$HOME/.local/bin/$bin_name"
        elif [ -x "$HOME/.cargo/bin/$bin_name" ]; then
            TARGET_BIN="$HOME/.cargo/bin/$bin_name"
        else
            TARGET_BIN="$(find "$HOME/.local" "$HOME/.uv" -name "$bin_name" -type f 2>/dev/null | head -n 1)"
        fi

        if [ -n "$TARGET_BIN" ] && [ -x "$TARGET_BIN" ]; then
            echo "    [+] Creating universal symlink for $bin_name ($TARGET_BIN)..."
            if [ -d "/usr/local/bin" ]; then
                run_root ln -sf "$TARGET_BIN" "/usr/local/bin/$bin_name" 2>/dev/null || true
            elif [ -d "/usr/bin" ]; then
                run_root ln -sf "$TARGET_BIN" "/usr/bin/$bin_name" 2>/dev/null || true
            fi
        fi
    done
}

setup_path_and_symlinks

echo "======================================================================"
echo " 🎉 FEDERaiDE POSIX installation complete!"
echo "======================================================================"
echo " To launch the application:"
echo "     federaide"
echo "======================================================================"