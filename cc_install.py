#!/usr/bin/env python3
"""
cc-install — Install any version of Adobe Creative Cloud apps on macOS.

Queries Adobe's public API, downloads packages from Adobe's CDN,
and installs via the HDBox/Setup IPC protocol.

Requires: macOS, Python 3.8+, Creative Cloud desktop app, active CC subscription.
Zero external dependencies.
"""

import argparse
import getpass
import json
import os
import platform
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from xml.etree import ElementTree as ET

# ─── Constants ────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
SETUP_PATH = "/Library/Application Support/Adobe/Adobe Desktop Common/HDBox/Setup"

ADOBE_API_URLS = {
    "v4": "https://prod-rel-ffc-ccm.oobesaas.adobe.com/adobe-ffc-external/core/v4/products/all?_type=xml&channel=ccm&channel=sti&platform={platform}&productType=Desktop",
    "v5": "https://prod-rel-ffc-ccm.oobesaas.adobe.com/adobe-ffc-external/core/v5/products/all?_type=xml&channel=ccm&channel=sti&platform={platform}&productType=Desktop",
    "v6": "https://prod-rel-ffc-ccm.oobesaas.adobe.com/adobe-ffc-external/core/v6/products/all?_type=xml&channel=ccm&channel=sti&platform={platform}&productType=Desktop",
}

CDN_BASE = "https://ccmdls.adobe.com"
APPLICATION_JSON_URL = "https://cdn-ffc.oobesaas.adobe.com/core/v3/applications"

PLATFORM_STRING = "osx10-64,osx10,macarm64,macuniversal"

ADOBE_HEADERS = {
    "X-Adobe-App-Id": "accc-apps-panel-desktop",
    "User-Agent": "Adobe Application Manager 2.0",
    "X-Api-Key": "CC_HD_ESD_1_0",
}

LANGUAGES = [
    "en_US", "en_GB", "es_ES", "es_MX", "pt_BR", "fr_FR", "fr_CA",
    "it_IT", "de_DE", "nl_NL", "ru_RU", "uk_UA", "zh_TW", "zh_CN",
    "ja_JP", "ko_KR", "pl_PL", "hu_HU", "cs_CZ", "tr_TR", "sv_SE",
    "nb_NO", "fi_FI", "da_DK", "ALL",
]

DRIVER_XML_TEMPLATE = """<DriverInfo>
    <ProductInfo>
        <Name>Adobe {name}</Name>
        <SAPCode>{sap_code}</SAPCode>
        <CodexVersion>{version}</CodexVersion>
        <Platform>{platform}</Platform>
        <EsdDirectory>./{sap_code}</EsdDirectory>
        <Dependencies>
{dependencies}
        </Dependencies>
    </ProductInfo>
    <RequestInfo>
        <InstallDir>/Applications</InstallDir>
        <InstallLanguage>{language}</InstallLanguage>
    </RequestInfo>
</DriverInfo>"""

DEPENDENCY_TEMPLATE = """            <Dependency>
                <SAPCode>{sap_code}</SAPCode>
                <BaseVersion>{version}</BaseVersion>
                <EsdDirectory>./{sap_code}</EsdDirectory>
            </Dependency>"""


# ─── Utilities ────────────────────────────────────────────────────────────────

class Colors:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    RESET = "\033[0m"

    @staticmethod
    def supports_color():
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def log(msg, level="info"):
    c = Colors if Colors.supports_color() else type("C", (), {k: "" for k in dir(Colors)})
    icons = {"info": f"{c.CYAN}●{c.RESET}", "ok": f"{c.GREEN}✓{c.RESET}",
             "warn": f"{c.YELLOW}⚠{c.RESET}", "error": f"{c.RED}✗{c.RESET}",
             "step": f"{c.BOLD}→{c.RESET}"}
    icon = icons.get(level, "●")
    print(f"  {icon} {msg}", flush=True)


def fatal(msg):
    log(msg, "error")
    sys.exit(1)


def progress_bar(current, total, width=40, label="", mode="bytes"):
    if total == 0:
        return
    pct = current / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    if mode == "bytes":
        size_mb = current / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        detail = f"{size_mb:.0f}/{total_mb:.0f} MB"
    else:
        detail = f"{int(pct * 100)}%"
    sys.stdout.write(f"\r  ▐{bar}▌ {pct:>6.1%}  {detail}  {label}")
    sys.stdout.flush()
    if current >= total:
        print()


# ─── Adobe API ────────────────────────────────────────────────────────────────

def api_request(url, headers=None, timeout=60, retries=2):
    """Make an HTTP GET request with retry. Returns response body as string."""
    hdrs = {**ADOBE_HEADERS, **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                log(f"Attempt {attempt + 1} failed, retrying...", "warn")
                time.sleep(2)
            else:
                log(f"API request failed after {retries + 1} attempts: {e}", "warn")
                return None


def fetch_products(api_version="v4"):
    """Fetch and parse products from Adobe's API."""
    url_template = ADOBE_API_URLS.get(api_version)
    if not url_template:
        fatal(f"Unknown API version: {api_version}")

    url = url_template.format(platform=PLATFORM_STRING)
    # v4 returns all versions (back to CS6) and is a much larger response
    timeout = 120 if api_version == "v4" else 60
    log(f"Querying Adobe API ({api_version})...", "step")

    body = api_request(url, timeout=timeout)
    if not body:
        return None, None

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, None

    # Parse based on API version
    prefix = "channels/" if api_version == "v6" else ""
    cdn_el = root.find(f"{prefix}channel/cdn/secure")
    cdn = cdn_el.text if cdn_el is not None else CDN_BASE

    products = {}
    for p in root.findall(f"{prefix}channel/products/product"):
        sap = p.get("id")
        display = p.find("displayName")
        display_name = display.text if display is not None else sap
        version = p.get("version")

        if sap not in products:
            products[sap] = {"name": display_name, "sap": sap, "versions": OrderedDict()}

        for pf in p.findall("platforms/platform"):
            app_platform = pf.get("id")
            lang_set = pf.find("languageSet")
            if lang_set is None:
                continue
            base_version = lang_set.get("baseVersion")
            build_guid = lang_set.get("buildGuid")
            deps = [
                {"sap": d.find("sapCode").text, "version": d.find("baseVersion").text}
                for d in lang_set.findall("dependencies/dependency")
                if d.find("sapCode") is not None and d.find("baseVersion") is not None
            ]

            products[sap]["versions"][version] = {
                "version": version,
                "base_version": base_version,
                "build_guid": build_guid,
                "platform": app_platform,
                "dependencies": deps,
            }

    return products, cdn


def find_working_api():
    """Try API versions until one works. Returns (products, cdn, api_version)."""
    # v4 has the most versions (back to CS6), try it first
    tried = []
    for ver in ["v4", "v6", "v5"]:
        products, cdn = fetch_products(ver)
        if products:
            if ver != "v4" and "v4" in tried:
                log(f"Connected to Adobe API ({ver}). Note: v4 (which has older versions) was unavailable.", "ok")
                log("Retry with --api v4 if you need versions older than what's shown.", "info")
            else:
                log(f"Connected to Adobe API ({ver})", "ok")
            return products, cdn, ver
        tried.append(ver)

    print()
    log("All Adobe API endpoints failed.", "error")
    log("This likely means Adobe changed their API endpoints.", "warn")
    print()
    print("  To find the new endpoints, paste this script's source into an AI")
    print("  assistant and ask it to search for the current Adobe FFC API URLs.")
    print()
    print("  You can also set a custom API URL:")
    print("    CC_INSTALL_API_URL=https://... python3 cc_install.py")
    print()
    fatal("Cannot continue without API access.")


# ─── Downloader ───────────────────────────────────────────────────────────────

def get_application_json(build_guid):
    """Fetch application.json for a specific build."""
    headers = {**ADOBE_HEADERS, "x-adobe-build-guid": build_guid}
    body = api_request(APPLICATION_JSON_URL, headers)
    if body:
        return json.loads(body)
    return None


def download_file(url, dest_path, label=""):
    """Download a file with progress bar. Supports resume."""
    headers = {"User-Agent": "Creative Cloud"}
    existing_size = 0

    if os.path.exists(dest_path):
        existing_size = os.path.getsize(dest_path)
        # Check remote size
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                remote_size = int(resp.headers.get("content-length", 0))
                if existing_size == remote_size and remote_size > 0:
                    return True  # Already complete
        except:
            pass
        headers["Range"] = f"bytes={existing_size}-"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("content-length", 0)) + existing_size
            mode = "ab" if existing_size > 0 else "wb"
            downloaded = existing_size

            with open(dest_path, mode) as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    progress_bar(downloaded, total, label=label)

        return True
    except Exception as e:
        log(f"Download failed: {e}", "error")
        return False


def download_packages(product_info, products, cdn, dest_dir, language="en_US"):
    """Download all packages for a product and its dependencies."""
    sap = product_info["sap_code"]
    version_info = product_info["version_info"]

    # Build list of things to download
    to_download = [
        {"sap": sap, "version": version_info["version"], "guid": version_info["build_guid"]}
    ]
    for dep in version_info["dependencies"]:
        dep_sap = dep["sap"]
        dep_ver = dep["version"]
        if dep_sap in products:
            # Find matching build guid
            for v_info in products[dep_sap]["versions"].values():
                if v_info.get("base_version") == dep_ver and v_info.get("build_guid"):
                    to_download.append({"sap": dep_sap, "version": dep_ver, "guid": v_info["build_guid"]})
                    break

    log(f"Downloading {len(to_download)} components...", "step")
    os.makedirs(dest_dir, exist_ok=True)

    for item in to_download:
        s, v, guid = item["sap"], item["version"], item["guid"]
        pkg_dir = os.path.join(dest_dir, s)
        os.makedirs(pkg_dir, exist_ok=True)

        # Download application.json
        json_path = os.path.join(pkg_dir, "application.json")
        if not os.path.exists(json_path):
            app_json = get_application_json(guid)
            if app_json:
                with open(json_path, "w") as f:
                    json.dump(app_json, f, separators=(",", ":"))
            else:
                log(f"Failed to get application.json for {s} {v}", "warn")
                continue
        else:
            with open(json_path) as f:
                app_json = json.load(f)

        # Download packages
        packages = app_json.get("Packages", {}).get("Package", [])
        for pkg in packages:
            is_core = pkg.get("Type") == "core"
            condition = pkg.get("Condition", "")
            lang_ok = (
                language == "ALL"
                or "Condition" not in pkg
                or "[installLanguage]" not in condition
                or f"[installLanguage]=={language}" in condition
            )

            if is_core or lang_ok:
                url = cdn + pkg["Path"]
                name = url.split("/")[-1].split("?")[0]
                file_path = os.path.join(pkg_dir, name)

                if not download_file(url, file_path, label=f"{s}/{name}"):
                    log(f"Failed to download {name}", "error")
                    return False

    log("All packages downloaded", "ok")
    return True


# ─── Driver XML ───────────────────────────────────────────────────────────────

def generate_driver_xml(product_info):
    """Generate driver.xml for installation."""
    vi = product_info["version_info"]
    deps_xml = "\n".join(
        DEPENDENCY_TEMPLATE.format(sap_code=d["sap"], version=d["version"])
        for d in vi["dependencies"]
    )
    return DRIVER_XML_TEMPLATE.format(
        name=product_info["name"],
        sap_code=product_info["sap_code"],
        version=vi["version"],
        platform=vi["platform"],
        dependencies=deps_xml,
        language=product_info["language"],
    )


# ─── HDBox IPC Protocol ──────────────────────────────────────────────────────

class HDBoxInstaller:
    """Communicates with Adobe's HDBox/Setup via named pipe IPC."""

    HEADER_FLAGS = b"\x0a\x00\x00\x00\xff\xff\x00\x00"
    HEADER_SIZE = 12

    def __init__(self, password):
        self.password = password
        self.pipe_name = f"cc_install_{os.getpid()}"
        self.pipe_in = f"/tmp/{self.pipe_name}_IN"   # Setup WRITES here
        self.pipe_out = f"/tmp/{self.pipe_name}_OUT"  # Setup READS here
        self.fd_in = None
        self.fd_out = None
        self.proc = None

    def cleanup(self):
        if self.fd_in is not None:
            try: os.close(self.fd_in)
            except: pass
        if self.fd_out is not None:
            try: os.close(self.fd_out)
            except: pass
        for p in [self.pipe_in, self.pipe_out]:
            try: os.unlink(p)
            except: pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(timeout=5)
            except: self.proc.kill()

    def _write_packet(self, data_bytes):
        header = self.HEADER_FLAGS + struct.pack("<I", len(data_bytes))
        os.write(self.fd_out, header + data_bytes)

    def _send_message(self, xml_str):
        self._write_packet(xml_str.encode("utf-8") + b"\x00")

    def _read_response(self, timeout=30):
        """Read a framed response from Setup. Returns parsed XML string or None."""
        end = time.time() + timeout
        buf = b""

        while time.time() < end:
            # Check if process exited
            if self.proc and self.proc.poll() is not None:
                return None
            try:
                r, _, _ = select.select([self.fd_in], [], [], 1.0)
                if r:
                    chunk = os.read(self.fd_in, 65536)
                    if chunk:
                        buf += chunk
                        # Try to parse a complete packet
                        if len(buf) >= self.HEADER_SIZE:
                            data_len = struct.unpack("<I", buf[8:12])[0]
                            if len(buf) >= self.HEADER_SIZE + data_len:
                                data = buf[self.HEADER_SIZE:self.HEADER_SIZE + data_len]
                                buf = buf[self.HEADER_SIZE + data_len:]
                                return data.decode("utf-8", errors="replace").rstrip("\x00")
            except (BlockingIOError, OSError):
                pass

        return None

    def _check_conflicting_processes(self):
        """Check for running Adobe apps that might conflict."""
        try:
            result = subprocess.run(
                ["pgrep", "-fl", "After Effects|Premiere|Photoshop|Illustrator|Media Encoder"],
                capture_output=True, text=True,
            )
            if result.stdout.strip():
                procs = set()
                for line in result.stdout.strip().split("\n"):
                    for app in ["After Effects", "Premiere", "Photoshop", "Illustrator", "Media Encoder"]:
                        if app in line and "crashpad" not in line:
                            procs.add(app)
                if procs:
                    return list(procs)
        except:
            pass
        return []

    def install(self, driver_xml_path, working_dir):
        """Run the full installation via IPC protocol."""

        # Pre-flight checks
        if not os.path.exists(SETUP_PATH):
            fatal("Adobe Creative Cloud is not installed (HDBox/Setup not found).\n"
                  "    Install Creative Cloud first: https://creativecloud.adobe.com/apps/download/creative-cloud")

        conflicts = self._check_conflicting_processes()
        if conflicts:
            fatal(f"Close these Adobe apps before installing: {', '.join(conflicts)}")

        # Clean up old pipes
        for p in [self.pipe_in, self.pipe_out]:
            try: os.unlink(p)
            except: pass

        # Create and open pipes
        os.mkfifo(self.pipe_in, 0o666)
        os.mkfifo(self.pipe_out, 0o666)
        os.chmod(self.pipe_in, 0o666)
        os.chmod(self.pipe_out, 0o666)

        self.fd_in = os.open(self.pipe_in, os.O_RDWR | os.O_NONBLOCK)
        self.fd_out = os.open(self.pipe_out, os.O_RDWR | os.O_NONBLOCK)

        # Start Setup
        log("Starting Adobe HyperDrive installer...", "step")
        self.proc = subprocess.Popen(
            ["sudo", "-S", SETUP_PATH, "--install=1",
             f"--driverXML={driver_xml_path}", f"--pipeName={self.pipe_name}"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=working_dir,
        )
        self.proc.stdin.write((self.password + "\n").encode())
        self.proc.stdin.flush()

        time.sleep(4)
        if self.proc.poll() is not None:
            stderr = self.proc.stderr.read().decode(errors="replace")
            if "incorrect password" in stderr.lower() or "Sorry" in stderr:
                fatal("Incorrect password.")
            fatal(f"Setup exited immediately (code {self.proc.returncode})")

        # Step 1: Create session
        log("Creating installation session...", "step")
        self._send_message(
            "<HDSetupMsg><MsgName>hdpimCreateSession</MsgName>"
            "<MsgData></MsgData></HDSetupMsg>"
        )

        resp = self._read_response()
        if not resp:
            self.cleanup()
            fatal("No response from Setup. The IPC protocol may have changed.\n"
                  "    Check for updates to this tool.")

        session_match = re.search(r"<sessionID>([^<]+)</sessionID>", resp)
        status_match = re.search(r"<status>([^<]+)</status>", resp)

        if not session_match or (status_match and status_match.group(1) != "success"):
            self.cleanup()
            error_match = re.search(r"<errorCode>([^<]+)</errorCode>", resp)
            code = error_match.group(1) if error_match else "unknown"
            fatal(f"Failed to create session (error {code})")

        session_id = session_match.group(1)
        log(f"Session: {session_id}", "ok")

        # Step 2: Install product
        with open(driver_xml_path) as f:
            driver_content = f.read()

        log("Installing packages...", "step")
        self._send_message(
            f"<HDSetupMsg><MsgName>hdpimInstallProduct</MsgName><MsgData>"
            f"<sessionID>{session_id}</sessionID>"
            f"<DeployInfo>{driver_content}</DeployInfo>"
            f"</MsgData></HDSetupMsg>"
        )

        # Monitor progress
        last_pct = -1
        self._install_complete = False
        start = time.time()

        while time.time() - start < 600:  # 10 min timeout
            if self.proc.poll() is not None:
                break

            resp = self._read_response(timeout=5)
            if not resp:
                continue

            msg_name = re.search(r"<MsgName>([^<]+)</MsgName>", resp)
            status = re.search(r"<status>([^<]+)</status>", resp)
            error = re.search(r"<errorCode>([^<]+)</errorCode>", resp)
            pct = re.search(r"<installPercentage>([^<]+)</installPercentage>", resp)

            if pct:
                p = float(pct.group(1))
                ip = int(p)
                if ip != last_pct:
                    last_pct = ip
                    progress_bar(ip, 100, label="Installing", mode="pct")
                if p >= 100.0:
                    self._install_complete = True
                    time.sleep(2)
                    try:
                        self._send_message(
                            f"<HDSetupMsg><MsgName>hdpimTerminateSession</MsgName><MsgData>"
                            f"<sessionID>{session_id}</sessionID>"
                            f"</MsgData></HDSetupMsg>"
                        )
                    except:
                        pass
                    time.sleep(3)
                    break

            if msg_name and "Response" in msg_name.group(1):
                if status and status.group(1) == "fail":
                    ec = error.group(1) if error else "?"
                    # Check for specific errors
                    conflict = re.search(r"<process[^>]*>([^<]+)</process>", resp)
                    if conflict:
                        self.cleanup()
                        fatal(f"Conflicting process: {conflict.group(1)}. Close it and retry.")
                    esd = re.search(r"EsdDirectory '([^']+)' .* does not exist", resp)
                    if esd:
                        self.cleanup()
                        fatal(f"Package directory not found: {esd.group(1)}")
                    self.cleanup()
                    fatal(f"Installation failed (error {ec})")

            # Check for conflicting processes error in progress callbacks
            if "<conflictingProcesses>" in resp:
                conflict = re.search(r"<process[^>]*>([^<]+)</process>", resp)
                if conflict:
                    self.cleanup()
                    fatal(f"Conflicting process detected: {conflict.group(1)}. Close it and retry.")

        # Wait for process to exit
        if self.proc.poll() is None:
            log("Finalizing...", "step")
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except:
                    self.proc.kill()

        self.cleanup()
        # Setup often needs to be terminated after install completes (it stays in pipe loop)
        # Treat SIGTERM (-15) as success if install reached 100%
        rc = self.proc.returncode or 0
        if self._install_complete and rc == -15:
            rc = 0
        return rc


# ─── Interactive CLI ──────────────────────────────────────────────────────────

def pick_from_list(items, prompt, default=None):
    """Interactive selection from a list."""
    for i, item in enumerate(items):
        marker = " (default)" if item == default else ""
        print(f"    {i+1:>3}. {item}{marker}")
    print()
    while True:
        choice = input(f"  {prompt} [{default or ''}]: ").strip()
        if not choice and default:
            return default
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return items[idx]
        except ValueError:
            # Try matching by name
            matches = [it for it in items if choice.upper() in it.upper()]
            if len(matches) == 1:
                return matches[0]
        print("    Invalid choice, try again.")


def interactive_mode(products, cdn):
    """Guide the user through product/version selection."""
    is_arm = platform.machine() == "arm64"
    allowed_platforms = ["macuniversal"] + (["macarm64"] if is_arm else ["osx10-64", "osx10"])

    # Filter to visible products with downloadable versions
    visible = {}
    for sap, prod in products.items():
        for v in prod["versions"].values():
            if v.get("build_guid") and v.get("platform") in allowed_platforms:
                visible[sap] = prod["name"]
                break

    # Product selection
    print()
    sap_list = sorted(visible.keys())
    display = [f"{s:<10} {visible[s]}" for s in sap_list]
    print(f"  {len(sap_list)} products available:\n")
    for d in display:
        print(f"    {d}")
    print()

    while True:
        sap_input = input("  Enter SAP code (e.g. AEFT for After Effects): ").strip().upper()
        if sap_input in visible:
            break
        matches = [s for s in sap_list if sap_input in s or sap_input in visible[s].upper()]
        if len(matches) == 1:
            sap_input = matches[0]
            break
        print("    Not found. Try again.")

    sap_code = sap_input
    product = products[sap_code]

    # Version selection
    available = []
    for v in product["versions"].values():
        if v.get("build_guid") and v.get("platform") in allowed_platforms:
            available.append(v["version"])

    print(f"\n  Available versions of {product['name']}:\n")
    # Show in reverse (newest first), but limit display
    for v in available[:30]:
        print(f"    {v}")
    if len(available) > 30:
        print(f"    ... and {len(available) - 30} more")
    print()

    while True:
        ver_input = input(f"  Enter version [{available[0]}]: ").strip()
        if not ver_input:
            ver_input = available[0]
        if ver_input in product["versions"]:
            break
        print("    Version not found. Try again.")

    version_info = product["versions"][ver_input]

    # Language
    print(f"\n  Language [{LANGUAGES[0]}]: ", end="")
    lang = input().strip() or LANGUAGES[0]
    if lang not in LANGUAGES:
        log(f"Unknown language '{lang}', using en_US", "warn")
        lang = "en_US"

    return {
        "name": product["name"],
        "sap_code": sap_code,
        "version_info": version_info,
        "language": lang,
    }


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Install any version of Adobe Creative Cloud apps on macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python3 cc_install.py                          # Interactive mode\n"
               "  python3 cc_install.py --list                   # List products\n"
               "  python3 cc_install.py --list AEFT              # List AE versions\n"
               "  python3 cc_install.py -s AEFT -v 22.6          # Install AE 2022\n",
    )
    parser.add_argument("-s", "--sap-code", help="SAP code (e.g. AEFT, PHSP, PPRO)")
    parser.add_argument("-v", "--version", help="Product version (e.g. 22.6)")
    parser.add_argument("-l", "--language", default="en_US", help="Install language (default: en_US)")
    parser.add_argument("-d", "--dest", help="Download directory (default: temp)")
    parser.add_argument("--list", nargs="?", const="ALL", metavar="SAP_CODE",
                        help="List available products, or versions of a specific product")
    parser.add_argument("--api", default="v4", choices=["v4", "v5", "v6"],
                        help="API version (default: v4, has most versions)")
    parser.add_argument("--download-only", action="store_true", help="Download without installing")
    parser.add_argument("--version-info", action="version", version=f"cc-install {VERSION}")
    args = parser.parse_args()

    # Banner
    c = Colors if Colors.supports_color() else type("C", (), {k: "" for k in dir(Colors)})
    print(f"\n  {c.BOLD}cc-install{c.RESET} {c.DIM}v{VERSION}{c.RESET}")
    print(f"  {c.DIM}Install any version of Adobe Creative Cloud apps{c.RESET}\n")

    # Platform check
    if sys.platform != "darwin":
        fatal("This tool only works on macOS.")

    # Custom API URL
    custom_url = os.environ.get("CC_INSTALL_API_URL")
    if custom_url:
        ADOBE_API_URLS["custom"] = custom_url
        args.api = "custom"

    # Fetch products
    products, cdn, api_ver = find_working_api()

    # List mode
    if args.list:
        is_arm = platform.machine() == "arm64"
        allowed = ["macuniversal"] + (["macarm64"] if is_arm else ["osx10-64", "osx10"])

        if args.list != "ALL" and args.list.upper() in products:
            sap = args.list.upper()
            prod = products[sap]
            print(f"  {prod['name']} ({sap}):\n")
            for v in prod["versions"].values():
                if v.get("build_guid") and v.get("platform") in allowed:
                    print(f"    {v['version']:>12}  {v['platform']}")
        else:
            for sap in sorted(products.keys()):
                prod = products[sap]
                has_version = any(
                    v.get("build_guid") and v.get("platform") in allowed
                    for v in prod["versions"].values()
                )
                if has_version:
                    print(f"    {sap:<10} {prod['name']}")
        print()
        return

    # Select product (interactive or from args)
    if args.sap_code and args.version:
        sap = args.sap_code.upper()
        if sap not in products:
            fatal(f"Unknown SAP code: {sap}")
        if args.version not in products[sap]["versions"]:
            fatal(f"Version {args.version} not found for {sap}")
        product_info = {
            "name": products[sap]["name"],
            "sap_code": sap,
            "version_info": products[sap]["versions"][args.version],
            "language": args.language,
        }
    else:
        product_info = interactive_mode(products, cdn)

    vi = product_info["version_info"]
    print(f"\n  {c.BOLD}Installing:{c.RESET} {product_info['name']} {vi['version']}")
    print(f"  {c.DIM}Platform: {vi['platform']} | Language: {product_info['language']}{c.RESET}\n")

    # Set up download directory
    if args.dest:
        dest_dir = args.dest
    else:
        dest_dir = os.path.join(tempfile.gettempdir(), f"cc-install-{product_info['sap_code']}-{vi['version']}")
    os.makedirs(dest_dir, exist_ok=True)

    # Download
    if not download_packages(product_info, products, cdn, dest_dir, product_info["language"]):
        fatal("Download failed.")

    # Generate driver.xml
    driver_xml = generate_driver_xml(product_info)
    driver_path = os.path.join(dest_dir, "driver.xml")
    with open(driver_path, "w") as f:
        f.write(driver_xml)
    log("Generated driver.xml", "ok")

    if args.download_only:
        log(f"Packages saved to: {dest_dir}", "ok")
        return

    # Get password (env var for CI/automation, otherwise interactive prompt)
    password = os.environ.get("CC_INSTALL_PASSWORD")
    if not password:
        print()
        password = getpass.getpass("  Enter your macOS password (for sudo): ")
        print()

    # Verify sudo
    verify = subprocess.run(
        ["sudo", "-S", "-v"],
        input=(password + "\n").encode(),
        capture_output=True,
    )
    if verify.returncode != 0:
        fatal("Incorrect password.")
    log("Password verified", "ok")

    # Install
    installer = HDBoxInstaller(password)
    try:
        exit_code = installer.install(driver_path, dest_dir)
    except KeyboardInterrupt:
        installer.cleanup()
        fatal("Interrupted.")
    except Exception as e:
        installer.cleanup()
        fatal(f"Installation error: {e}")

    print()
    if exit_code == 0:
        log(f"{product_info['name']} {vi['version']} installed successfully!", "ok")
        # Find the actual install directory
        major = vi['version'].split('.')[0]
        year = 2000 + int(major) if int(major) < 50 else int(major)
        expected = f"/Applications/Adobe {product_info['name']} {year}"
        if os.path.isdir(expected):
            log(f"Location: {expected}/", "info")
        else:
            log("Check /Applications/ for the installed app.", "info")
    else:
        log(f"Setup exited with code {exit_code}. Check /Library/Logs/Adobe/Installers/Install.log", "warn")

    print()


if __name__ == "__main__":
    main()
