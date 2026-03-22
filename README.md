# cc-install

Install any version of Adobe Creative Cloud apps on macOS, including versions Adobe no longer offers through Creative Cloud.

## Why?

Adobe restricts Creative Cloud to installing only the latest version and one version back. If you're a developer testing plugin compatibility, need to open legacy projects, or just want a specific version that worked better for your workflow, you're stuck. Until now.

## How it works

1. Queries Adobe's public product API for all available versions (going back to CS6)
2. Downloads packages directly from Adobe's CDN (`ccmdls.adobe.com`)
3. Installs via Adobe's own HyperDrive installer using a reverse-engineered IPC protocol

**No patches. No cracks. No third-party binaries.** Everything comes from Adobe's servers and installs through Adobe's own tools. A valid Creative Cloud subscription is required.

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.8+
- Adobe Creative Cloud desktop app installed
- Active Creative Cloud subscription

## Usage

```bash
# Interactive mode
python3 cc_install.py

# List all available products
python3 cc_install.py --list

# List available versions of After Effects
python3 cc_install.py --list AEFT

# Install After Effects 2022 directly
python3 cc_install.py -s AEFT -v 22.6

# Install Photoshop 2021
python3 cc_install.py -s PHSP -v 22.5.1

# Download only (don't install)
python3 cc_install.py -s AEFT -v 22.6 --download-only -d ~/Downloads/ae2022
```

## Common SAP codes

| Code | Application |
|------|------------|
| AEFT | After Effects |
| PHSP | Photoshop |
| PPRO | Premiere Pro |
| ILST | Illustrator |
| IDSN | InDesign |
| AME  | Media Encoder |
| AUDE | Audition |
| DRWV | Dreamweaver |
| LTRM | Lightroom Classic |
| ANMR | Animate |

## How the IPC protocol works

Adobe's `HDBox/Setup` binary communicates via named pipes using a simple framed protocol:

1. **Pipe setup**: Two FIFOs are created at `/tmp/{name}_IN` (Setup writes) and `/tmp/{name}_OUT` (Setup reads).
2. **Frame format**: Each message is an 8-byte flags field, a 4-byte little-endian data length, and a UTF-8 XML payload.
3. **Session flow**:
   - Send `hdpimCreateSession` to receive a session UUID.
   - Send `hdpimInstallProduct` with the session UUID and driver XML to begin installation. Setup sends progress callbacks until complete.
   - Send `hdpimTerminateSession` to clean up.

## Legal

This tool downloads software from Adobe's own servers using their public API. A valid Creative Cloud subscription is required. The installed apps authenticate against your Adobe account on launch, just like any app installed through Creative Cloud.

This tool does not circumvent any copy protection or DRM. It provides access to versions that Adobe has chosen to hide from the Creative Cloud UI while still hosting on their servers.

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## License

MIT
