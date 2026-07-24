# gtnh-prism-updater

Keeps a [GregTech: New Horizons](https://www.gtnewhorizons.com/) instance in
[Prism Launcher](https://prismlauncher.org/) on the same version as your server —
and backs up your worlds and settings before it touches anything.

One Python file, standard library only, no dependencies. Works on Windows, macOS
and Linux.

```
python3 gtnh-prism-update.py --setup      # install/update + check on every launch
python3 gtnh-prism-update.py --check      # ask the server, offer to update
python3 gtnh-prism-update.py --dry-run    # show what it would do, change nothing
python3 gtnh-prism-update.py --list       # every GTNH version available
```

Non-technical players can double-click `GTNH-Updater.bat` (Windows) or
`gtnh-updater.command` (macOS/Linux) instead — see [README.txt](README.txt).


## What it does

1. **Asks your server which version it runs.** A plain Minecraft server-list ping
   reads the pack version out of the MOTD (`GT:New Horizons 2.9.0-beta-2`), so
   nothing has to be installed or changed server-side. With `--latest` it targets
   the newest GTNH release instead; nightlies are always skipped.
2. **Backs up first**, before downloading anything — a timestamped zip holding
   saves, journeymap, config, options, servers.dat, schematics, screenshots,
   shaderpacks, visualprospecting and `instance.cfg`, written to `GTNH-Backups/`
   next to your instances folder. `--backup-mode full` archives the entire
   instance, `none` skips it.
3. **Downloads the client pack** from `downloads.gtnewhorizons.com` — resumable,
   retried, size-verified, and structure-checked before use. Betas and RCs come
   from the `betas/` path automatically.
4. **Migrates.** `--mode new` (default) installs the new version as a separate
   instance and copies your user data across, leaving the old one untouched —
   the method the [GTNH wiki recommends][wiki]. `--mode in-place` replaces
   `config`, `mods`, `serverutilities`, `libraries`, `patches` and
   `mmc-pack.json` in the existing instance while merging `journeymap` and
   `resourcepacks` and keeping your saves and `instance.cfg`.

[wiki]: https://wiki.gtnewhorizons.com/wiki/Installing_and_Migrating


## The launch-time check

`--setup` (or `--install-hook`) registers the script as Prism's **pre-launch
command** for the instance. From then on, pressing Play pings the server, and if
your version differs a dialog appears:

```
The server is running GTNH 2.9.0-beta-2.
Your instance 'GTNH' is on 2.9.0-beta-1.
You need to update before you can join.

Update now? Your saves and settings are backed up first.
```

**Yes** backs up, updates in place, and cancels that launch so Prism re-reads the
new pack files — press Play again to start. **No** launches the game anyway
(single-player still works, joining the server won't). If the server is
unreachable or its MOTD carries no version, it warns and launches normally: a
network hiccup never blocks you from playing.

Point it at your own server with `--server host:port`, the `GTNH_SERVER`
environment variable, or by editing `SERVER_ADDRESS` at the top of the script.


## Settings safety

- `JvmArgs` is **not** carried into a new instance — Java arguments are
  version-specific and a common way to break a fresh install. Java path, memory
  limits, window/console overrides and notes are carried. `--keep-instance-cfg`
  copies the whole file instead.
- `config/shaders.properties` and `config/vendingmachine/favourites` survive the
  config replacement.
- Mods you added yourself are detected and listed, so you know what to re-add;
  `--keep-extra-mods` carries them forward automatically.
- Nothing is deleted before the backup is written, and `--dry-run` prints the
  full plan without downloading a single byte.


## Requirements

- Python 3.8+ — the double-click launchers install it for you if it's missing
  (winget or the official installer on Windows, the python.org package on macOS,
  your package manager on Linux), always after asking first
- Prism Launcher or MultiMC
- Java 21+ for the Java 17-25 packs, Java 8 for `--java 8`
- Linux: `python3-tk` for the pop-up dialog; without it the check falls back to
  a terminal prompt


## Options

| Flag | Meaning |
| --- | --- |
| `--setup` | install or update to the server's version, then install the launch check |
| `--check` | compare against the server and offer to update (used by the hook) |
| `--install-hook` / `--remove-hook` | add or remove Prism's pre-launch check |
| `--server HOST:PORT` | server to ask (default: the baked-in `SERVER_ADDRESS`) |
| `--latest` | target the newest GTNH release instead of the server's version |
| `--version X` / `--url U` / `--file F` | pin a version, a URL, or a downloaded pack |
| `--mode new｜in-place` | separate instance (default) or update the existing one |
| `--instance DIR` / `--instances-dir DIR` | skip auto-detection |
| `--backup-mode user｜full｜none` | how much to archive first (default `user`) |
| `--backup-dir` / `--cache-dir` | where backups and downloads go |
| `--java 17｜8` | which pack flavour to prefer |
| `--keep-extra-mods` / `--keep-instance-cfg` | carry your mods / your whole instance.cfg |
| `--force` / `--dry-run` / `-y` | reinstall, preview, or skip prompts |

Instance auto-detection covers the usual Prism and MultiMC locations on all
three platforms, including Flatpak and Snap.


## Restoring a backup

Close Prism and unzip the archive over the instance folder, replacing files.
Backups are ordinary zips — nothing proprietary, nothing to install.


## License

MIT
