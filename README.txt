GTNH updater — read me first
============================

This keeps your GregTech: New Horizons copy on the same version as our server,
so you can always join. It backs your stuff up before it changes anything.


What you need once
------------------
1. Prism Launcher            https://prismlauncher.org/download/
2. Java 21 (or newer)        Prism will offer to download it for you
3. Our server in your ZeroTier network (so 10.242.74.230 is reachable)

Python 3 is also required, but you don't have to go get it — if it's missing,
the updater offers to install it for you and does the whole thing itself.
(Windows: silent, no clicking. Mac/Linux: it asks for your password once,
the way any installer does.) Say no and it just prints the download link.


How to use it
-------------
Windows : double-click  GTNH-Updater.bat
Mac      : double-click  gtnh-updater.command
           macOS blocks files downloaded from the web. The first time, either
           RIGHT-CLICK the file and choose Open (then Open again), or run this
           once in Terminal:
               xattr -dr com.apple.quarantine ~/Downloads/gtnh-updater
Linux    : run          ./gtnh-updater.command

The first time, it asks whether you are part of Squishy Squadron. Answer Yes —
that points it at our server and turns on the mod fixes we run. (If you're
someone else who found this tool: answer No and it shows you how to point it
at your own server; --reconfigure asks again.)

It will:
  * check that your Java works with the version it installs, and switch the
    instance to a Java that does if it finds one
  * apply our pinned mod fixes and extras (Angelica 2.1.51 fix, FPS Reducer,
    borderless fullscreen; which fixes the
    personal dimension rendering only the block you stand in when clouds are
    off) — each fix retires itself once the pack ships that version
  * ask the server which GTNH version it is running
  * install that version if you have no GTNH instance yet
  * update your existing instance to it if they differ
  * back up your worlds, settings, screenshots and waypoints first
  * set Prism up so it checks the server every time you press Play

That's it. Give it time — the pack is about 650 MB.

It updates the GTNH instance you already have, in place: same instance in
Prism, same worlds, same settings, renamed to the new version number. Nothing
is deleted, and the backup is taken before any of it. If you would rather keep
the old version around as a separate instance, add --mode new.

It also keeps itself up to date — each run checks for a newer version of the
updater and fetches it, so you never have to download this zip again.


After that
----------
When we update the server, just press Play in Prism as usual. If your copy is
behind, a box pops up:

    The server is running GTNH 2.9.0-beta-2.
    Your instance 'GTNH' is on 2.9.0-beta-1.
    Update now? Your saves and settings are backed up first.

Click Yes, wait for it to finish, then press Play again. Click No and the game
still starts — you just won't be able to join the server.

Close Prism before running the updater by hand. Your worlds are never deleted.


If something goes wrong
-----------------------
Backups are zip files in the GTNH-Backups folder next to your Prism
"instances" folder, named like GTNH_2.8.4_20260724-153143.zip.

Close Prism, then put things back with:

  python3 gtnh-prism-update.py --restore
      everything from the newest backup

  python3 gtnh-prism-update.py --restore --only journeymap,visualprospecting
      just your map, waypoints and ore veins

  python3 gtnh-prism-update.py --restore --dry-run
      show what a backup holds without writing anything

Add --only saves for worlds. You can also just unzip the backup over the
instance folder by hand — it's an ordinary zip.

Missing waypoints or ore veins are not always lost: JourneyMap and
VisualProspecting file their data per server, under a folder named after the
server entry. If that name changes, they start a fresh empty map and the old
data is still sitting in .minecraft/journeymap/data/mp/ and
.minecraft/visualprospecting/ under the previous name. Look there first;
renaming the old folder to the new name brings everything back.

Your worlds live in <instance>/.minecraft/saves — worth copying somewhere safe
before a big update, on top of what this does.

The first launch after an update may ask about "missing blocks". Answer Yes.


Handy extras (Terminal / Command Prompt)
----------------------------------------
  python3 gtnh-prism-update.py --check        ask the server, compare, offer to update
  python3 gtnh-prism-update.py --dry-run      show what it would do, change nothing
  python3 gtnh-prism-update.py --list         list every GTNH version available
  python3 gtnh-prism-update.py --latest       update to the newest GTNH release
                                              instead of the server's version
  python3 gtnh-prism-update.py --remove-hook  stop checking the server on launch
  python3 gtnh-prism-update.py --help         everything else

Server it asks: 10.242.74.230:25565  (override with --server host:port)
