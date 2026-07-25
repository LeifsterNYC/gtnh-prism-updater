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
4. **Migrates**, in one of two ways — see below.


## In-place or a new instance

| How you run it | Default |
| --- | --- |
| `--setup`, i.e. the double-click launchers | **in-place** |
| `--check`, i.e. the launch hook and its "update now" | **in-place** |
| plain `gtnh-prism-update.py` | **new instance** |
| nothing installed yet | fresh install |

**in-place** replaces `config`, `mods`, `serverutilities`, `libraries`,
`patches` and `mmc-pack.json` inside the existing instance, merges `journeymap`
and `resourcepacks`, and keeps your saves, your `instance.cfg` and everything
else. The instance is renamed in Prism if its name carried the old version
number. This is the default for the launcher and hook flow because one instance
keeps the pre-launch hook attached, keeps auto-detection unambiguous, and avoids
a pile of folders each holding a stale copy of your saves.

**new instance** installs alongside the old one and copies your user data
across, leaving the original completely untouched — the method the
[GTNH wiki recommends][wiki], because a fresh instance cannot inherit a stale
file. The trade-off is a second full copy of your saves on disk.

Either way the backup happens first, and `--mode new` / `--mode in-place`
overrides the default anywhere, including `--setup --mode new` (the hook is
re-pointed at the new instance automatically).

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


## Updating the updater

Every run first checks this repository's latest release and, if there is a newer
one, replaces the script and its launchers and re-runs itself with the same
arguments. Since the launch hook runs on every press of Play, players end up on
current code without re-downloading anything by hand.

Failures are deliberately silent-ish and never fatal — offline, rate-limited or
a read-only folder just logs a line and carries on with the version already
present. `--no-self-update` turns it off entirely.


## Java

The pack flavour follows the instance rather than a guess: an instance carrying
`lwjgl3ify-*-forgePatches.jar` is a Java 17+ setup and gets the `Java_17-25`
pack, anything else gets `Java_8`. Force it with `--java 17` / `--java 8`.

After migrating, the Java the instance actually points at is **run** (`java
-version`) and compared against the range in the pack's file name. If it doesn't
fit, the script searches Prism's downloaded runtimes and the usual system
locations for one that does and offers to switch the instance to it; if nothing
suitable exists it says so and points at Prism's Java settings. A carried-over
`JavaPath` from a Java 8 instance therefore can't silently break a 17-25 pack.


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
| `--mode new｜in-place` | override the default above |
| `--no-self-update` | don't pull a newer copy of this script first |
| `--instance DIR` / `--instances-dir DIR` | skip auto-detection |
| `--backup-mode user｜full｜none` | how much to archive first (default `user`) |
| `--backup-dir` / `--cache-dir` | where backups and downloads go |
| `--java 17｜8` | which pack flavour to prefer |
| `--keep-extra-mods` / `--keep-instance-cfg` | carry your mods / your whole instance.cfg |
| `--force` / `--dry-run` / `-y` | reinstall, preview, or skip prompts |

Instance auto-detection covers the usual Prism and MultiMC locations on all
three platforms, including Flatpak and Snap.


## Restoring a backup

```
python3 gtnh-prism-update.py --restore                    # newest backup, everything
python3 gtnh-prism-update.py --restore --only journeymap,visualprospecting
python3 gtnh-prism-update.py --restore backup.zip --dry-run
```

Restoring merges files back into the instance; it does not roll the pack back,
so you can recover map data without undoing the update. Backups are ordinary
zips, so unzipping one over the instance folder by hand works too.

Note that missing waypoints or ore veins are not always lost data: JourneyMap
and VisualProspecting store per server, in a folder named after the server
entry. If that name changes they start an empty map, and the old data is still
in `.minecraft/journeymap/data/mp/` and `.minecraft/visualprospecting/` under
the previous name — renaming it back restores everything.


## License

MIT
