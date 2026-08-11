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

# Our server. Its MOTD carries the pack version, so a plain server-list ping
# tells us which version a client needs. Anyone else can point the updater at
# their own with --server, the GTNH_SERVER variable, or the config file.
SQUAD_NAME = "Squishy Squadron"
SQUAD_SERVER = "10.242.74.230:25565"     # ZeroTier address of hermes

# Mod fixes applied to squad members' instances until GTNH itself ships them.
# Each entry stops applying as soon as the pack carries an equal or newer
# version, so this list does not need pruning to stay correct.
MOD_FIXES = [
    # Empty by design. Hand-picking single jars into a pack repeatedly broke
    # things: a removed method with two callers, then a hard dependency on a
    # newer CodeChickenCore that took the server down. Daily builds ship whole
    # coherent mod sets, so fixes belong there — gtnh-daily-updater pulls them.
    # If something genuinely needs pinning ahead of the pack, verify BOTH that
    # nothing calls a method it removed AND that its declared dependencies are
    # satisfied before adding an entry here.
]

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

# RAM guidance per the GTNH wiki: 6 GB, min equal to max (unequal values cause
# GC pauses), and never more than 8 GB (G1 degrades with huge heaps).
RECOMMEND_MB = 6144
MAX_SANE_MB = 8192

# The wiki's tuned GC set for Java 8 ONLY. Java 17+ must NOT get these — the
# wiki is explicit that they are built in there, and the module flags a modern
# pack needs come from the pack's own patches/ files via Prism.
JAVA8_ARGS = ("-XX:+UseG1GC -XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC "
              "-XX:MaxGCPauseMillis=80 -Dsun.rmi.dgc.server.gcInterval=2147483646 "
              "-XX:G1NewSizePercent=20 -XX:G1ReservePercent=20")

# Optional extras installed for squad members when absent. Unlike MOD_FIXES
# these never replace anything — if a jar matching `match` exists (the user's
# own copy, or the pack shipping it one day), the entry does nothing.
MOD_EXTRAS = [
    {
        "match": "fpsreducer",
        "packs": "2.9",
        "jar": "FpsReducer-mc1.7.10-1.10.3.jar",
        "url": "https://mediafilez.forgecdn.net/files/2627/303/FpsReducer-mc1.7.10-1.10.3.jar",
        "why": "drops CPU/GPU load and mutes sound while AFK or tabbed out (client-side, "
               "from the wiki's Additional Mods list)",
    },
    {
        # GTNH's own WorldEdit fork. Client-side this enables single-player
        # editing; on the squad server it works only if the server runs it too.
        "match": "worldedit-v",
        "packs": "2.9",
        "jar": "worldedit-v0.0.10.jar",
        "url": "https://github.com/GTNewHorizons/worldedit-gtnh/releases/download/v0.0.10/worldedit-v0.0.10.jar",
        "why": "WorldEdit (GTNH fork) — //wand, //copy, //paste in single-player",
    },
    {
        "match": "worldeditcui",
        "packs": "2.9",
        "jar": "WorldEditCuiFe-v1.0.7-mf-1.7.10-10.13.4.1566.jar",
        "url": "https://mediafilez.forgecdn.net/files/2390/420/WorldEditCuiFe-v1.0.7-mf-1.7.10-10.13.4.1566.jar",
        "why": "WorldEdit CUI — draws your WorldEdit selections in the world",
    },
]

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
__version__ = "2.0.0"
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


def _dialog(kind, title, message, timeout, default):
    """Show a dialog that closes itself, so nothing waits on a human forever.

    Deliberately NOT tkinter.messagebox: on macOS that is a native NSAlert run
    by runModalForWindow:, which ignores Tk's event loop — an `after` deadline
    never fires it and the process blocks until killed, holding Prism's Play
    button hostage. A plain Toplevel is ours to destroy on time.
    """
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title(title)
        answer = {"value": default}

        def finish(value):
            answer["value"] = value
            root.destroy()

        frame = tk.Frame(root, padx=22, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=message, justify="left", wraplength=460).pack(anchor="w")
        buttons = tk.Frame(frame)
        buttons.pack(anchor="e", pady=(16, 0))
        if kind == "askyesno":
            tk.Button(buttons, text="Not now", width=10,
                      command=lambda: finish(False)).pack(side="right", padx=(8, 0))
            confirm_button = tk.Button(buttons, text="Update", width=10,
                                       command=lambda: finish(True))
        else:
            confirm_button = tk.Button(buttons, text="OK", width=10, command=lambda: finish(True))
        confirm_button.pack(side="right")

        root.bind("<Return>", lambda _e: finish(kind == "askyesno" or default))
        root.bind("<Escape>", lambda _e: finish(default))
        root.protocol("WM_DELETE_WINDOW", lambda: finish(default))
        root.after(int(timeout * 1000), root.destroy)   # the deadline that works
        root.attributes("-topmost", True)
        root.update_idletasks()
        width, height = root.winfo_width(), root.winfo_height()
        root.geometry("+%d+%d" % (max(0, (root.winfo_screenwidth() - width) // 2),
                                  max(0, (root.winfo_screenheight() - height) // 3)))
        root.lift()
        try:
            root.focus_force()
            confirm_button.focus_force()
        except Exception:
            pass
        if platform.system() == "Darwin":
            # Prism spawns us without activating, so the window floats above
            # everything while the keyboard still belongs to another app.
            try:
                subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to set frontmost of every process '
                     'whose unix id is %d to true' % os.getpid()],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                    **quiet_process_kwargs())
            except (OSError, subprocess.SubprocessError):
                pass                                     # buttons still work
        root.mainloop()                                  # returns once destroyed
        return answer["value"]
    except Exception:
        return None                                      # no GUI at all


def ask_yes_no(title, message, assume_yes=False, timeout=180):
    """Yes/no question — a real dialog box when one is possible, else the terminal.

    Prism runs pre-launch commands without a usable stdin, so the dialog is
    what makes the launch-time check work at all.
    """
    if assume_yes:
        return True
    print(message, flush=True)
    answer = _dialog("askyesno", title, message, timeout, False)
    if answer is None:
        return confirm("Continue?", False)
    return bool(answer)


def show_message(title, message, timeout=60):
    print(message, flush=True)
    return _dialog("showinfo", title, message, timeout, True)


# --------------------------------------------------------------------------
# settings, asked once and remembered
# --------------------------------------------------------------------------

def config_path():
    if IS_WIN:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "gtnh-updater" / "config.json"


def load_config():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
    except OSError as e:
        warn("could not save your settings (%s)" % e)


def resolve_server(args):
    """Return (server, squad_member), asking once on the very first run.

    Squad members get our address baked in; everybody else is pointed at the
    config file so the tool is useful to them too.
    """
    cfg = load_config()
    if args.squad is not None:                       # explicit flag, remember it
        cfg["squad"] = bool(args.squad)
        if args.squad and not args.server:
            cfg["server"] = SQUAD_SERVER
        save_config(cfg)
    if args.server:                                  # explicit address wins
        changed = cfg.get("server") != args.server
        cfg["server"] = args.server
        # Hooks installed before the question existed bake our address in;
        # pointing at the squad server IS the answer.
        if args.server == SQUAD_SERVER and "squad" not in cfg:
            cfg["squad"] = True
            changed = True
        if changed:
            save_config(cfg)
        return args.server, bool(cfg.get("squad"))
    from_env = os.environ.get("GTNH_SERVER")
    if from_env:
        return from_env, bool(cfg.get("squad")) or from_env == SQUAD_SERVER

    if "squad" not in cfg or args.reconfigure:
        joined = ask_yes_no(
            "GTNH updater — first run",
            "Are you part of %s?\n\n"
            "Yes — you play on our server. The updater will keep your GTNH matched "
            "to it, and apply the mod fixes we run.\n\n"
            "No — you play somewhere else or single-player. You'll be shown how to "
            "point this at your own server." % SQUAD_NAME,
            assume_yes=False, timeout=300)
        cfg["squad"] = bool(joined)
        if joined:
            cfg["server"] = SQUAD_SERVER
        save_config(cfg)
        if not joined:
            show_message(
                "GTNH updater — pointing it at your server",
                "No problem. To follow your own server's version, run this once:\n\n"
                "    gtnh-prism-update.py --server your.server.address:25565\n\n"
                "It will be remembered in:\n%s\n\n"
                "Without a server it simply updates to the newest GTNH release, "
                "which you can also ask for directly with --latest."
                % config_path(), timeout=120)
    return cfg.get("server"), bool(cfg.get("squad"))


# --------------------------------------------------------------------------
# mod fixes
# --------------------------------------------------------------------------

def newest_fix_release(fix):
    """Newest published release of a fixed mod, or None if GitHub is unreachable.

    Returns (version, jar name, url). Only releases at or above the version
    that carries the fix are considered.
    """
    if not fix.get("repo") or not fix.get("jar_pattern"):
        return None
    try:
        releases = http_json("https://api.github.com/repos/%s/releases?per_page=20" % fix["repo"])
    except Exception:
        return None
    best = None
    line = version_tuple(fix["fixed_in"])[:2]
    for release in releases:
        tag = (release.get("tag_name") or "").lstrip("vV")
        if release.get("draft") or release.get("prerelease") or not version_tuple(tag):
            continue
        if version_tuple(tag) < version_tuple(fix["fixed_in"]):
            continue
        if version_tuple(tag)[:2] != line:
            # Patch releases within the line the pack is on only. A minor bump
            # is where APIs move, and other pack mods mixin into these — that
            # is exactly how NEI 2.8.119 broke bogosorter's recipe mixin.
            continue
        wanted = fix["jar_pattern"] % tag
        asset = next((a for a in release.get("assets", []) if a["name"] == wanted), None)
        if asset and (best is None or version_tuple(tag) > version_tuple(best[0])):
            best = (tag, asset["name"], asset["browser_download_url"])
    return best


DAILY_TOOL_REPO = "Caedis/gtnh-daily-updater"


def daily_tool_path(instance: Path):
    """Where we keep gtnh-daily-updater — next to Prism, beside our own copy."""
    home = instance.parent.parent if instance.parent.name == "instances" else instance.parent
    return home / ("gtnh-daily-updater.exe" if IS_WIN else "gtnh-daily-updater")


def install_daily_tool(instance: Path):
    """Fetch the right gtnh-daily-updater build for this machine.

    Dailies are not published as pack zips, so updating one means running the
    tool GTNH's own devs maintain: it resolves whole mod sets from the pack
    manifest, which is the only way the dependency graph stays coherent.
    """
    target = daily_tool_path(instance)
    if target.is_file():
        return target
    system, machine = platform.system().lower(), platform.machine().lower()
    goos = {"windows": "windows", "darwin": "darwin", "linux": "linux"}.get(system)
    goarch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if not goos:
        warn("no gtnh-daily-updater build for %s" % system)
        return None
    try:
        latest = http_json("https://api.github.com/repos/%s/releases/latest" % DAILY_TOOL_REPO)
    except Exception as e:
        warn("could not reach the daily-updater releases (%s)" % e)
        return None
    want = "%s-%s" % (goos, goarch)
    asset = next((a for a in latest.get("assets", [])
                  if want in a["name"] and a["name"].endswith(".zip")), None)
    if not asset:
        warn("no daily-updater build published for %s" % want)
        return None
    log("fetching %s %s for %s" % (DAILY_TOOL_REPO, latest.get("tag_name"), want))
    staging = Path(tempfile.mkdtemp(prefix="gtnh-daily-tool-"))
    try:
        archive = staging / asset["name"]
        size, _ = remote_size(asset["browser_download_url"])
        download(asset["browser_download_url"], archive, size or -1, attempts=2)
        with zipfile.ZipFile(long_path(archive)) as zf:
            member = next((n for n in zf.namelist() if n.rsplit("/", 1)[-1].startswith("gtnh-daily-updater")
                           and not n.endswith("/")), None)
            if not member:
                raise ValueError("archive has no gtnh-daily-updater binary")
            with zf.open(member) as src, open(long_path(target), "wb") as dst:
                shutil.copyfileobj(src, dst)
        if not IS_WIN:
            os.chmod(str(target), 0o755)
        return target
    except Exception as e:
        warn("could not install gtnh-daily-updater (%s)" % e)
        return None
    finally:
        rmtree(staging)


def update_daily_instance(instance: Path, dry_run, assume_yes):
    """Hand a daily instance to gtnh-daily-updater and report what it did."""
    tool = install_daily_tool(instance)
    if tool is None:
        die("this instance is on a daily build, which only gtnh-daily-updater can update.\n"
            "       Install it from https://github.com/%s/releases and run:\n"
            "         gtnh-daily-updater update -d \"%s\" --side client" % (DAILY_TOOL_REPO, instance))
    cmd = [str(tool), "update", "-d", str(instance)]
    if dry_run:
        cmd.append("--dry-run")
    log("running: %s" % " ".join(cmd))
    try:
        result = subprocess.run(cmd, timeout=3600)
    except (OSError, subprocess.SubprocessError) as e:
        die("gtnh-daily-updater failed to run (%s)" % e)
    if result.returncode != 0:
        die("gtnh-daily-updater exited %d — nothing further was changed" % result.returncode)
    return True


def apply_mod_fixes(instance: Path, squad, dry_run=False):
    """Swap in fixed mod jars the pack hasn't caught up with yet.

    Costs nothing when there is nothing to do: the decision is made from the
    jar file names, so no network call happens once a fix is in place.
    """
    if not squad or instance is None:
        return
    mc = mc_dir(instance)
    mods = mc / "mods" if mc else None
    if not mods or not mods.is_dir():
        return
    pack = instance_version(instance)
    for fix in MOD_FIXES:
        if fix.get("packs") and not pack.startswith(fix["packs"]):
            # A fix jar built for 2.9 dropped into a 2.8 pack is a crash, not
            # a favour — declined updates keep the pack they have. Say so:
            # silently doing nothing is indistinguishable from being broken.
            log("fix:        skipping %s — this instance reports pack %s, the fix is "
                "for %s. Run --status if that pack version looks wrong."
                % (fix["mod"], pack, fix["packs"]))
            continue
        installed = {}
        for jar in mods.iterdir():
            if jar.is_file() and mod_key(jar.name) == fix["mod"]:
                found = re.search(r"(\d+\.\d+(?:\.\d+)?)", jar.name)
                installed[jar] = version_tuple(found.group(1)) if found else (0,)
        if not installed:
            continue
        newest = max(installed.values())
        wanted = version_tuple(fix["fixed_in"])
        if fix.get("exact"):
            # Pin to one specific jar, downgrading if necessary — used to walk
            # back a fix that turned out to break something else.
            if any(jar.name == fix["jar"] for jar in installed) and len(installed) == 1:
                continue
        elif newest >= wanted:
            continue                                  # pack shipped it; superseded
        have = ".".join(str(n) for n in newest) if newest != (0,) else "unknown"
        jar_name, jar_url, to_version = fix["jar"], fix["url"], fix["fixed_in"]
        if not fix.get("exact"):
            latest = newest_fix_release(fix)
            if latest and version_tuple(latest[0]) > version_tuple(fix["fixed_in"]):
                to_version, jar_name, jar_url = latest
            if newest >= version_tuple(to_version):
                continue                              # already at or past it
        log("fix:        %s %s -> %s" % (fix["mod"], have, to_version))
        print("      %s" % fix["why"])
        if dry_run:
            continue
        target = mods / jar_name
        try:
            size, _ = remote_size(jar_url)
            download(jar_url, target, size or -1, attempts=2)
            with zipfile.ZipFile(long_path(target)) as jar_zip:   # must be a real jar
                if not jar_zip.namelist():
                    raise ValueError("empty jar")
        except SystemExit:
            warn("could not download the %s fix — the pack's own version (%s) is still "
                 "in place. The game runs; the fix retries on the next launch." % (fix["mod"], have))
            return
        except Exception as e:
            warn("could not install the %s fix (%s) — leaving the pack's version (%s). "
                 "It retries on the next launch." % (fix["mod"], e, have))
            if target.exists() and target not in installed:
                target.unlink()
            continue
        for jar in installed:
            if jar != target:
                rmtree(jar)
        log("fix:        installed %s" % jar_name)

    for extra in MOD_EXTRAS:
        if extra.get("packs") and not pack.startswith(extra["packs"]):
            continue
        if any(extra["match"] in jar.name.lower() for jar in mods.iterdir() if jar.is_file()):
            continue                                  # present, whoever put it there
        log("extra:      %s — %s" % (extra["jar"], extra["why"]))
        if dry_run:
            continue
        target = mods / extra["jar"]
        try:
            size, _ = remote_size(extra["url"])
            download(extra["url"], target, size or -1, attempts=2)
            with zipfile.ZipFile(long_path(target)) as jar_zip:
                if not jar_zip.namelist():
                    raise ValueError("empty jar")
            log("extra:      installed %s" % extra["jar"])
        except SystemExit:
            warn("could not download %s — skipping, the game runs fine without it" % extra["jar"])
            return
        except Exception as e:
            warn("could not install %s (%s) — skipping" % (extra["jar"], e))
            if target.exists():
                target.unlink()


def squad_touchups(instance: Path, squad):
    """Everything the launch check maintains besides the version itself."""
    apply_mod_fixes(instance, squad)
    apply_config_tweaks(instance, squad, remember=True)


def apply_config_tweaks(instance: Path, squad, dry_run=False, remember=False):
    """Squad config preferences, reapplied after updates reset the configs.

    Currently: borderless fullscreen — the wiki's recommended replacement for
    exclusive fullscreen on Java 17+ setups.
    """
    if not squad or instance is None:
        return
    mc = mc_dir(instance)
    cfg_file = (mc / "config" / "lwjgl3ify.cfg") if mc else None
    if not cfg_file or not cfg_file.is_file():
        return                       # appears after first launch; caught next time
    user_cfg = load_config()
    if remember and "borderless" in user_cfg.get("tweaks", []):
        return                       # applied once already; their setting is theirs now
    try:
        text = cfg_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if not re.search(r"B:borderless=false", text):
        return
    log("tweak:      enabling borderless fullscreen (lwjgl3ify.cfg) — alt-tab freely")
    if dry_run:
        return
    try:
        cfg_file.write_text(text.replace("B:borderless=false", "B:borderless=true", 1),
                            encoding="utf-8")
        if remember:
            user_cfg.setdefault("tweaks", []).append("borderless")
            save_config(user_cfg)
    except OSError as e:
        warn("could not set borderless (%s)" % e)


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
    # Daily builds stamp a date, e.g. 2.9.0-nightly-2026-08-07 — match those
    # before the shorter release forms so the date is not truncated away.
    m = (re.search(r"\d+\.\d+(?:\.\d+)?[-_]nightly[-_]\d{4}-\d{2}-\d{2}", motd, re.I)
         or re.search(r"\d+\.\d+(?:\.\d+)?(?:[-_](?:beta|rc|alpha|pre)[-_]?\d*)?", motd, re.I))
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

def quiet_process_kwargs():
    """Keep helper processes from flashing up console windows on Windows.

    Prism launches the pre-launch check as a GUI child, so every java -version
    or curl probe would otherwise open its own console window.
    """
    if IS_WIN:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


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
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                              **quiet_process_kwargs())
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


def pack_version_key(text):
    """Orderable key for GTNH pack versions.

    Understands that 2.9.0-beta-2 > 2.9.0-beta-1 and that a final 2.9.0
    outranks its own betas and RCs — version_tuple() alone sees all of those
    as (2, 9, 0).
    """
    text = text or ""
    base = re.split(r"[-_](?:alpha|beta|pre|rc)", text, flags=re.I)[0]
    nums = tuple(int(n) for n in re.findall(r"\d+", base)[:3])
    m = re.search(r"[-_](alpha|beta|pre|rc)[-_]?(\d+)?", text, re.I)
    phase = {"alpha": 0, "beta": 1, "pre": 2, "rc": 2}[m.group(1).lower()] if m else 3
    return nums, phase, int(m.group(2)) if m and m.group(2) else 0


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


def staging_dir(near: Path, tag):
    """Scratch space on the same volume as `near`, but never inside the
    instances folder — Prism scans that and will load a half-extracted pack as
    a real instance while the update is still running."""
    parent = near.parent
    if parent.name == "instances":
        parent = parent.parent
    return parent / (".gtnh-%s-%d" % (tag, os.getpid()))


def extract_pack(zip_path: Path, target: Path):
    zf, root = open_pack(zip_path)
    with zf:
        staging = staging_dir(target, "extract")
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


def daily_state(instance: Path):
    """gtnh-daily-updater's state file, if this instance is on a daily build."""
    try:
        return json.loads((instance / ".gtnh-daily-updater.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def instance_version(instance: Path):
    """Best-effort read of the GTNH version an instance is currently on."""
    state = daily_state(instance)
    if state and state.get("config_version"):
        return state["config_version"]          # daily builds: authoritative
    stamp = instance / ".gtnh-version"
    if stamp.is_file():
        v = stamp.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if v and v[0]:
            return v[0]
    # What the instance calls itself beats a changelog: packs updated by hand
    # keep the changelog of whichever version first shipped it.
    for text in (read_cfg(instance / "instance.cfg").get("name", ""), instance.name):
        m = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-_](?:beta|rc|alpha|pre)[-_]?\d*)?", text)
        if m:
            return m.group(0)
    mc = mc_dir(instance)
    if mc:
        best = None
        for f in mc.glob("changelog from * to *.md"):
            m = re.search(r"to (.+)\.md$", f.name)
            if m and (best is None or version_tuple(m.group(1)) > version_tuple(best)):
                best = m.group(1)
        if best:
            return best
    return "unknown"


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
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / ("%s_%s_%s.zip" % (re.sub(r"[^\w.\-]+", "_", instance.name), version, stamp))

    log("backup (%s): %s of data -> %s" % (mode, human(total), dest))
    for p, arc in items:
        print("      + %s (%s)" % (arc, human(tree_size(p))))
    if dry_run:                       # a dry run must not even create the folder
        return dest
    backup_dir.mkdir(parents=True, exist_ok=True)

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

def cfg_value(path_like):
    """Format a path for instance.cfg.

    instance.cfg is a Qt QSettings INI file, where backslash is an escape
    character: a raw C:\\Users\\eric path comes back out as C:Userseric and the
    launch command cannot start. Prism itself stores paths with forward
    slashes, which Windows accepts everywhere, so do the same.
    """
    return str(path_like).replace("\\", "/")


def ini_escape(value):
    """Encode a value the way Qt's QSettings would write it.

    Qt treats " as a section delimiter and strips it, gluing '"a" "b"' into
    'ab' — which is exactly how a perfectly good launch command turns into one
    nonexistent path. Qt writes them as \\" and reads that back verbatim;
    checked against QSettings itself, not deduced.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def cfg_set(cfg, key, value):
    """Set a value we own, encoded for Qt. Values we never touch stay verbatim."""
    cfg[key] = ini_escape(value)


def read_cfg(path: Path, section="General"):
    """Read one section of an INI file. Other sections are none of our business."""
    out, current = {}, None
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current = stripped[1:-1]
                continue
            if current in (None, section) and "=" in line and not stripped.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_cfg(path: Path, data, section="General"):
    """Update one section in place, leaving the rest of the file untouched.

    Prism keeps a [UI] section (column layouts and such) alongside [General].
    Rewriting the whole file as one flat section destroys it, so edit lines
    where they sit and copy everything else through verbatim.
    """
    if not path.parent.is_dir():
        raise FileNotFoundError("instance folder is gone: %s" % path.parent)
    original = (path.read_text(encoding="utf-8", errors="replace").splitlines()
                if path.is_file() else [])
    out, seen, current, section_end = [], set(), None, None
    for line in original:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current == section and section_end is None:
                section_end = len(out)          # leaving our section: new keys go here
            current = stripped[1:-1]
            out.append(line)
            continue
        if current in (None, section) and "=" in line and not stripped.startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in data:
                out.append("%s=%s" % (key, data[key]))
                seen.add(key)
            continue                            # key dropped from data: drop the line
        out.append(line)

    fresh = ["%s=%s" % (k, v) for k, v in data.items() if k not in seen]
    if not original:
        out = ["[%s]" % section] + fresh
    elif section_end is not None:
        out[section_end:section_end] = fresh
    else:
        out.extend(fresh)

    # Write beside the target and swap it in, so a failure never leaves Prism
    # with a half-written instance.cfg.
    tmp = path.with_name(path.name + ".gtnh-tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


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
    if mc is None or not (instance / "instance.cfg").is_file():
        die("%s is not a usable instance any more (no %s or instance.cfg).\n"
            "       Nothing was changed. If it vanished, restore it with --restore."
            % (instance, ".minecraft"))
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
    touched = set(replace_mc) | set(merge_mc)
    print("      keep:    instance.cfg, %s" % ", ".join(
        r for r in CARRY_OVER if (mc / r).exists() and r.split("/")[0] not in touched))

    if dry_run:
        report_extra_mods(extras, mc / "mods", keep_extra, dry_run)
        return instance

    staging = staging_dir(instance, "new-%s" % re.sub(r"[^\w.\-]+", "_", version))
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
                                stderr=subprocess.STDOUT, timeout=20,
                                **quiet_process_kwargs())
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
        # Prism's downloaded runtimes are bundles on macOS.
        globs.append(root.parent / "java" / "*" / "jre.bundle" / "Contents" / "Home" / "bin" / exe)
        globs.append(root.parent / "java" / "*" / "Contents" / "Home" / "bin" / exe)
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


def launcher_java(java_path):
    """Prefer javaw.exe for the setting Prism launches with.

    java.exe is the console-subsystem binary: Minecraft would run with a
    console window attached for the whole session. Probing still uses
    java.exe, which is the one that reliably prints -version.
    """
    java_path = Path(java_path)
    if IS_WIN and java_path.name.lower() == "java.exe":
        windowless = java_path.with_name("javaw.exe")
        if windowless.is_file():
            return windowless
    return java_path


def apply_java(instance: Path, java_path):
    """Write a decided Java into an instance. Never prompts, never raises."""
    if not java_path:
        return
    java_path = launcher_java(java_path)
    cfg_path = instance / "instance.cfg"
    if not cfg_path.is_file():
        warn("no instance.cfg at %s — set Java for this instance in Prism yourself." % cfg_path)
        return
    cfg = read_cfg(cfg_path)
    cfg["OverrideJavaLocation"] = "true"
    cfg_set(cfg, "JavaPath", cfg_value(java_path))
    for stale in ("JavaVersion", "JavaTimestamp", "JavaSignature",
                  "JavaArchitecture", "JavaRealArchitecture"):
        cfg.pop(stale, None)
    try:
        write_cfg(cfg_path, cfg)
    except OSError as e:
        warn("could not save the Java setting (%s) — set it in Prism yourself" % e)
        return
    log("java:       instance set to %s" % java_path)


def ensure_java(instance: Path, pack_name, dry_run, assume_yes):
    """Make sure the instance points at a Java the new pack can actually run.

    Asked BEFORE anything is downloaded or written: this prompt used to run
    after the migration, and anything that happened to the instance while it
    waited (Prism deleting it, say) turned into a crash at the very end.
    Returns the Java to write once the update is done, or None.
    """
    wanted = pack_java_range(pack_name)
    if not wanted:
        return None
    low, high = wanted
    cfg_path = instance / "instance.cfg"
    if not cfg_path.is_file():
        warn("no instance.cfg at %s — skipping the Java check.\n"
             "         Set Java %s for this instance in Prism yourself."
             % (cfg_path, "%d-%d" % (low, high) if low != high else low))
        return None
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
        return None
    best = sorted(fits)[-1]
    if not current and not cfg.get("OverrideJavaLocation"):
        log("java:       Java %s available at %s if Prism's default is wrong" % best)
        return None
    if dry_run:
        log("java:       would offer to switch the instance to Java %s (%s)" % best)
        return None
    if confirm("Point this instance at Java %s (%s) after updating?" % best, assume_yes):
        return best[1]
    return None


def tune_performance(instance: Path, java_ver, assume_yes, dry_run):
    """Bring memory and GC arguments in line with the GTNH wiki's guidance.

    One combined offer, applied in a single write: 6 GB with min == max, the
    tuned G1 set on Java 8, and no GC arguments at all on Java 17+ (stale
    Java-8 flags carried into a modern setup get cleared, per the wiki).
    """
    cfg_path = instance / "instance.cfg"
    if not cfg_path.is_file():
        return
    cfg = read_cfg(cfg_path)
    changes, notes = {}, []

    min_mb = int(cfg.get("MinMemAlloc") or 0)
    max_mb = int(cfg.get("MaxMemAlloc") or 0)
    if max_mb > MAX_SANE_MB:
        warn("this instance has %d MB allocated — over 8 GB the garbage collector "
             "gets slower, not faster. Consider 6144." % max_mb)
    elif max_mb < RECOMMEND_MB or min_mb != max_mb:
        changes.update({"OverrideMemory": "true",
                        "MinMemAlloc": str(RECOMMEND_MB), "MaxMemAlloc": str(RECOMMEND_MB)})
        notes.append("memory %s -> 6144 MB min and max (unequal or low values cause GC stutter)"
                     % ("%d/%d MB" % (min_mb, max_mb) if max_mb else "unset"))

    args_now = cfg.get("JvmArgs", "").strip().strip('"')
    if java_ver == 8:
        if not args_now:
            changes.update({"OverrideJavaArgs": "true", "JvmArgs": JAVA8_ARGS})
            notes.append("Java 8 tuned G1 garbage-collector arguments (from the GTNH wiki)")
    elif java_ver and java_ver >= 17 and re.search(r"-XX:\+UseG1GC|MaxGCPauseMillis|G1NewSizePercent", args_now):
        changes.update({"OverrideJavaArgs": "false", "JvmArgs": ""})
        notes.append("removing Java-8-era GC arguments — on Java %d they are already built in "
                     "and the wiki says not to use them" % java_ver)

    if not changes:
        log("perf:       memory and Java arguments already follow the wiki guidance")
        return
    for n in notes:
        log("perf:       %s" % n)
    if dry_run:
        return
    if not confirm("Apply these performance settings?", assume_yes):
        return
    cfg.update(changes)
    try:
        write_cfg(cfg_path, cfg)
        log("perf:       applied")
    except OSError as e:
        warn("could not save performance settings (%s)" % e)


def prune_backups(instance: Path, backup_dir: Path, keep):
    """Delete all but the newest `keep` backups for this instance."""
    if keep <= 0:
        return
    for stale in find_backups(instance, backup_dir)[keep:]:
        size = stale.stat().st_size
        try:
            stale.unlink()
            log("backup:     pruned %s (%s) — keeping the newest %d" % (stale.name, human(size), keep))
        except OSError as e:
            warn("could not prune %s (%s)" % (stale.name, e))


def show_changelog(instance: Path, version):
    """Print the highlights of the changelog the pack ships for this hop."""
    mc = mc_dir(instance)
    if not mc:
        return
    match = None
    for f in mc.glob("changelog from * to *.md"):
        if version in f.name:
            match = f
            break
    if not match:
        return
    lines = [l.rstrip() for l in match.read_text(encoding="utf-8", errors="replace").splitlines()]
    shown = 0
    print()
    log("what's new (%s):" % match.name)
    for line in lines:
        if not line.strip():
            continue
        print("      %s" % line[:110])
        shown += 1
        if shown >= 12:
            print("      ... full changelog: %s" % match)
            break


def run_status(args):
    """Everything support needs in one paste."""
    print("gtnh-updater %s" % __version__)
    print("  config    : %s" % config_path())
    cfg = load_config()
    print("              %s" % (json.dumps(cfg) if cfg else "(none yet — first run not done)"))
    server = args.server or cfg.get("server") or os.environ.get("GTNH_SERVER")
    inst = pick_instance(args.instance, True, allow_none=True)
    if inst is None:
        print("  instance  : none found")
        return 0
    icfg = read_cfg(inst / "instance.cfg")
    print("  instance  : %s" % inst)
    print("  pack      : %s" % instance_version(inst))
    hook = icfg.get("PreLaunchCommand", "")
    print("  hook      : %s" % ("installed" if "gtnh-prism-update" in hook else "NOT installed"))
    print("  prism     : %s" % ("RUNNING — settings written now will be overwritten when it closes"
                                if prism_running() else "closed"))
    java = icfg.get("JavaPath", "").strip('"').replace("\\\\", "\\").replace('\\"', '"')
    print("  java      : %s (major %s)" % (java or "(Prism default)", java_major(java) if java else "?"))
    print("  memory    : min %s / max %s MB" % (icfg.get("MinMemAlloc", "?"), icfg.get("MaxMemAlloc", "?")))
    print("  jvm args  : %s" % (icfg.get("JvmArgs") or "(none — correct for Java 17+)"))
    mods = (mc_dir(inst) or inst) / "mods"
    pack = instance_version(inst)
    for fix in MOD_FIXES:
        jars = sorted(p.name for p in mods.glob("*.jar") if mod_key(p.name) == fix["mod"]) if mods.is_dir() else []
        have = max((version_tuple(re.search(r"(\d+\.\d+(?:\.\d+)?)", j).group(1))
                    for j in jars if re.search(r"(\d+\.\d+(?:\.\d+)?)", j)), default=(0,))
        if not jars:
            verdict = "mod not installed"
        elif fix.get("packs") and not pack.startswith(fix["packs"]):
            verdict = "SKIPPED — pack reads %s, fix is for %s" % (pack, fix["packs"])
        elif have >= version_tuple(fix["fixed_in"]):
            verdict = "OK"
        else:
            verdict = "NEEDED — still on the pack's version, fix has not applied"
        print("  fix %-8s: %s  [%s]" % (fix["mod"][:8], ", ".join(jars) or "-", verdict))
    backup_dir = (Path(args.backup_dir).expanduser() if args.backup_dir
                  else inst.parent.parent / "GTNH-Backups")
    backups = find_backups(inst, backup_dir)
    print("  backups   : %d in %s%s" % (len(backups), backup_dir,
          " (newest: %s)" % backups[0].name if backups else ""))
    if server:
        status = probe_server(server)
        print("  server    : %s -> %s" % (server, status.get("version") or status.get("error")))
    else:
        print("  server    : not configured")
    return 0


# --------------------------------------------------------------------------
# launch-time check + Prism pre-launch hook
# --------------------------------------------------------------------------

def prism_running():
    """Is Prism open right now?

    Prism holds instance settings in memory and writes its own copy back when
    it closes, silently undoing anything we wrote to instance.cfg in the
    meantime. Checked once and cached.
    """
    if getattr(prism_running, "cached", None) is not None:
        return prism_running.cached
    running = False
    try:
        if IS_WIN:
            result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq prismlauncher.exe", "/NH"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=20, **quiet_process_kwargs())
            running = b"prismlauncher.exe" in (result.stdout or b"").lower()
        else:
            for name in ("prismlauncher", "PrismLauncher", "multimc", "MultiMC"):
                if subprocess.run(["pgrep", "-x", name], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=10).returncode == 0:
                    running = True
                    break
    except (OSError, subprocess.SubprocessError):
        running = False
    prism_running.cached = running
    return running


def warn_if_prism_open(what):
    if not prism_running():
        return False
    warn("Prism Launcher is open. It keeps its own copy of the instance settings and "
         "will overwrite %s when it closes.\n"
         "         Close Prism, then run this again so the change sticks." % what)
    return True


def stable_python():
    """An interpreter path that will still exist next month.

    sys.executable on macOS often points inside the Command Line Tools bundle,
    which OS upgrades relocate; /usr/bin/python3 is the stub that survives.
    """
    if platform.system() == "Darwin" and "CommandLineTools" in sys.executable:
        if Path("/usr/bin/python3").exists():
            return "/usr/bin/python3"
    return sys.executable


def install_self_near(instance: Path):
    """Keep the copy the hook calls somewhere permanent.

    Left in ~/Downloads it gets cleaned up or moved, and then Prism refuses to
    launch at all because its pre-launch command fails.
    """
    script = Path(os.path.abspath(__file__))
    home = instance.parent.parent if instance.parent.name == "instances" else instance.parent
    target = home / "gtnh-prism-update.py"
    try:
        if target.resolve() != script.resolve():
            shutil.copy2(str(script), str(target))
            log("copied the updater to %s so the launch check keeps working" % target)
        return target
    except OSError as e:
        warn("could not copy the updater next to Prism (%s) — the launch check will "
             "call it where it is now, so don't move or delete %s" % (e, script))
        return script


def hook_command(instance: Path, server, script: Path):
    quote = lambda s: '"%s"' % cfg_value(s)
    return " ".join([quote(stable_python()), quote(script),
                     "--check", "--instance", quote(instance),
                     "--server", server])


def manage_hook(args):
    if not args.server:
        args.server, _ = resolve_server(args)
    if not args.server and not args.remove_hook:
        die("no server to check against — run with --server host:port (or --squad) first")
    inst = pick_instance(args.instance, args.yes)
    cfg_path = inst / "instance.cfg"
    cfg = read_cfg(cfg_path)
    if args.remove_hook:
        if "gtnh-prism-update" not in cfg.get("PreLaunchCommand", ""):
            log("no update check installed on %s" % inst.name)
            return 0
        cfg["PreLaunchCommand"] = ""
        if not cfg.get("PostExitCommand") and not cfg.get("WrapperCommand"):
            cfg["OverrideCommands"] = "false"   # don't leave the box ticked for nothing
        write_cfg(cfg_path, cfg)
        log("removed the launch-time update check from %s" % inst.name)
        return 0
    cfg["OverrideCommands"] = "true"
    cfg_set(cfg, "PreLaunchCommand", hook_command(inst, args.server, install_self_near(inst)))
    write_cfg(cfg_path, cfg)
    if warn_if_prism_open("the launch check"):
        return 1
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
    args.server, squad = resolve_server(args)
    if not args.server:
        log("no server configured — run with --server host:port to follow one. Launching.")
        squad_touchups(inst, squad)
        return 0
    local = instance_version(inst)
    status = probe_server(args.server)

    if status.get("error") or not status.get("version"):
        warn("could not read the server version from %s (%s) — launching anyway"
             % (args.server, status.get("error") or "no version in the MOTD"))
        squad_touchups(inst, squad)
        return 0

    server_ver = status["version"]
    if same_version(local, server_ver):
        log("server %s is on %s — your instance matches. Have fun!" % (args.server, server_ver))
        squad_touchups(inst, squad)
        return 0
    if pack_version_key(local) > pack_version_key(server_ver):
        log("your instance (%s) is AHEAD of the server (%s) — the server needs updating, "
            "not you. Launching." % (local, server_ver))
        squad_touchups(inst, squad)
        return 0

    message = ("The server is running GTNH %s.\n"
               "Your instance '%s' is on %s.\n\n"
               "You need to update before you can join.\n\n"
               "Update now? Your saves and settings are backed up first."
               % (server_ver, inst.name, local))
    if not ask_yes_no("GTNH update needed", message, args.yes):
        warn("not updating — you can play single-player, but joining the server will fail")
        squad_touchups(inst, squad)
        return 0

    args.check = False
    args.version = server_ver
    args.instance = str(inst)
    args.yes = True
    rc = run_update(args)
    if args.dry_run:
        log("dry run — the launch is not being cancelled.")
        return 0
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
    p.add_argument("--server", default=None,
                   help="server to ask for the required version; remembered for next time "
                        "(default: whatever you chose on first run)")
    p.add_argument("--squad", dest="squad", action="store_true", default=None,
                   help="you play on the %s server — use its address" % SQUAD_NAME)
    p.add_argument("--no-squad", dest="squad", action="store_false",
                   help="you don't; skip our address and our mod fixes")
    p.add_argument("--reconfigure", action="store_true",
                   help="ask the first-run questions again")
    p.add_argument("--status", action="store_true",
                   help="print a full diagnostic (instance, versions, hook, fixes, backups)")
    p.add_argument("--keep-backups", type=int, default=3, metavar="N",
                   help="backups to keep per instance, oldest pruned after each new one "
                        "(default: %(default)s; 0 keeps everything)")
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


def pid_alive(pid):
    """Is that process still running? Unsure counts as dead — a lock nobody
    can clear is worse than the small chance of two runs overlapping."""
    if pid <= 0:
        return False
    if IS_WIN:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                            # someone else's process, so alive
    except OSError:
        return False


def single_instance_lock(tag, stale_after=30 * 60):
    """Refuse to run twice at once.

    Prism can fire the pre-launch check again while one is still on screen,
    and two updaters writing the same instance is the last thing anyone needs.
    A lock whose owner has died (closed console, killed launch) is taken over
    immediately — waiting hours for a stale file helps nobody.
    Returns the lock path to release, or None if a LIVE run holds it.
    """
    lock = Path(tempfile.gettempdir()) / ("gtnh-updater-%s.lock" % re.sub(r"\W+", "-", tag)[-60:])
    for attempt in (1, 2):
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            return lock
        except FileExistsError:
            if attempt == 2:
                return None
            try:
                owner = int((lock.read_text(encoding="utf-8", errors="replace").strip() or "0"))
            except (OSError, ValueError):
                owner = 0
            expired = time.time() - lock.stat().st_mtime > stale_after
            if owner == os.getpid() or not pid_alive(owner) or expired:
                try:
                    lock.unlink()              # owner is gone; take it over
                except OSError:
                    return None
                continue
            single_instance_lock.owner = owner
            return None
        except OSError:
            return lock                        # locking is best-effort, never fatal


def release_lock(lock):
    if lock:
        try:
            lock.unlink()
        except OSError:
            pass


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
            # Date-stamped tags are dev builds; the keyword list also has to
            # survive upstream typos like "experiemental". A dated beta or RC
            # is still a pre-release, so check that first.
            if re.search(r"(beta|rc|pre)", v["version"], re.I):
                label = "pre-release"
            elif re.search(r"(experi\w*mental|daily|dev|\d{4}-\d{2}-\d{2})", v["version"], re.I):
                label = "experimental"
            elif v["prerelease"]:
                label = "pre-release"
            else:
                label = "stable"
            print("  %-32s %s  %s" % (v["version"], v["published"], label))
        return 0
    if args.status:
        return run_status(args)
    if args.restore is not None:
        return run_restore(args)
    if args.install_hook or args.remove_hook:
        return manage_hook(args)

    lock = single_instance_lock(args.instance or "auto")
    if lock is None:
        owner = getattr(single_instance_lock, "owner", 0)
        warn("another copy of the updater is running (process %s) — leaving this one to it.\n"
             "         If it is showing an update box, answer that one. Otherwise close it, "
             "or wait a moment and try again." % (owner or "?"))
        return 0
    try:
        if args.check:
            return run_check(args)
        return _run_and_hook(args)
    finally:
        release_lock(lock)


def _run_and_hook(args):
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
    args.server, squad = resolve_server(args)

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
        if not version and not args.latest and not args.server:
            log("no server configured — using the newest GTNH release. "
                "Point at one with --server host:port.")
        if not version and not args.latest and args.server:
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
        apply_mod_fixes(old, squad, args.dry_run)   # fixes still get applied
        return 0
    if (old is not None and old_version not in ("none", "unknown") and not args.dry_run
            and pack_version_key(version) < pack_version_key(old_version)):
        # Never silently downgrade — worlds saved on the newer pack may not
        # load on the older one. -y does not waive this; only a human can.
        if not ask_yes_no(
                "GTNH downgrade?",
                "This would DOWNGRADE %s from %s to %s.\n\n"
                "Worlds saved on %s may not load on %s. A backup is taken first, "
                "but going backwards is rarely what you want.\n\n"
                "Downgrade anyway?" % (old.name, old_version, version, old_version, version),
                assume_yes=False, timeout=300):
            log("not downgrading. If the server is behind, it needs the update — not you.")
            apply_mod_fixes(old, squad, args.dry_run)
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
    if not args.dry_run and prism_running():
        warn("Prism Launcher is open. Close it before updating — Prism rewrites "
             "instance settings when it exits, undoing the Java, memory and "
             "launch-check changes made here.")
    if not args.dry_run and not confirm("Close Prism Launcher first. Proceed?", args.yes):
        log("aborted, nothing changed")
        return 1

    # ---- daily builds are the daily tool's job ---------------------------
    if old is not None and daily_state(old):
        log("this instance is on a daily build (%s) — handing the pack update to "
            "gtnh-daily-updater" % old_version)
        if args.backup_mode != "none":
            backup = make_backup(old, backup_dir, args.backup_mode, old_version,
                                 args.dry_run, args.yes)
            if backup is not None and not args.dry_run:
                prune_backups(old, backup_dir, args.keep_backups)
        if not args.dry_run and prism_running():
            warn("Prism is open — close it first; it rewrites instance settings on exit.")
        update_daily_instance(old, args.dry_run, args.yes)
        run_update.target = old
        if not args.dry_run:
            new_version = instance_version(old)
            log("done: %s is now on %s" % (old.name, new_version))
            java_choice = ensure_java(old, "Java_17-25", False, args.yes)
            apply_java(old, java_choice)
            apply_config_tweaks(old, squad)
        return 0

    # ---- 1. settle the Java question while nothing is at stake ----------
    pack_name = Path(url or pack or "").name
    java_choice = None
    if old is not None:
        java_choice = ensure_java(old, pack_name, args.dry_run, args.yes)
    elif pack_java_range(pack_name):
        low, high = pack_java_range(pack_name)
        log("java:       this pack needs Java %s" % ("%d-%d" % (low, high) if low != high else low))

    # ---- 2. backup ------------------------------------------------------
    backup = None
    if old is not None and args.backup_mode != "none":
        backup = make_backup(old, backup_dir, args.backup_mode, old_version, args.dry_run, args.yes)
        if backup is not None and not args.dry_run:
            prune_backups(old, backup_dir, args.keep_backups)

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

    if not args.dry_run and not (target / "instance.cfg").is_file():
        die("the instance folder %s is not there after the update.\n"
            "       Close Prism and restore it with:  --restore --instance \"%s\"%s"
            % (target, target, "\n       Your backup: %s" % backup if backup else ""))
    if not args.dry_run:
        apply_java(target, java_choice)
    apply_mod_fixes(target, squad, args.dry_run)
    apply_config_tweaks(target, squad, args.dry_run)
    final_java = java_choice or read_cfg(target / "instance.cfg").get("JavaPath", "").strip('"')
    tune_performance(target, java_major(final_java) if final_java else None,
                     args.yes, args.dry_run)
    if not args.dry_run:
        show_changelog(target, version)

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
