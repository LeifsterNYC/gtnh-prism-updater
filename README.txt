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
           (first time: right-click > Open, to get past the security warning)
Linux    : run          ./gtnh-updater.command

It will:
  * ask the server which GTNH version it is running
  * install that version if you have no GTNH instance yet
  * update your existing instance to it if they differ
  * back up your worlds, settings, screenshots and waypoints first
  * set Prism up so it checks the server every time you press Play

That's it. Give it time — the pack is about 650 MB.


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
"instances" folder, named like GTNH_2.8.4_20260724-153143.zip. To go back:
close Prism, then unzip one over the instance folder, replacing files.

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
