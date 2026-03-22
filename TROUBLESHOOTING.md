# Troubleshooting

## "Conflicting process detected"

Close all Adobe apps before installing. The installer can't modify shared components while other Adobe apps are running.

## "Adobe Creative Cloud is not installed"

Install the Creative Cloud desktop app first. The installer needs Adobe's HyperDrive engine, which ships with Creative Cloud.

## "All Adobe API endpoints failed"

Adobe may have changed their API URLs. You can set a custom URL:

```bash
CC_INSTALL_API_URL="https://new-url-here" python3 cc_install.py
```

If you don't know the new URL, paste the source of `cc_install.py` into an AI assistant and ask it to find the current Adobe FFC product API endpoints. The endpoints have been stable for years but could change at any time.

You can also open an issue on the repository. The community will find the new endpoints quickly.

## App won't launch / "damaged or incomplete"

This usually means the install was interrupted. Delete the app from `/Applications/` and run the installer again. Make sure no other Adobe apps are running during installation.

## "Incorrect password"

The tool uses `sudo` to run Adobe's installer, which requires your macOS user password. If you're getting this error, make sure you're entering your macOS login password, not your Adobe account password.

## Installation hangs with no progress

Check `/Library/Logs/Adobe/Installers/Install.log` for details. The most common cause is a conflicting Adobe process that wasn't detected automatically. Try quitting all Adobe apps and the Creative Cloud desktop app, then run the installer again.

## "EsdDirectory does not exist"

The downloaded packages are missing or incomplete. Delete the download directory and run the installer again to re-download everything.
