bigprint for Windows (beta)
===========================

A free model cutter for life-size 3D prints: scale a model up to life size and
split it into printable, pinned, labelled pieces. No installation and no
Python needed.

bigprint is in beta. Before committing to a big print, print one joint (two
neighbouring pieces and a dowel) to check the fit with your printer.

Getting started
---------------
1. Unzip this folder anywhere you like (for example Documents\bigprint).
   Keep all the files together: the programs need the _internal folder.
2. Double-click bigprint.exe. Your web browser opens to bigprint.
   It runs entirely on your computer; nothing is uploaded anywhere.
3. When you're done, click "Quit bigprint" at the top right of the page.
   If you just close the tab, bigprint stops by itself after 10 minutes.

Double-clicking bigprint.exe while it is already running simply reopens the
page.

Windows may warn "Windows protected your PC" the first time, because this
build isn't signed by a known publisher yet. Click "More info", then
"Run anyway".

Command line
------------
bigprint-cli.exe offers every option from a terminal, for example:

    bigprint-cli.exe "C:\Models\statue.stl" --height 1.8m --printer bambu-x1

Run "bigprint-cli.exe --help" for all options.

Troubleshooting
---------------
The GUI writes a log to %LOCALAPPDATA%\bigprint\bigprint-gui.log.

Support bigprint
----------------
bigprint is free. If it saved you time or filament, a tip keeps it going:

    Ko-fi:            https://ko-fi.com/pootertf
    Buy Me a Coffee:  https://buymeacoffee.com/pootertf
    GitHub Sponsors:  https://github.com/sponsors/PooterTF

License and source code
-----------------------
bigprint is free software, licensed under the GNU Affero General Public
License v3.0 or later. The full license is in licenses\bigprint-LICENSE.txt.
Source code for this and every release:

    https://github.com/PooterTF/bigprint

Third-party components and their licenses are listed in the licenses folder.
