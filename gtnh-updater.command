#!/bin/bash
# Double-click me on macOS (or run me on Linux).
cd "$(dirname "$0")" || exit 1

PYVER=3.13.14
PY=""

find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
            PY="$(command -v "$candidate")"
            return 0
        fi
    done
    return 1
}

install_python_macos() {
    local pkg="/tmp/python-$PYVER-macos11.pkg"
    echo "  Downloading the official Python installer..."
    if curl -fL --progress-bar -o "$pkg" \
        "https://www.python.org/ftp/python/$PYVER/python-$PYVER-macos11.pkg"; then
        echo "  Installing Python — macOS will ask for your Mac password."
        if sudo installer -pkg "$pkg" -target /; then
            rm -f "$pkg"
            # python.org builds ship without CA certificates; this installs them,
            # otherwise every HTTPS download fails to verify.
            for certs in /Applications/Python\ 3.*/Install\ Certificates.command; do
                [ -f "$certs" ] && /bin/bash "$certs" >/dev/null 2>&1
            done
            return 0
        fi
    fi
    rm -f "$pkg"
    echo "  Trying Apple's developer tools instead — click Install in the window that opens,"
    echo "  then run this file again."
    xcode-select --install 2>/dev/null
    return 1
}

install_python_linux() {
    echo "  Installing Python — your sudo password may be asked for."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3 python3-tk
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-tkinter
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --noconfirm python tk
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y python3 python3-tk
    else
        echo "  Unknown Linux distribution — install python3 and python3-tk with your package manager."
        return 1
    fi
}

if ! find_python; then
    echo
    echo "  GTNH Updater needs Python 3, and it isn't installed."
    echo
    read -r -p "  Install it now? [Y/n] " answer
    case "$answer" in
        [Nn]*) answer=no ;;
        *)     answer=yes ;;
    esac
    if [ "$answer" = yes ]; then
        echo
        if [ "$(uname)" = "Darwin" ]; then
            install_python_macos
        else
            install_python_linux
        fi
        hash -r 2>/dev/null
    fi
    if ! find_python; then
        echo
        echo "  Still no Python 3. Install it from https://www.python.org/downloads/"
        echo "  and run this file again."
        echo
        read -r -p "Press Enter to close "
        exit 1
    fi
fi

echo
"$PY" ./gtnh-prism-update.py --setup "$@"
STATUS=$?
echo
read -r -p "Press Enter to close "
exit $STATUS
