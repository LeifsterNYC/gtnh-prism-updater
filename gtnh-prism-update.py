#!/usr/bin/env python3
"""
GTNH updater for Prism Launcher / MultiMC — Windows, macOS and Linux.

Asks the server which GregTech: New Horizons version it runs (or falls back to
the latest non-nightly release), backs up your existing instance, and migrates
your settings, saves and other user data onto that version.

Stdlib only. Python 3.8+.

    python3 gtnh-prism-update.py --setup         # install/update + check at launch
    python3 gtnh-prism-update.py --check         # compare against the server
    python3 gtnh-prism-update.py --dry-run       # show exactly what it would do
    python3 gtnh-prism-update.py                 # back up + migrate

--setup also registers this script as Prism's pre-launch command, so pressing
Play pings the server and offers a one-click update when versions differ.

Default mode is "new": the new version is installed as a *separate* instance
and your user data is copied into it, leaving the old instance untouched
(this is the method the GTNH wiki recommends). Use --mode in-place to update
the existing instance directory instead.
"""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# The server everyone plays on. Its MOTD carries the pack version, so a plain
# server-list ping tells us which version a client needs. Override with
# --server or the GTNH_SERVER environment variable.
SERVER_ADDRESS = "10.242.74.230:25565"   # ZeroTier address of hermes

REPO_API = "https://api.github.com/repos/GTNewHorizons/GT-New-Horizons-Modpack/releases"
DL_BASE = "https://downloads.gtnewhorizons.com/Multi_mc_downloads"
UA = "gtnh-prism-update/1.0 (+https://www.gtnewhorizons.com/)"

# Pack file name variants, most preferred first, per --java choice.
JAVA17_VARIANTS = ["Java_17-25", "Java_17-21", "Java_8"]
JAVA8_VARIANTS = ["Java_8", "Java_17-25", "Java_17-21"]

# User data carried from the old instance into the new one, per
# https://wiki.gtnewhorizons.com/wiki/Installing_and_Migrating (Method 1).
# Paths are relative to the instance's .minecraft folder.
CARRY_OVER = [
    "backups",                        # single-player world backups
    "config/vendingmachine/favourites",
    "config/shaders.properties",
    "ESM",                            # sound muffler settings
    "journeymap",                     # map data + waypoints
    "resourcepacks",
    "saves",                          # worlds + NEI data
    "schematics",
    "screenshots",
    "shaderpacks",
    "TCNodeTracker",                  # JourneyMap node data
    "visualprospecting",              # JourneyMap ore vein data
    "BotaniaVars.dat",
    "localconfig.cfg",
    "options.txt",
    "optionsnf.txt",
    "optionsshaders.txt",
    "servers.dat",
    "servers.dat_old",
]

# Directories the pack ships that must be merged into (never wipe) on an
# in-place update, because the user's own data lives alongside the pack's.
MERGE_ONLY = {"journeymap", "resourcepacks"}

# Files inside .minecraft/config that survive a config replacement.
CONFIG_KEEP = ["vendingmachine/favourites", "shaders.properties"]

# Backed up in addition to CARRY_OVER: replaced by the update, but cheap to
# keep and full of settings people tweak by hand.
BACKUP_EXTRA = ["config", "serverutilities"]

# instance.cfg keys carried from the old instance to the new one. Deliberately
# excludes JvmArgs, which are Java-version specific and a common way to break
# a fresh instance; use --keep-instance-cfg to copy the whole file instead.
CFG_CARRY = [
    "JavaPath", "JavaVersion", "JavaArchitecture", "JavaRealArchitecture",
    "JavaTimestamp", "JavaSignature", "OverrideJavaLocation",
    "OverrideMemory", "MinMemAlloc", "MaxMemAlloc", "PermGen",
    "OverrideWindow", "LaunchMaximized", "MinecraftWinantWidth",
    "MinecraftWinWidth", "MinecraftWinHeight",
    "OverrideCommands", "PreLaunchCommand", "PostExitCommand", "WrapperCommand",
    "OverrideConsole", "ShowConsole", "ShowConsoleOnError", "AutoCloseConsole",
    "OverrideNativeWorkarounds", "UseNativeGLFW", "UseNativeOpenAL",
    "OverrideMCLaunchMethod", "MCLaunchMethod",
    "JoinServerOnLaunch", "JoinServerOnLaunchAddress",
    "notes",
]

IS_WIN = os.name == "nt"
__version__ = "1.3.0"
SELF_RELEASE_API = "https://api.github.com/repos/LeifsterNYC/gtnh-prism-updater/releases/latest"


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

def _ansi_supported():
    """The old Windows console prints escape codes literally unless asked not to."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if not IS_WIN:
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


ANSI = _ansi_supported()


def _color(code, text):
    return text if not ANSI else "\033[%sm%s\033[0m" % (code, text)


def log(msg):
    print("%s %s" % (_color("1;36", "[gtnh]"), msg), flush=True)


def warn(msg):
    print("%s %s" % (_color("1;33", "[gtnh] warning:"), msg), flush=True)


def die(msg):
    print("%s %s" % (_color("1;31", "[gtnh] error:"), msg), file=sys.stderr, flush=True)
    sys.exit(1)


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def confirm(question, assume_yes):
    if assume_yes:
        return True
    try:
        return input("%s %s [y/N] " % (_color("1;36", "[gtnh]"), question)).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ask_yes_no(title, message, assume_yes=False):
    """Yes/no question — a real dialog box when one is possible, else the terminal.

    Prism runs pre-launch commands without a usable stdin, so the dialog is
    what makes the launch-time check work at all.
    """
    if assume_yes:
        return True
    print(message, flush=True)
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        answer = messagebox.askyesno(title, message)
        root.destroy()
        return bool(answer)
    except Exception:
        return confirm("Continue?", False)


def show_message(title, message):
    print(message, flush=True)
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        pass


# --------------------------------------------------------------------------
# server probe (Minecraft server-list ping)
# --------------------------------------------------------------------------

def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock):
    n = shift = 0
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("server closed the connection")
        b = chunk[0]
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n


def _flatten_description(desc):
    if isinstance(desc, str):
        return desc
    if isinstance(desc, dict):
        text = desc.get("text", "")
        for extra in desc.get("extra", []) or []:
            text += _flatten_description(extra)
        return text
    if isinstance(desc, list):
        return "".join(_flatten_description(d) for d in desc)
    return ""


def probe_server(address, timeout=8):
    """Ping a Minecraft server and return its status dict, or None."""
    host, _, port_s = address.partition(":")
    port = int(port_s) if port_s else 25565
    try:
        with socket.create_connection((host, port), timeout) as sock:
            sock.settimeout(timeout)
            handshake = (b"\x00" + _varint(5) + _varint(len(host.encode()))
                         + host.encode() + struct.pack(">H", port) + _varint(1))
            sock.sendall(_varint(len(handshake)) + handshake)
            sock.sendall(_varint(1) + b"\x00")
            _read_varint(sock)          # packet length
            _read_varint(sock)          # packet id
            length = _read_varint(sock)
            buf = b""
            while len(buf) < length:
                chunk = sock.recv(min(8192, length - len(buf)))
                if not chunk:
                    break
                buf += chunk
        status = json.loads(buf.decode("utf-8", "replace"))
    except (OSError, ValueError, ConnectionError) as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    if not isinstance(status, dict):
        # A booting or restarting server answers with a bare string, e.g.
        # "Server is still starting! Please wait before reconnecting."
        return {"error": "server said: %s" % str(status)[:120]}
    motd = _flatten_description(status.get("description"))
    m = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-_](?:beta|rc|alpha|pre)[-_]?\d*)?", motd, re.I)
    return {"motd": motd.strip(),
            "version": m.group(0) if m else None,
            "players": status.get("players", {}),
            "mods": len((status.get("modinfo") or {}).get("modList") or [])}


def same_version(a, b):
    norm = lambda v: re.sub(r"[^a-z0-9]", "", (v or "").lower())
    return bool(a) and bool(b) and norm(a) == norm(b)


# --------------------------------------------------------------------------
# filesystem helpers (Windows-safe)
# --------------------------------------------------------------------------

def _on_rm_error(func, path, _exc):
    """Clear the read-only bit Windows likes to leave on extracted files."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise


def rmtree(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(long_path(path), onerror=_on_rm_error)
    elif path.exists() or path.is_symlink():
        try:
            os.chmod(str(path), stat.S_IWRITE)
        except OSError:
            pass
        path.unlink()


def copy_any(src: Path, dst: Path):
    """Copy a file or a whole tree, merging into dst if it already exists."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(long_path(src), long_path(dst), dirs_exist_ok=True, symlinks=True)
    else:
        shutil.copy2(long_path(src), long_path(dst))


def tree_size(path: Path):
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(str(path)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def long_path(p: Path):
    """GTNH config paths get deep; opt into Windows extended-length paths."""
    if not IS_WIN:
        return str(p)
    s = os.path.abspath(str(p))
    return s if s.startswith("\\\\?\\") else "\\\\?\\" + s


# --------------------------------------------------------------------------
# version discovery
# --------------------------------------------------------------------------

def curl(args, timeout=180, fail=True):
    """Run curl, or return None if it isn't usable.

    Python installed from python.org on macOS has no CA certificates until you
    run its "Install Certificates.command", so every HTTPS request fails
    verification. curl ships with the system and its own trust store, so it is
    the fallback whenever urllib cannot make a connection.
    """
    try:
        return subprocess.run(["curl", "-sS", "-L", "--connect-timeout", "20", "-A", UA]
                              + (["--fail"] if fail else []) + args,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception:
        result = curl(["-H", "Accept: application/vnd.github+json", url], timeout=90)
        if result is None or result.returncode != 0:
            raise
        return json.loads(result.stdout.decode("utf-8", "replace"))


def version_tuple(text):
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:3])


def self_update(argv):
    """Replace this script with the newest release, then re-run it.

    Runs before anything else so a fix never waits for someone to re-download
    the zip by hand. Any failure is ignored: an updater that cannot update
    itself must still update Minecraft.
    """
    if os.environ.get("GTNH_SELF_UPDATED"):
        return
    script = Path(__file__).resolve()
    try:
        latest = http_json(SELF_RELEASE_API)
    except Exception:
        return                                    # offline, rate-limited, whatever
    tag = latest.get("tag_name") or ""
    if not version_tuple(tag) or version_tuple(tag) <= version_tuple(__version__):
        return
    asset = next((a for a in latest.get("assets", []) if a["name"].endswith(".zip")), None)
    if not asset:
        return
    log("updating the updater itself: %s -> %s" % (__version__, tag))
    try:
        staging = Path(tempfile.mkdtemp(prefix="gtnh-selfupdate-"))
        archive = staging / "release.zip"
        size, _ = remote_size(asset["browser_download_url"])
        download(asset["browser_download_url"], archive, size or -1, attempts=2)
        with zipfile.ZipFile(long_path(archive)) as zf:
            names = zf.namelist()
            if "gtnh-prism-update.py" not in names:
                raise ValueError("release zip has no gtnh-prism-update.py")
            zf.extractall(long_path(staging))
        new_script = staging / "gtnh-prism-update.py"
        compile(new_script.read_text(encoding="utf-8"), str(new_script), "exec")
        for name in names:
            source = staging / name
            if not source.is_file():
                continue
            if name == "gtnh-prism-update.py" or (script.parent / name).exists():
                shutil.copy2(str(source), str(script.parent / name))
        rmtree(staging)
    except Exception as e:
        warn("could not update the updater (%s) — carrying on with %s" % (e, __version__))
        return
    env = dict(os.environ, GTNH_SELF_UPDATED="1")
    result = subprocess.run([sys.executable, str(script)] + list(argv[1:]), env=env)
    sys.exit(result.returncode)


def list_versions(limit_pages=3):
    """Every published non-nightly release, newest first."""
    out = []
    for page in range(1, limit_pages + 1):
        try:
            data = http_json("%s?per_page=100&page=%d" % (REPO_API, page))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if out:
                warn("stopped querying GitHub after page %d: %s" % (page - 1, e))
                break
            die("could not reach the GitHub releases API (%s).\n"
                "       Pass an explicit --version or --url instead." % e)
        if not data:
            break
        for rel in data:
            tag = rel.get("tag_name") or ""
            if rel.get("draft") or "nightly" in tag.lower():
                continue
            if not re.match(r"^\d+\.\d+", tag):
                continue
            out.append({"version": tag,
                        "published": (rel.get("published_at") or "")[:10],
                        "prerelease": bool(rel.get("prerelease")) or bool(
                            re.search(r"(beta|rc|alpha|pre)", tag, re.I))})
    # GitHub returns newest first; keep that order but drop duplicates.
    seen, uniq = set(), []
    for v in out:
        if v["version"] not in seen:
            seen.add(v["version"])
            uniq.append(v)
    return uniq


def pack_url_candidates(version, java_pref):
    # Pre-releases live under betas/, stable releases at the top level; try
    # the likely one first but fall back to the other.
    subs = ["betas/", ""] if re.search(r"(beta|rc|alpha|pre)", version, re.I) else ["", "betas/"]
    variants = JAVA8_VARIANTS if java_pref == "8" else JAVA17_VARIANTS
    return ["%s/%sGT_New_Horizons_%s_%s.zip" % (DL_BASE, sub, version, v)
            for sub in subs for v in variants]


def remote_size(url, attempts=2):
    """Return (size, "") if the URL is downloadable, else (None, why).

    Retries once, and falls back to a plain request in case something between
    here and the CDN dislikes Range headers — a network problem must not be
    reported as "that version does not exist".
    """
    detail = "unknown error"
    for attempt in range(attempts):
        for headers in ({"User-Agent": UA, "Range": "bytes=0-0"}, {"User-Agent": UA}):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=45) as r:
                    content_range = r.headers.get("Content-Range")
                    if content_range and "/" in content_range:
                        return int(content_range.rsplit("/", 1)[1]), ""
                    length = r.headers.get("Content-Length")
                    return (int(length) if length else -1), ""
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None, "404 (no such file on the download server)"
                detail = "HTTP %s" % e.code
            except Exception as e:                      # TLS, DNS, proxy, timeout
                detail = "%s: %s" % (type(e).__name__, e)
        if attempt + 1 < attempts:
            time.sleep(2)

    result = curl(["-r", "0-0", "-D", "-", "-o", os.devnull, "-w", "\n%{http_code}", url],
                  timeout=90, fail=False)
    if result is None or result.returncode != 0:
        if result is not None and result.stderr:
            detail = "%s (curl: %s)" % (detail, result.stderr.decode("utf-8", "replace").strip()[:120])
        return None, detail
    body = result.stdout.decode("utf-8", "replace")
    code = body.rsplit("\n", 1)[-1].strip()
    if code == "404":
        return None, "404 (no such file on the download server)"
    if code not in ("200", "206"):
        return None, "HTTP %s" % code
    ranges = re.findall(r"content-range:\s*bytes\s*\d+-\d+/(\d+)", body, re.I)
    if ranges:
        return int(ranges[-1]), ""
    lengths = re.findall(r"content-length:\s*(\d+)", body, re.I)
    return (int(lengths[-1]) if lengths else -1), ""


def resolve_pack(version, java_pref):
    """Return (url, size) for a version, or None if no pack was published."""
    problems = []
    for url in pack_url_candidates(version, java_pref):
        size, why = remote_size(url)
        if size is not None:
            return url, size
        problems.append((url, why))
    resolve_pack.problems = problems
    return None


def explain_resolve_failure(version, problems):
    """Say whether the version is missing or the download server is unreachable."""
    reachable = [p for p in problems if not p[1].startswith("404")]
    if reachable:
        return ("could not reach the GTNH download server.\n"
                "       %s\n"
                "       Check your internet connection, VPN or proxy, then try again.\n"
                "       You can also download the pack in a browser and pass it with --file:\n"
                "         %s"
                % (reachable[0][1], problems[0][0]))
    return ("GTNH %s has no client pack on the download server.\n"
            "       Run --list to see the versions that do, or pass --url/--file yourself.\n"
            "       Tried:\n         %s"
            % (version, "\n         ".join(url for url, _ in problems)))


def resolve_latest(java_pref, versions, limit=10):
    """Newest version that actually has a client pack (some tags never get one)."""
    for cand in versions[:limit]:
        found = resolve_pack(cand["version"], java_pref)
        if found:
            log("latest non-nightly release with a client pack: %s (%s, %s)" %
                (cand["version"], cand["published"],
                 "pre-release" if cand["prerelease"] else "stable"))
            return cand["version"], found[0], found[1]
        problems = getattr(resolve_pack, "problems", [])
        if any(not why.startswith("404") for _, why in problems):
            die(explain_resolve_failure(cand["version"], problems))
        log("%s has no client pack published — looking further back" % cand["version"])
    die("none of the %d newest releases has a downloadable client pack" % min(limit, len(versions)))


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------

def download_with_curl(url, part: Path, expected_size):
    """Last-resort download for machines where Python's TLS is unusable."""
    try:
        result = subprocess.run(["curl", "-fL", "--retry", "3", "-C", "-", "-A", UA,
                                 "--progress-bar", "-o", str(part), url])
    except (OSError, subprocess.SubprocessError) as e:
        warn("curl could not run either (%s)" % e)
        return False
    if result.returncode != 0:
        return False
    return not (expected_size > 0 and part.stat().st_size != expected_size)


def download(url, dest: Path, expected_size, attempts=4):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and expected_size > 0 and dest.stat().st_size == expected_size:
        log("using cached download %s" % dest.name)
        return dest

    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        if expected_size > 0 and have > expected_size:
            part.unlink()
            have = 0
        if expected_size > 0 and have == expected_size:
            break
        headers = {"User-Agent": UA}
        if have:
            headers["Range"] = "bytes=%d-" % have
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                mode = "ab" if have and r.status == 206 else "wb"
                if mode == "wb":
                    have = 0
                last = time.monotonic()
                with open(str(part), mode) as fh:
                    while True:
                        chunk = r.read(1024 * 512)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        if time.monotonic() - last > 2:
                            last = time.monotonic()
                            pct = ("%5.1f%% " % (100.0 * have / expected_size)) if expected_size > 0 else ""
                            print("\r    downloading %s%s of %s" %
                                  (pct, human(have), human(expected_size) if expected_size > 0 else "?"),
                                  end="", flush=True)
            print("\r    downloaded %s%s" % (human(have), " " * 30), flush=True)
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            print("", flush=True)
            if attempt == attempts:
                warn("download failed in Python (%s) — retrying with curl" % e)
                if not download_with_curl(url, part, expected_size):
                    die("download failed after %d attempts: %s" % (attempts, e))
                break
            warn("download interrupted (%s) — retrying (%d/%d)" % (e, attempt + 1, attempts))
            time.sleep(3 * attempt)

    if expected_size > 0 and part.stat().st_size != expected_size:
        die("download size mismatch: got %d bytes, expected %d" % (part.stat().st_size, expected_size))
    if dest.exists():
        dest.unlink()
    part.replace(dest)
    return dest


def pack_mod_names(zip_path: Path):
    zf, root = open_pack(zip_path)
    with zf:
        prefix = "%s/.minecraft/mods/" % root
        return [n.rsplit("/", 1)[-1] for n in zf.namelist()
                if n.startswith(prefix) and not n.endswith("/")]


def open_pack(zip_path: Path):
    """Open the pack zip, sanity-check it, and return (zipfile, root prefix)."""
    try:
        zf = zipfile.ZipFile(long_path(zip_path))
    except zipfile.BadZipFile as e:
        die("downloaded pack is not a valid zip (%s). Delete it and re-run." % e)
    names = zf.namelist()
    roots = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(roots) != 1:
        die("unexpected pack layout: %d top-level entries (%s)" % (len(roots), sorted(roots)[:5]))
    root = roots.pop()
    for required in ("%s/mmc-pack.json" % root, "%s/.minecraft/" % root):
        if not any(n == required or n.startswith(required) for n in names):
            die("pack is missing %s — is this really a Prism/MultiMC pack?" % required)
    return zf, root


def extract_pack(zip_path: Path, target: Path):
    zf, root = open_pack(zip_path)
    with zf:
        staging = target.parent / (".gtnh-extract-%d" % os.getpid())
        rmtree(staging)
        staging.mkdir(parents=True)
        members = zf.infolist()
        total = len(members)
        for i, m in enumerate(members, 1):
            zf.extract(m, long_path(staging))
            if i % 500 == 0 or i == total:
                print("\r    extracting %d/%d files" % (i, total), end="", flush=True)
        print("", flush=True)
        src = staging / root
        target.parent.mkdir(parents=True, exist_ok=True)
        rmtree(target)
        shutil.move(str(src), str(target))
        rmtree(staging)
    return target


# --------------------------------------------------------------------------
# instance discovery
# --------------------------------------------------------------------------

def candidate_instance_dirs():
    home = Path.home()
    sysname = platform.system()
    paths = []
    if sysname == "Windows":
        appdata = os.environ.get("APPDATA")
        localapp = os.environ.get("LOCALAPPDATA")
        if appdata:
            paths += [Path(appdata) / "PrismLauncher" / "instances",
                      Path(appdata) / "MultiMC" / "instances"]
        if localapp:
            paths += [Path(localapp) / "Programs" / "PrismLauncher" / "instances",
                      Path(localapp) / "PrismLauncher" / "instances"]
        paths += [home / "AppData" / "Roaming" / "PrismLauncher" / "instances",
                  home / "PrismLauncher" / "instances",
                  Path("C:/PrismLauncher/instances"),
                  Path("C:/MultiMC/instances")]
    elif sysname == "Darwin":
        sup = home / "Library" / "Application Support"
        paths += [sup / "PrismLauncher" / "instances",
                  sup / "MultiMC" / "instances",
                  home / "PrismLauncher" / "instances"]
    else:
        paths += [home / ".local" / "share" / "PrismLauncher" / "instances",
                  home / ".var" / "app" / "org.prismlauncher.PrismLauncher" / "data"
                  / "PrismLauncher" / "instances",
                  home / "snap" / "prismlauncher" / "current" / ".local" / "share"
                  / "PrismLauncher" / "instances",
                  home / ".local" / "share" / "multimc" / "instances",
                  home / ".local" / "share" / "MultiMC" / "instances",
                  home / "PrismLauncher" / "instances",
                  home / "MultiMC" / "instances"]
    seen, out = set(), []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        if p.is_dir():
            out.append(p)
    return out


def mc_dir(instance: Path):
    for name in (".minecraft", "minecraft"):
        d = instance / name
        if d.is_dir():
            return d
    return None


def mod_key(filename):
    """Rough 'same mod, different version' key for a jar file name."""
    base = re.sub(r"\.(jar|litemod)(\.disabled)?$", "", filename, flags=re.I)
    base = re.split(r"[-_ ]v?\d", base)[0]
    return re.sub(r"[^a-z0-9]", "", base.lower())


def extra_mods(old_mods: Path, pack_names):
    """Mods present in the old instance that the new pack does not ship."""
    if not old_mods.is_dir():
        return []
    pack_keys = {mod_key(n) for n in pack_names}
    return sorted(p for p in old_mods.iterdir()
                  if p.is_file() and mod_key(p.name) not in pack_keys)


def instance_version(instance: Path):
    """Best-effort read of the GTNH version an instance is currently on."""
    stamp = instance / ".gtnh-version"
    if stamp.is_file():
        v = stamp.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if v and v[0]:
            return v[0]
    mc = mc_dir(instance)
    if mc:
        newest, newest_mtime = None, -1
        for f in mc.glob("changelog from * to *.md"):
            m = re.search(r"to (.+)\.md$", f.name)
            if m and f.stat().st_mtime > newest_mtime:
                newest, newest_mtime = m.group(1), f.stat().st_mtime
        if newest:
            return newest
    m = re.search(r"(\d+\.\d+\.\d+[\w.\-]*)", instance.name)
    return m.group(1) if m else "unknown"


def is_gtnh_instance(instance: Path):
    mc = mc_dir(instance)
    if not mc:
        return False
    if re.search(r"(gtnh|new.?horizons)", instance.name, re.I):
        return True
    cfg = instance / "instance.cfg"
    if cfg.is_file() and re.search(r"(gtnh|new horizons)",
                                   cfg.read_text(encoding="utf-8", errors="replace"), re.I):
        return True
    mods = mc / "mods"
    if mods.is_dir():
        for f in mods.iterdir():
            if re.match(r"(gtnhlib|GT5-Unofficial|gregtech)", f.name, re.I):
                return True
    return False


def find_instances():
    found = []
    for root in candidate_instance_dirs():
        for child in sorted(root.iterdir()):
            if child.is_dir() and is_gtnh_instance(child):
                found.append(child)
    return found


def default_instances_root():
    for root in candidate_instance_dirs():
        return root
    die("could not find a Prism Launcher instances folder.\n"
        "       Install Prism Launcher first, or pass --instances-dir /path/to/instances.")


def pick_instance(explicit, assume_yes, allow_none=False):
    if explicit:
        inst = Path(explicit).expanduser().resolve()
        if not mc_dir(inst):
            die("%s does not look like a Prism instance (no .minecraft folder)" % inst)
        return inst
    found = find_instances()
    if not found:
        if allow_none:
            return None
        die("no GTNH instance found automatically. Pass --instance /path/to/instance\n"
            "       (the folder that contains .minecraft, instance.cfg and mmc-pack.json).")
    if len(found) == 1:
        return found[0]
    print("Multiple GTNH instances found:")
    for i, inst in enumerate(found, 1):
        print("  %2d) %-40s  (version %s)" % (i, inst.name, instance_version(inst)))
    if assume_yes:
        die("several instances match — re-run with --instance to say which one.")
    try:
        choice = input("Which instance? [1-%d] " % len(found)).strip()
        return found[int(choice) - 1]
    except (ValueError, IndexError, EOFError):
        die("no valid instance selected")


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------

def backup_paths(instance: Path, mode):
    """Return the list of (absolute path, arcname) pairs to archive."""
    mc = mc_dir(instance)
    items = []
    if mode == "full":
        for child in sorted(instance.iterdir()):
            items.append((child, child.name))
        return items
    for name in ("instance.cfg", "mmc-pack.json", ".gtnh-version", "instance.png",
                 "gtnh_icon.png", "icon.png"):
        p = instance / name
        if p.exists():
            items.append((p, name))
    wanted = CARRY_OVER + BACKUP_EXTRA
    # Drop entries already covered by a parent that is being archived whole.
    tops = {r for r in wanted if "/" not in r}
    for rel in wanted:
        if "/" in rel and rel.split("/")[0] in tops:
            continue
        p = mc / rel
        if p.exists():
            items.append((p, "%s/%s" % (mc.name, rel)))
    return items


def make_backup(instance: Path, backup_dir: Path, mode, version, dry_run, assume_yes):
    items = backup_paths(instance, mode)
    if not items:
        warn("nothing to back up in %s" % instance)
        return None
    total = sum(tree_size(p) for p, _ in items)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / ("%s_%s_%s.zip" % (re.sub(r"[^\w.\-]+", "_", instance.name), version, stamp))

    log("backup (%s): %s of data -> %s" % (mode, human(total), dest))
    for p, arc in items:
        print("      + %s (%s)" % (arc, human(tree_size(p))))
    if dry_run:
        return dest

    free = shutil.disk_usage(str(backup_dir)).free
    if free < total * 0.6:
        warn("only %s free on the backup volume for %s of data" % (human(free), human(total)))
        if not confirm("continue anyway?", assume_yes):
            die("aborted before backup")

    done = [0]
    t0 = time.monotonic()

    def add(zf, path: Path, arc):
        if path.is_dir():
            for root, _dirs, files in os.walk(str(path)):
                rel_root = os.path.relpath(root, str(path))
                for f in files:
                    src = Path(root) / f
                    name = arc if rel_root == "." else "%s/%s" % (arc, rel_root.replace(os.sep, "/"))
                    try:
                        zf.write(long_path(src), "%s/%s" % (name, f))
                        done[0] += src.stat().st_size
                    except OSError as e:
                        warn("skipped %s (%s)" % (src, e))
                    if time.monotonic() - t0 > 1 and total:
                        print("\r    archiving %5.1f%%  (%s)" %
                              (100.0 * done[0] / total, human(done[0])), end="", flush=True)
        else:
            zf.write(long_path(path), arc)
            done[0] += path.stat().st_size

    try:
        kwargs = {"compresslevel": 1} if sys.version_info >= (3, 7) else {}
        with zipfile.ZipFile(long_path(dest), "w", zipfile.ZIP_DEFLATED,
                             allowZip64=True, **kwargs) as zf:
            for p, arc in items:
                add(zf, p, arc)
    except KeyboardInterrupt:
        rmtree(dest)
        die("backup interrupted — no changes were made")
    print("\r    archived %s%s" % (human(done[0]), " " * 30), flush=True)
    log("backup written: %s (%s on disk)" % (dest, human(dest.stat().st_size)))
    return dest


def find_backups(instance: Path, backup_dir: Path):
    """Backup zips for an instance, newest first."""
    if not backup_dir.is_dir():
        return []
    stem = re.sub(r"[^\w.\-]+", "_", instance.name)
    zips = [p for p in backup_dir.glob("*.zip") if p.name.startswith(stem + "_")]
    return sorted(zips, key=lambda p: p.stat().st_mtime, reverse=True)


def run_restore(args):
    """Put files from a backup zip back into an instance."""
    inst = pick_instance(args.instance, args.yes)
    backup_dir = (Path(args.backup_dir).expanduser() if args.backup_dir
                  else inst.parent.parent / "GTNH-Backups")
    if args.restore not in ("", "auto"):
        archive = Path(args.restore).expanduser()
    else:
        found = find_backups(inst, backup_dir)
        if not found:
            die("no backups for %s in %s\n"
                "       Pass the zip explicitly: --restore /path/to/backup.zip" % (inst.name, backup_dir))
        if len(found) > 1 and not args.yes:
            print("Backups for %s:" % inst.name)
            for i, p in enumerate(found, 1):
                print("  %2d) %-52s %s" % (i, p.name, human(p.stat().st_size)))
            try:
                archive = found[int(input("Which one? [1-%d] " % len(found)).strip()) - 1]
            except (ValueError, IndexError, EOFError):
                die("no valid backup selected")
        else:
            archive = found[0]
    if not archive.is_file():
        die("no such backup: %s" % archive)

    wanted = [p.strip() for p in args.only.split(",") if p.strip()] if args.only else []
    mc = mc_dir(inst)
    with zipfile.ZipFile(long_path(archive)) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if wanted:
            members = [m for m in members
                       if any(m.filename == w or m.filename.startswith(w.rstrip("/") + "/") or
                              m.filename.startswith("%s/%s" % (mc.name, w.rstrip("/")))
                              for w in wanted)]
        if not members:
            die("nothing matching %s in %s" % (args.only, archive.name))
        total = sum(m.file_size for m in members)
        tops = sorted({m.filename.split("/")[0] + ("/" + m.filename.split("/")[1]
                                                   if m.filename.startswith(mc.name + "/") else "")
                       for m in members})
        log("restoring %d files (%s) from %s" % (len(members), human(total), archive.name))
        for t in tops:
            print("      + %s" % t)
        log("into %s" % inst)
        if args.dry_run:
            log("dry run — nothing was written.")
            return 0
        if not confirm("Close Prism first. Overwrite these files in the instance?", args.yes):
            log("aborted, nothing changed")
            return 1
        for m in members:
            zf.extract(m, long_path(inst))
    log("restored. Launch the instance and check your waypoints before playing.")
    return 0


# --------------------------------------------------------------------------
# instance.cfg merge
# --------------------------------------------------------------------------

def read_cfg(path: Path):
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_cfg(path: Path, data):
    lines = ["[General]"] if "[General]" not in data else []
    body = "\n".join("%s=%s" % (k, v) for k, v in data.items() if k != "[General]")
    path.write_text("\n".join(lines + [body]) + "\n", encoding="utf-8")


def merge_instance_cfg(old_instance: Path, new_instance: Path, new_name, keep_whole, dry_run):
    old_cfg_path, new_cfg_path = old_instance / "instance.cfg", new_instance / "instance.cfg"
    old_cfg = read_cfg(old_cfg_path)
    if not old_cfg:
        return []
    if keep_whole:
        if not dry_run:
            shutil.copy2(str(old_cfg_path), str(new_cfg_path))
            cfg = read_cfg(new_cfg_path)
            cfg["name"] = new_name
            write_cfg(new_cfg_path, cfg)
        return ["<entire instance.cfg>"]
    new_cfg = read_cfg(new_cfg_path)
    carried = []
    for key in CFG_CARRY:
        if key in old_cfg:
            new_cfg[key] = old_cfg[key]
            carried.append("%s=%s" % (key, old_cfg[key]))
    new_cfg["name"] = new_name
    if not dry_run:
        write_cfg(new_cfg_path, new_cfg)
    return carried


# --------------------------------------------------------------------------
# migration
# --------------------------------------------------------------------------

def report_extra_mods(extras, dest_mods: Path, keep_extra, dry_run, do_copy=True):
    if not extras:
        return
    if keep_extra:
        log("carrying %d extra mod(s) forward (--keep-extra-mods):" % len(extras))
    else:
        log("%d mod(s) you added are not part of the new pack:" % len(extras))
    for p in extras:
        print("      %s %s" % ("+" if keep_extra else "-", p.name))
    if keep_extra:
        if do_copy and not dry_run:
            for p in extras:
                copy_any(p, dest_mods / p.name)
    else:
        print("      re-add them by hand if they still work with this version, "
              "or re-run with --keep-extra-mods")


def migrate_new_instance(old: Path, zip_path: Path, instances_root: Path, version,
                         name, keep_whole_cfg, keep_extra, dry_run, force):
    new_dir = instances_root / name
    if new_dir.exists():
        if not force:
            die("%s already exists. Use --force to replace it, or --name to pick another name." % new_dir)
        log("replacing existing %s (--force)" % new_dir)

    log("installing %s -> %s" % (version, new_dir))
    if not dry_run:
        extract_pack(zip_path, new_dir)  # noqa: zip_path is never None outside dry-run
    new_mc = new_dir / ".minecraft"

    if old is None:  # first-time install, nothing to carry over
        if not dry_run:
            (new_dir / ".gtnh-version").write_text(version + "\n", encoding="utf-8")
        return new_dir

    old_mc = mc_dir(old)
    log("copying your data from %s" % old)
    copied = []
    for rel in CARRY_OVER:
        src = old_mc / rel
        if not src.exists():
            continue
        copied.append(rel)
        print("      + %s (%s)" % (rel, human(tree_size(src))))
        if not dry_run:
            copy_any(src, new_mc / rel)
    if not copied:
        warn("found no user data in %s — is that the right instance?" % old_mc)

    if zip_path is not None:
        report_extra_mods(extra_mods(old_mc / "mods", pack_mod_names(zip_path)),
                          new_mc / "mods", keep_extra, dry_run)

    carried = merge_instance_cfg(old, new_dir, name, keep_whole_cfg, dry_run)
    if carried:
        log("carried over instance settings: %s" % ", ".join(carried))

    if not dry_run:
        (new_dir / ".gtnh-version").write_text(version + "\n", encoding="utf-8")
    return new_dir


def migrate_in_place(instance: Path, zip_path: Path, version, keep_extra, dry_run):
    mc = mc_dir(instance)
    if zip_path is None:  # dry run without the pack on disk
        log("in-place update of %s -> %s" % (instance.name, version))
        print("      replace: %s/{config,mods,serverutilities}, libraries, patches, mmc-pack.json"
              % mc.name)
        print("      merge:   %s/{journeymap,resourcepacks}" % mc.name)
        print("      keep:    instance.cfg, saves and the rest of your user data")
        return instance
    extras = extra_mods(mc / "mods", pack_mod_names(zip_path))
    # Copy them somewhere safe before the mods folder is wiped.
    stash_mods = None
    if extras and keep_extra and not dry_run:
        stash_mods = Path(tempfile.mkdtemp(prefix="gtnh-mods-"))
        for p in extras:
            copy_any(p, stash_mods / p.name)
    zf, root = open_pack(zip_path)
    with zf:
        names = zf.namelist()
        mc_prefix = "%s/.minecraft/" % root
        pack_mc_entries = sorted({n[len(mc_prefix):].split("/", 1)[0]
                                  for n in names if n.startswith(mc_prefix) and n != mc_prefix})
        pack_root_entries = sorted({n[len(root) + 1:].split("/", 1)[0]
                                    for n in names
                                    if n.startswith(root + "/") and not n.startswith(mc_prefix)
                                    and n != root + "/"})
    replace_mc = [e for e in pack_mc_entries if e not in MERGE_ONLY]
    merge_mc = [e for e in pack_mc_entries if e in MERGE_ONLY]
    # instance.cfg holds the user's Java path / RAM; never overwrite it.
    replace_root = [e for e in pack_root_entries if e != "instance.cfg"]

    log("in-place update of %s -> %s" % (instance.name, version))
    print("      replace: %s" % ", ".join(["%s/%s" % (mc.name, e) for e in replace_mc] + replace_root))
    print("      merge:   %s" % ", ".join("%s/%s" % (mc.name, e) for e in merge_mc))
    print("      keep:    instance.cfg, %s" % ", ".join(
        r for r in CARRY_OVER if (mc / r).exists() and r.split("/")[0] not in replace_mc))

    if dry_run:
        report_extra_mods(extras, mc / "mods", keep_extra, dry_run)
        return instance

    staging = instance.parent / (".gtnh-new-%s-%d" % (re.sub(r"[^\w.\-]+", "_", version), os.getpid()))
    extract_pack(zip_path, staging)
    try:
        # Stash the user files that live inside the config folder we replace.
        stash = {}
        for rel in CONFIG_KEEP:
            p = mc / "config" / rel
            if p.exists():
                tmp = Path(tempfile.mkdtemp(prefix="gtnh-keep-")) / Path(rel).name
                copy_any(p, tmp)
                stash[rel] = tmp

        for entry in replace_mc:
            rmtree(mc / entry)
            copy_any(staging / ".minecraft" / entry, mc / entry)
        for entry in merge_mc:
            copy_any(staging / ".minecraft" / entry, mc / entry)
        for entry in replace_root:
            rmtree(instance / entry)
            copy_any(staging / entry, instance / entry)

        for rel, tmp in stash.items():
            copy_any(tmp, mc / "config" / rel)
            rmtree(tmp.parent)

        if extras:
            if stash_mods:
                for p in sorted(stash_mods.iterdir()):
                    copy_any(p, mc / "mods" / p.name)
            report_extra_mods(extras, mc / "mods", keep_extra, dry_run, do_copy=False)

        (instance / ".gtnh-version").write_text(version + "\n", encoding="utf-8")
    finally:
        rmtree(staging)
        if stash_mods:
            rmtree(stash_mods)
    return instance


# --------------------------------------------------------------------------
# Java runtime
# --------------------------------------------------------------------------

def pack_java_range(pack_name):
    """Java majors a pack flavour needs, read from its file name."""
    m = re.search(r"Java_(\d+)(?:-(\d+))?", str(pack_name))
    if not m:
        return None
    low = int(m.group(1))
    return low, int(m.group(2)) if m.group(2) else low


def instance_java_flavour(instance: Path):
    """'17' if an instance is set up for the Java 17+ pack, '8' if not."""
    if instance is None:
        return None
    if list((instance / "libraries").glob("lwjgl3ify*forgePatches.jar")) or \
            list((instance / "patches").glob("me.eigenraven.lwjgl3ify.*")):
        return "17"
    return "8" if (instance / "mmc-pack.json").is_file() else None


def java_major(java_exe):
    """Major version of a java binary, or None if it won't run."""
    try:
        result = subprocess.run([str(java_exe), "-version"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', result.stdout.decode("utf-8", "replace"))
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major   # "1.8.0_392" -> 8


def candidate_javas(instance: Path):
    """Every java binary we can find, Prism's own runtimes first."""
    exe = "java.exe" if IS_WIN else "java"
    globs = []
    for root in candidate_instance_dirs():          # <prism data>/instances
        globs.append(root.parent / "java" / "*" / "bin" / exe)
    if IS_WIN:
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "Amazon Corretto"):
                    globs.append(Path(base) / vendor / "*" / "bin" / exe)
    elif platform.system() == "Darwin":
        globs.append(Path("/Library/Java/JavaVirtualMachines/*/Contents/Home/bin") / exe)
        globs.append(Path.home() / "Library/Java/JavaVirtualMachines/*/Contents/Home/bin" / exe)
        globs.append(Path("/opt/homebrew/opt/openjdk*/bin") / exe)
    else:
        globs.append(Path("/usr/lib/jvm/*/bin") / exe)
        globs.append(Path.home() / ".sdkman/candidates/java/*/bin" / exe)

    found, seen = [], set()
    for pattern in globs:
        for path in sorted(Path(pattern.anchor).glob(str(pattern.relative_to(pattern.anchor)))):
            if path.is_file() and str(path) not in seen:
                seen.add(str(path))
                found.append(path)
    on_path = shutil.which("java")
    if on_path and on_path not in seen:
        found.append(Path(on_path))
    return found


def ensure_java(instance: Path, pack_name, dry_run, assume_yes):
    """Make sure the instance points at a Java the new pack can actually run."""
    wanted = pack_java_range(pack_name)
    if not wanted:
        return
    low, high = wanted
    cfg_path = instance / "instance.cfg"
    cfg = read_cfg(cfg_path)
    current = cfg.get("JavaPath", "").strip('"')

    if current:
        have = java_major(current)
        if have is None:
            warn("the Java set for this instance does not run: %s" % current)
        elif low <= have <= high:
            log("java:       %s is Java %s — right for this pack (needs %s)"
                % (current, have, "%d-%d" % (low, high) if low != high else low))
            return
        else:
            warn("this instance is set to Java %s but the pack needs Java %s"
                 % (have, "%d-%d" % (low, high) if low != high else low))
    else:
        log("java:       no per-instance Java set; Prism's default must be Java %s"
            % ("%d-%d" % (low, high) if low != high else low))

    fits = []
    for java in candidate_javas(instance):
        major = java_major(java)
        if major is not None and low <= major <= high:
            fits.append((major, java))
    if not fits:
        warn("no Java %s found on this computer — in Prism open Settings > Java and let it "
             "download one, or Edit Instance > Settings > Java" %
             ("%d-%d" % (low, high) if low != high else low))
        return
    best = sorted(fits)[-1]
    if not current and not cfg.get("OverrideJavaLocation"):
        log("java:       Java %s available at %s if Prism's default is wrong" % best)
        return
    if not dry_run and confirm("Point this instance at Java %s (%s)?" % best, assume_yes):
        cfg["OverrideJavaLocation"] = "true"
        cfg["JavaPath"] = str(best[1])
        for stale in ("JavaVersion", "JavaTimestamp", "JavaSignature",
                      "JavaArchitecture", "JavaRealArchitecture"):
            cfg.pop(stale, None)
        write_cfg(cfg_path, cfg)
        log("java:       switched the instance to Java %s (%s)" % best)


# --------------------------------------------------------------------------
# launch-time check + Prism pre-launch hook
# --------------------------------------------------------------------------

def hook_command(instance: Path, server):
    quote = lambda s: '"%s"' % s
    return " ".join([quote(sys.executable), quote(os.path.abspath(__file__)),
                     "--check", "--instance", quote(str(instance)),
                     "--server", server])


def manage_hook(args):
    inst = pick_instance(args.instance, args.yes)
    cfg_path = inst / "instance.cfg"
    cfg = read_cfg(cfg_path)
    if args.remove_hook:
        if "gtnh-prism-update" not in cfg.get("PreLaunchCommand", ""):
            log("no update check installed on %s" % inst.name)
            return 0
        cfg["PreLaunchCommand"] = ""
        write_cfg(cfg_path, cfg)
        log("removed the launch-time update check from %s" % inst.name)
        return 0
    cfg["OverrideCommands"] = "true"
    cfg["PreLaunchCommand"] = hook_command(inst, args.server)
    write_cfg(cfg_path, cfg)
    log("installed the launch-time update check on %s" % inst.name)
    print("      every time you press Play, it pings %s and offers to update" % args.server)
    print("      if the server is on a different version. Remove it with --remove-hook.")
    return 0


def run_check(args):
    """Pre-launch check. Exit 0 lets Prism launch, non-zero cancels it."""
    inst = pick_instance(args.instance, True, allow_none=True)
    if inst is None:
        warn("no GTNH instance found — skipping the version check")
        return 0
    local = instance_version(inst)
    status = probe_server(args.server)

    if status.get("error") or not status.get("version"):
        warn("could not read the server version from %s (%s) — launching anyway"
             % (args.server, status.get("error") or "no version in the MOTD"))
        return 0

    server_ver = status["version"]
    if same_version(local, server_ver):
        log("server %s is on %s — your instance matches. Have fun!" % (args.server, server_ver))
        return 0

    message = ("The server is running GTNH %s.\n"
               "Your instance '%s' is on %s.\n\n"
               "You need to update before you can join.\n\n"
               "Update now? Your saves and settings are backed up first."
               % (server_ver, inst.name, local))
    if not ask_yes_no("GTNH update needed", message, args.yes):
        warn("not updating — you can play single-player, but joining the server will fail")
        return 0

    args.check = False
    args.version = server_ver
    args.instance = str(inst)
    args.yes = True
    rc = run_update(args)
    if rc == 0:
        show_message("GTNH updated",
                     "Updated to %s.\n\nPress Play again to start the game." % server_ver)
        return 1  # cancel this launch; Prism must re-read the new pack files
    return rc


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="gtnh-prism-update",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Install or update a GTNH Prism Launcher / MultiMC instance to the "
                    "version our server runs (or the latest non-nightly release).",
        epilog="""examples:
  %(prog)s --setup                      # install/update + check on every launch
  %(prog)s --check                      # ask the server, offer to update
  %(prog)s --list
  %(prog)s --dry-run
  %(prog)s                              # match the server, as a new instance
  %(prog)s --latest                     # newest release/beta/RC instead
  %(prog)s --version 2.8.4              # pin a version
  %(prog)s --mode in-place --instance "~/.local/share/PrismLauncher/instances/GTNH"
  %(prog)s --file ~/Downloads/GT_New_Horizons_2.9.0-beta-2_Java_17-25.zip
""")
    p.add_argument("--list", action="store_true", help="list available versions and exit")
    p.add_argument("--restore", nargs="?", const="auto", metavar="ZIP",
                   help="put files back from a backup zip (newest one for the instance "
                        "by default); combine with --only to restore just part of it")
    p.add_argument("--only", metavar="PATHS",
                   help="with --restore: comma-separated paths to restore, "
                        "e.g. journeymap,visualprospecting,saves")
    p.add_argument("--check", action="store_true",
                   help="ask the server which version it runs, compare it with your instance "
                        "and offer to update (this is what the launch hook uses)")
    p.add_argument("--setup", action="store_true",
                   help="do everything: install or update to the server's version, then make "
                        "Prism check the server on every launch (what the double-click "
                        "shortcuts run)")
    p.add_argument("--install-hook", action="store_true",
                   help="make Prism run that check every time you press Play")
    p.add_argument("--remove-hook", action="store_true", help="undo --install-hook")
    p.add_argument("--server", default=os.environ.get("GTNH_SERVER", SERVER_ADDRESS),
                   help="server to ask for the required version (default: %(default)s)")
    p.add_argument("--latest", action="store_true",
                   help="target the newest GTNH release instead of the version the server runs")
    p.add_argument("--version", help="version to install (default: whatever the server runs)")
    p.add_argument("--url", help="explicit pack URL (skips version lookup)")
    p.add_argument("--file", help="use an already-downloaded pack zip")
    p.add_argument("--java", choices=["17", "8"], default=None,
                   help="pack flavour: 17 for the Java 17-25 packs, 8 for the Java 8 ones "
                        "(default: whichever your instance already uses, else 17)")
    p.add_argument("--instance", help="path to the existing GTNH instance (default: auto-detect)")
    p.add_argument("--instances-dir", help="Prism instances folder to install into "
                                           "when you have no GTNH instance yet")
    p.add_argument("--mode", choices=["new", "in-place"], default=None,
                   help="new: install as a separate instance and copy your data across "
                        "(default, recommended). in-place: update the existing instance folder.")
    p.add_argument("--name", help="name for the new instance (mode=new)")
    p.add_argument("--backup-mode", choices=["user", "full", "none"], default="user",
                   help="user: saves, settings and other user data (default). "
                        "full: the entire instance folder. none: skip the backup.")
    p.add_argument("--backup-dir", help="where to write the backup zip "
                                        "(default: GTNH-Backups next to your instances folder)")
    p.add_argument("--cache-dir", help="where to keep downloaded packs (default: system temp)")
    p.add_argument("--keep-instance-cfg", action="store_true",
                   help="copy the old instance.cfg wholesale instead of only the safe keys "
                        "(carries your JVM args too — may break a Java-version change)")
    p.add_argument("--keep-extra-mods", action="store_true",
                   help="carry mods you added yourself into the updated instance "
                        "(they are only listed, not copied, by default)")
    p.add_argument("--no-self-update", action="store_true",
                   help="don't update this script from GitHub before running")
    p.add_argument("--keep-download", action="store_true", help="don't delete the pack zip afterwards")
    p.add_argument("--force", action="store_true", help="overwrite an existing target instance")
    p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    args.mode = args.mode or ("in-place" if (args.check or args.setup) else "new")
    if not args.no_self_update:
        self_update(sys.argv)

    if args.list:
        versions = list_versions()
        if not versions:
            die("no releases returned by the GitHub API")
        print("Available GTNH versions (nightlies excluded):")
        for v in versions[:25]:
            print("  %-22s %s  %s" % (v["version"], v["published"],
                                      "pre-release" if v["prerelease"] else "stable"))
        return 0
    if args.restore is not None:
        return run_restore(args)
    if args.install_hook or args.remove_hook:
        return manage_hook(args)
    if args.check:
        return run_check(args)
    rc = run_update(args)
    if rc == 0 and args.setup and not args.dry_run:
        args.install_hook, args.remove_hook = True, False
        # Use the instance we just wrote, never re-detect: with more than one
        # GTNH instance around, re-detection would stop and ask.
        if getattr(run_update, "target", None):
            args.instance = str(run_update.target)
        rc = manage_hook(args)
    return rc


def run_update(args):
    # ---- work out where it's going --------------------------------------
    old = pick_instance(args.instance, args.yes, allow_none=True)
    if old is None:
        instances_root = (Path(args.instances_dir).expanduser() if args.instances_dir
                          else default_instances_root())
        log("no GTNH instance yet — installing a fresh one into %s" % instances_root)
        args.mode = "new"
        args.backup_mode = "none"
        old_version = "none"
    else:
        old_version = instance_version(old)
        instances_root = old.parent
    if args.mode == "in-place" and old is None:
        args.mode = "new"

    # Stay on the flavour the instance already uses, so a Java 8 setup is not
    # silently handed a pack that needs Java 17+.
    if not args.java:
        args.java = instance_java_flavour(old) or "17"
        if old is not None:
            log("java:       your instance uses the Java %s flavour of the pack"
                % ("17-25" if args.java == "17" else "8"))

    # ---- work out what we're installing ---------------------------------
    if args.file:
        pack = Path(args.file).expanduser().resolve()
        if not pack.is_file():
            die("no such file: %s" % pack)
        m = re.search(r"GT_New_Horizons_(.+?)_(?:Java|Client)", pack.name)
        version = args.version or (m.group(1) if m else pack.stem)
        url, size = None, pack.stat().st_size
    elif args.url:
        url = args.url
        m = re.search(r"GT_New_Horizons_(.+?)_(?:Java|Client)", url)
        version = args.version or (m.group(1) if m else "custom")
        size, why = remote_size(url)
        if size is None:
            die("cannot download %s\n       %s" % (url, why))
        pack = None
    else:
        version = args.version
        if not version and not args.latest:
            status = probe_server(args.server)
            if status.get("version"):
                version = status["version"]
                log("server %s is running GTNH %s (%s)"
                    % (args.server, version, status.get("motd") or "no MOTD"))
            else:
                warn("could not ask %s which version it runs (%s) — using the newest release"
                     % (args.server, status.get("error") or "no version in the MOTD"))
        if version:
            found = resolve_pack(version, args.java)
            if not found:
                die(explain_resolve_failure(version, resolve_pack.problems))
            url, size = found
        else:
            versions = list_versions()
            if not versions:
                die("no releases returned by the GitHub API")
            version, url, size = resolve_latest(args.java, versions)
        pack = None

    backup_dir = (Path(args.backup_dir).expanduser() if args.backup_dir
                  else instances_root.parent / "GTNH-Backups")
    cache_dir = (Path(args.cache_dir).expanduser() if args.cache_dir
                 else Path(tempfile.gettempdir()) / "gtnh-packs")

    if old is not None and old_version == version and not args.force:
        log("%s is already on %s — nothing to do (use --force to reinstall)." % (old.name, version))
        return 0

    print()
    log("instance:   %s" % ("%s  (currently %s)" % (old, old_version) if old
                            else "none yet — fresh install"))
    log("target:     %s" % version)
    if url:
        log("pack:       %s (%s)" % (url, human(size) if size > 0 else "size unknown"))
    else:
        log("pack:       %s" % pack)
    log("mode:       %s" % ("fresh install" if old is None else
                            "new instance (old one left untouched)" if args.mode == "new"
                            else "in-place update of the existing instance"))
    log("backup:     %s" % ("skipped (--backup-mode none)" if args.backup_mode == "none"
                            else "%s -> %s" % (args.backup_mode, backup_dir)))
    log("updater:    %s" % __version__)
    print()

    if args.mode == "in-place" and args.backup_mode == "none" and not args.dry_run:
        warn("in-place update with no backup — there is no way back if this goes wrong.")
    if not args.dry_run and not confirm("Close Prism Launcher first. Proceed?", args.yes):
        log("aborted, nothing changed")
        return 1

    # ---- 1. backup, always first ----------------------------------------
    backup = None
    if old is not None and args.backup_mode != "none":
        backup = make_backup(old, backup_dir, args.backup_mode, old_version, args.dry_run, args.yes)

    # ---- 2. fetch --------------------------------------------------------
    if pack is None:
        cached = cache_dir / url.rsplit("/", 1)[-1]
        if args.dry_run:
            # Never pull hundreds of MB just to preview.
            if cached.is_file() and (size <= 0 or cached.stat().st_size == size):
                pack = cached
                log("using cached download %s" % cached)
            else:
                log("would download %s -> %s" % (human(size) if size > 0 else "pack", cached))
        else:
            pack = download(url, cached, size)
    if pack is not None:
        open_pack(pack)[0].close()

    # ---- 3. migrate ------------------------------------------------------
    if args.mode == "new":
        name = args.name or ("GT New Horizons %s" % version)
        target = migrate_new_instance(old, pack, instances_root, version, name,
                                      args.keep_instance_cfg, args.keep_extra_mods,
                                      args.dry_run, args.force)
    else:
        target = migrate_in_place(old, pack, version, args.keep_extra_mods, args.dry_run)
        # "GT New Horizons 2.9.0-beta-1" holding beta-2 is just confusing. Only
        # rename when the name carries the old version, never a custom name.
        cfg = read_cfg(target / "instance.cfg")
        display = cfg.get("name", target.name)
        if old_version != "none" and old_version in display and not args.dry_run:
            cfg["name"] = display.replace(old_version, version)
            write_cfg(target / "instance.cfg", cfg)
            log("renamed the instance to %r in Prism" % cfg["name"])
    run_update.target = target

    pack_name = Path(url or pack or "").name
    if not args.dry_run:
        ensure_java(target, pack_name, args.dry_run, args.yes)
    elif pack_java_range(pack_name):
        low, high = pack_java_range(pack_name)
        log("java:       this pack needs Java %s" % ("%d-%d" % (low, high) if low != high else low))

    if not args.keep_download and url and not args.dry_run:
        try:
            Path(pack).unlink()
        except OSError:
            pass

    # ---- 4. what to do next ---------------------------------------------
    print()
    if args.dry_run:
        log("dry run complete — nothing was changed.")
        return 0
    log("done: %s is now on %s" % (target.name, version))
    if backup:
        print("      backup:  %s" % backup)
        print("               put anything back with --restore, e.g.")
        print("               --restore --only journeymap,visualprospecting,saves")
    print("      next:    open Prism, check the instance's Java version (Java 21+ for 17-25 packs),")
    print("               memory (4-6 GB) and launch once before deleting anything.")
    if args.mode == "new" and old is not None:
        print("      note:    your old instance %s is untouched — delete it once the new one works." % old.name)
        if "gtnh-prism-update" in read_cfg(target / "instance.cfg").get("PreLaunchCommand", ""):
            manage_hook(argparse.Namespace(instance=str(target), server=args.server,
                                           yes=True, remove_hook=False, install_hook=True))
    print("      reminder: update one major version at a time (2.6 -> 2.7 -> 2.8), and answer")
    print("               'Yes' if the game asks about missing blocks on first world load.")
    if "gtnh-prism-update" not in read_cfg(target / "instance.cfg").get("PreLaunchCommand", ""):
        print("      tip:     run this script once with --install-hook and Prism will check")
        print("               against %s every time you press Play." % args.server)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
