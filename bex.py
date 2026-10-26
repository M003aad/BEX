#!/usr/bin/env python3
"""BEX (Bot Extension): sequentially load unpacked extensions in regular Chrome."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_FILE = ROOT / "bex.txt"
DEFAULT_DEST = Path("~/bex/extensions").expanduser()
STATE_FILE = Path("~/bex/state.json").expanduser()
ASSETS = ROOT / "assets"
BRAVE_COORDINATES = ROOT / "coordinates.json"


@dataclass(frozen=True)
class Browser:
    key: str
    label: str
    executable_names: tuple[str, ...]
    windows_paths: tuple[str, ...]
    mac_path: str
    linux_paths: tuple[str, ...]
    profile_windows: str
    profile_mac: str
    profile_linux: str


BROWSERS = {
    "chrome": Browser(
        "chrome", "Google Chrome", ("chrome.exe", "Google Chrome"),
        ("Google/Chrome/Application/chrome.exe",),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ("/usr/bin/google-chrome", "/usr/bin/chromium"),
        "Google/Chrome/User Data",
        "~/Library/Application Support/Google/Chrome",
        "~/.config/google-chrome",
    ),
    "edge": Browser(
        "edge", "Microsoft Edge", ("msedge.exe", "Microsoft Edge"),
        ("Microsoft/Edge/Application/msedge.exe",),
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ("/usr/bin/microsoft-edge",),
        "Microsoft/Edge/User Data",
        "~/Library/Application Support/Microsoft Edge",
        "~/.config/microsoft-edge",
    ),
    "brave": Browser(
        "brave", "Brave", ("brave.exe", "Brave Browser"),
        ("BraveSoftware/Brave-Browser/Application/brave.exe",),
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ("/usr/bin/brave-browser",),
        "BraveSoftware/Brave-Browser/User Data",
        "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "~/.config/BraveSoftware/Brave-Browser",
    ),
}


# ---------------------------------------------------------------- download

def repo_details(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.lower() != "github.com" or len(parts) < 2:
        raise ValueError("expected https://github.com/<user>/<repo>")
    return parts[0], parts[1].removesuffix(".git")


def download_repo(owner: str, repo: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for branch in ("main", "master"):
        url = (
            f"https://github.com/{urllib.parse.quote(owner)}/"
            f"{urllib.parse.quote(repo)}/archive/refs/heads/{branch}.zip"
        )
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                destination.write_bytes(response.read())
            return
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            last_error = exc
            if destination.exists():
                destination.unlink()
    raise RuntimeError(f"main and master downloads failed: {last_error}")


def extract_flat(archive: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        members = [item for item in source.infolist() if item.filename]
        if not members:
            raise ValueError("downloaded ZIP is empty")
        for item in members:
            name = item.filename.replace("\\", "/")
            relative = name.split("/", 1)[1] if "/" in name else ""
            if not relative:
                continue
            target = (destination / relative).resolve()
            if root not in target.parents:
                raise ValueError(f"unsafe ZIP path: {item.filename}")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as input_file, target.open("wb") as output:
                    shutil.copyfileobj(input_file, output)


def resolve_message(folder: Path, manifest: dict[str, Any], raw: str) -> str:
    """Resolve names like __MSG_extName__ using _locales/<locale>/messages.json."""
    key = raw[6:-2].casefold()
    locales = [manifest.get("default_locale"), "en", "en_US"]
    for locale in locales:
        if not locale:
            continue
        path = folder / "_locales" / locale / "messages.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for k, v in data.items():
            if k.casefold() == key and isinstance(v, dict) and v.get("message"):
                return str(v["message"])
    return raw


def validate_manifest(folder: Path) -> tuple[str, str]:
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("manifest.json is missing from the extension root")
    try:
        manifest: dict[str, Any] = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest.json is invalid JSON: {exc}") from exc
    if manifest.get("manifest_version") != 3:
        raise ValueError("manifest.json must use manifest_version 3")
    name, version = manifest.get("name"), manifest.get("version")
    if isinstance(name, str) and name.startswith("__MSG_") and name.endswith("__"):
        name = resolve_message(folder, manifest, name)
    if not isinstance(name, str) or not name:
        raise ValueError("manifest.json has no valid name")
    if not isinstance(version, str) or not version:
        raise ValueError("manifest.json has no valid version")
    return name, version


# ------------------------------------------------------------------- state

def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def folder_fingerprint(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in folder.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(folder)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# ----------------------------------------------------------------- browser

def browser_running(browser: Browser) -> bool:
    if platform.system() == "Windows":
        result = subprocess.run(
            ["tasklist"], capture_output=True, text=True, check=False
        )
        output = result.stdout.lower()
        return any(
            name.lower() in output
            for name in browser.executable_names
            if name.endswith(".exe")
        )
    return any(
        subprocess.run(
            ["pgrep", "-x", name],
            stdout=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        for name in browser.executable_names
    )


def wait_for_browser_close(browser: Browser, timeout: int = 20) -> None:
    print(f"Close {browser.label} now. BEX will continue automatically.")
    deadline = time.monotonic() + timeout
    while browser_running(browser) and time.monotonic() < deadline:
        time.sleep(1)
    if browser_running(browser) and platform.system() == "Windows":
        # Chrome/Edge keep background processes alive after the window closes.
        print("Background processes still running, forcing close...")
        for name in browser.executable_names:
            if name.endswith(".exe"):
                subprocess.run(
                    ["taskkill", "/F", "/IM", name],
                    capture_output=True,
                    check=False,
                )
        time.sleep(2)
    if browser_running(browser):
        raise RuntimeError(f"{browser.label} is still running")
    print(f"{browser.label} is closed. Continuing...")


def browser_executable(browser: Browser) -> Path:
    candidates: list[Path] = []
    system = platform.system()
    if system == "Windows":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.extend(Path(base) / path for path in browser.windows_paths)
    elif system == "Darwin":
        candidates.append(Path(browser.mac_path))
    else:
        candidates.extend(Path(path) for path in browser.linux_paths)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for name in browser.executable_names:
        found = shutil.which(name)
        if found:
            return Path(found)
    raise RuntimeError(f"{browser.label} executable was not found")


def profile_directory(browser: Browser) -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ["LOCALAPPDATA"]) / browser.profile_windows
    if system == "Darwin":
        return Path(browser.profile_mac).expanduser()
    return Path(browser.profile_linux).expanduser()


def enable_developer_mode(profile: Path) -> None:
    """Must be called while the browser is CLOSED (it rewrites Preferences on exit)."""
    preferences = profile / "Default" / "Preferences"
    if not preferences.is_file():
        raise RuntimeError(f"Preferences not found: {preferences}")
    backup = preferences.with_name("Preferences.bex-backup")
    if not backup.exists():
        shutil.copy2(preferences, backup)
    data = json.loads(preferences.read_text(encoding="utf-8"))
    extensions = data.setdefault("extensions", {})
    extensions.setdefault("ui", {})["developer_mode"] = True
    preferences.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


# ------------------------------------------------------------ GUI helpers

def find_asset(name: str, timeout: float = 3):
    image = ASSETS / name
    if not image.is_file():
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            location = pyautogui.locateOnScreen(str(image))
        except Exception:  # ImageNotFoundException on newer pyautogui
            location = None
        if location:
            return location
        time.sleep(0.4)
    return None


def click_asset(name: str, timeout: float = 12) -> bool:
    location = find_asset(name, timeout)
    if location:
        pyautogui.click(pyautogui.center(location))
        return True
    return False


def calibrated_position(name: str) -> tuple[int, int] | None:
    if not BRAVE_COORDINATES.is_file():
        return None
    try:
        data = json.loads(BRAVE_COORDINATES.read_text(encoding="utf-8"))
        position = data[name]
        if (
            isinstance(position, list)
            and len(position) == 2
            and all(isinstance(value, int) for value in position)
        ):
            return position[0], position[1]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def click_address_bar() -> None:
    position = calibrated_position("address_bar")
    if position is None:
        screen_width, _ = pyautogui.size()
        position = (screen_width // 2, 50)
    pyautogui.click(*position)
    time.sleep(0.5)


def click_load_unpacked() -> None:
    position = calibrated_position("load_unpacked")
    if position is not None:
        print(f"Clicking Load unpacked at x={position[0]}, y={position[1]}")
        pyautogui.moveTo(*position, duration=0.3)
        pyautogui.click()
        time.sleep(2)
        return
    if not (ASSETS / "load_unpacked.png").is_file():
        raise RuntimeError(
            "missing assets/load_unpacked.png. Run `python bex.py --calibrate` "
            "with the selected browser open on the extensions page."
        )
    if not click_asset("load_unpacked.png"):
        raise RuntimeError(
            "could not find the Load unpacked button. Run "
            "`python bex.py --calibrate` again with the browser maximized."
        )
    time.sleep(1)


def ensure_developer_mode_on_screen(browser: Browser) -> None:
    has_on = (ASSETS / "developer_mode_on.png").is_file()
    has_off = (ASSETS / "developer_mode_off.png").is_file()
    if not (has_on or has_off):
        print(
            "Warning: no developer_mode images found, assuming Developer mode "
            "is already ON (set by Preferences)."
        )
        return
    if has_on and find_asset("developer_mode_on.png", 3):
        return
    if has_off and click_asset("developer_mode_off.png", 3):
        time.sleep(1)
        return
    raise RuntimeError(
        f"Can't detect Developer mode state in {browser.label}. "
        "Run `python bex.py --calibrate`."
    )


def copy_page_text() -> str:
    pyperclip.copy("")
    pyautogui.hotkey("ctrl", "a")
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.4)
    return pyperclip.paste()


def extension_registered(browser: Browser, folder: Path, timeout: float = 12) -> bool:
    """Check the browser profile files for this unpacked extension path."""
    target = os.path.normcase(os.path.normpath(str(folder.resolve())))
    profile = profile_directory(browser) / "Default"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for filename in ("Secure Preferences", "Preferences"):
            try:
                data = json.loads((profile / filename).read_text(encoding="utf-8"))
                settings = data.get("extensions", {}).get("settings", {})
            except (OSError, ValueError):
                continue
            for entry in settings.values():
                path = entry.get("path") if isinstance(entry, dict) else None
                if path and os.path.normcase(os.path.normpath(path)) == target:
                    return True
        time.sleep(1)
    return False


def load_extension(folder: Path, name: str, browser: Browser) -> str:
    click_load_unpacked()
    time.sleep(2.5)
    pyperclip.copy(str(folder.resolve()))
    if platform.system() == "Darwin":
        pyautogui.hotkey("command", "shift", "g")
        time.sleep(0.8)
        pyautogui.hotkey("command", "v")
        pyautogui.press("enter")
        time.sleep(0.8)
        pyautogui.press("enter")
    else:
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.5)
        pyautogui.press("enter")
        time.sleep(0.8)
        pyautogui.press("enter")
    time.sleep(2.5)
    if extension_registered(browser, folder):
        print(f"Loaded {name} (confirmed in profile)")
        return "loaded"
    expected = name.casefold()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if expected in copy_page_text().casefold():
            print(f"Loaded {name} (confirmed on page)")
            return "loaded"
        time.sleep(0.5)
    print(
        f"Warning: could not confirm {name}. The folder dialog finished, so it "
        "is probably loaded, check chrome://extensions manually."
    )
    return "unverified"


def capture_control(name: str) -> None:
    x, y = pyautogui.position()
    width, height = 260, 120
    left = max(0, x - width // 2)
    top = max(0, y - height // 2)
    pyautogui.screenshot(str(ASSETS / name), region=(left, top, width, height))


def calibrate() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    print("Maximize the selected browser and open its extensions page.")
    input("Set Developer mode OFF, place the mouse over the toggle, then press Enter: ")
    capture_control("developer_mode_off.png")
    input("Set Developer mode ON, place the mouse over the toggle, then press Enter: ")
    capture_control("developer_mode_on.png")
    input("Place the mouse over the Load unpacked button, then press Enter: ")
    capture_control("load_unpacked.png")
    print(f"Saved calibration screenshots in {ASSETS}")


# -------------------------------------------------------------- navigation

def maximize_browser() -> None:
    if platform.system() == "Windows":
        pyautogui.hotkey("win", "up")
    elif platform.system() == "Darwin":
        pyautogui.hotkey("command", "control", "f")
    else:
        pyautogui.press("f11")
    time.sleep(2)


def navigate_to_extensions_page(browser: Browser) -> None:
    extensions_url = (
        "brave://extensions" if browser.key == "brave" else
        "edge://extensions" if browser.key == "edge" else
        "chrome://extensions"
    )
    print(f"Opening {extensions_url} in {browser.label}...")
    print("You have 5 seconds to move the mouse or adjust the browser window.")
    for remaining in range(5, 0, -1):
        print(f"{remaining}...")
        time.sleep(1)
    modifier = "command" if platform.system() == "Darwin" else "ctrl"
    click_address_bar()
    pyautogui.hotkey(modifier, "a")
    pyperclip.copy(extensions_url)
    pyautogui.hotkey(modifier, "v")
    pyautogui.press("enter")
    time.sleep(4)


def choose_browser(requested: str | None) -> Browser:
    if requested:
        return BROWSERS[requested]
    print("Choose a browser:")
    choices = list(BROWSERS.values())
    for index, browser in enumerate(choices, 1):
        print(f"  {index}. {browser.label}")
    answer = input("Browser [1]: ").strip() or "1"
    try:
        return choices[int(answer) - 1]
    except (ValueError, IndexError) as exc:
        raise RuntimeError("invalid browser choice") from exc


def prepare_browser(browser: Browser) -> None:
    if browser_running(browser):
        answer = input(
            f"{browser.label} is running. Close it now so BEX can use your "
            "regular profile? [y/N] "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            print(f"Using the existing {browser.label} window.")
            maximize_browser()
            navigate_to_extensions_page(browser)
            return  # main() checks developer mode afterwards
        wait_for_browser_close(browser)

    # Browser is closed here, so Preferences edits will stick.
    enable_developer_mode(profile_directory(browser))
    subprocess.Popen([str(browser_executable(browser))])
    time.sleep(6)
    maximize_browser()
    navigate_to_extensions_page(browser)


# -------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bex.py",
        description="BEX: load GitHub Chrome extensions into your regular browser.",
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument(
        "--browser",
        choices=tuple(BROWSERS),
        help="browser to use; without it BEX asks at startup",
    )
    args = parser.parse_args()

    global pyautogui, pyperclip
    try:
        import pyautogui
        import pyperclip
    except ImportError:
        print("Missing dependency. Install it with: pip install pyautogui pyperclip")
        return
    pyautogui.FAILSAFE = True

    try:
        browser = choose_browser(args.browser)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return
    print(f"Selected browser: {browser.label}")

    if args.calibrate:
        calibrate()
        return
    if not args.file.is_file():
        print(f"Error: source file not found: {args.file}")
        return

    urls = [
        line.strip()
        for line in args.file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    print("Do not touch mouse/keyboard until done.")
    for remaining in range(5, 0, -1):
        print(f"{remaining}...")
        time.sleep(1)

    dest = args.dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    state = load_state()
    archive_dir = dest / ".bex-downloads"
    archive_dir.mkdir(exist_ok=True)

    status: dict[str, str] = {}
    versions: dict[str, str] = {}
    pending: list[tuple[str, str, Path, str, str]] = []

    for url in urls:
        repo = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        versions[repo] = "-"
        try:
            owner, repo = repo_details(url)
            versions.setdefault(repo, "-")
            folder = dest / repo
            archive = archive_dir / f"{repo}.zip"
            print(f"\n[{repo}] Downloading...")
            download_repo(owner, repo, archive)
            print(f"[{repo}] Downloaded")
            extract_flat(archive, folder)
            print(f"[{repo}] Extracted")
            name, version = validate_manifest(folder)
            print(f"[{repo}] Validated: {name} {version}")
            versions[repo] = version
            pending.append((repo, name, folder, version, f"{browser.key}:{repo}"))
            status[repo] = "pending"
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            print(f"[{repo}] Error: {exc}")
            status[repo] = "failed"

    if pending:
        try:
            prepare_browser(browser)
            ensure_developer_mode_on_screen(browser)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Error preparing browser: {exc}")
            for repo, *_ in pending:
                status[repo] = "failed"
            pending = []

        for repo, name, folder, version, state_key in pending:
            print(f"[{repo}] Loading unpacked extension...")
            try:
                result = load_extension(folder, name, browser)
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"[{repo}] Error: {exc}")
                status[repo] = "failed"
                pyautogui.press("escape")  # close any stray dialog
                continue
            status[repo] = result
            state[state_key] = {
                "version": version,
                "status": result,
                "path": str(folder),
                "fingerprint": folder_fingerprint(folder),
            }
            save_state(state)

    print("\nSummary")
    print(f"{'Repo':<28} {'Version':<14} Status")
    print("-" * 55)
    for repo, result in status.items():
        print(f"{repo:<28} {versions.get(repo, '-'):<14} {result}")


if __name__ == "__main__":
    main()