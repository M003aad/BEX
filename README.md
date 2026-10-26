# BEX

`bex.py` downloads Manifest V3 browser extensions from GitHub and loads them
into your regular browser profile one at a time using the browser's built-in
**Load unpacked** flow. It does not use Selenium or temporary startup flags.

## Requirements

- Python 3.10+
- Google Chrome, Microsoft Edge, or Brave installed locally
- `pyautogui` and `pyperclip`

Install the Python dependencies:

```bash
python -m pip install -r requirements.txt
```

On Windows, `pyautogui` controls the native file picker. Keep the desktop
unlocked and do not move the mouse or type while BEX is loading an extension.

## Preparing the input file

Create a local `bex.txt` by copying the example file:

```bash
copy bex.example bex.txt
```

Or, on PowerShell:

```powershell
Copy-Item .\bex.example .\bex.txt
```

Put one GitHub repository URL per line in `bex.txt`. The file is intentionally
ignored by Git because it may contain private repository URLs. Blank lines and
lines beginning with `#` are ignored. Each repository must contain a
Manifest V3 `manifest.json` at its root.

Example:

```text
https://github.com/user/extension-one
https://github.com/user/extension-two
```

## Usage

```bash
python bex.py
```

Use a specific browser, input file, or destination directory:

```bash
python bex.py --browser brave --file bex.txt --dest ~/bex/extensions
```

Command-line options:

- `--file PATH`: input file containing GitHub URLs (default: `bex.txt`)
- `--dest PATH`: directory for extracted extension folders (default: `~/bex/extensions`)
- `--browser {chrome,edge,brave}`: choose the browser to automate
- `--force`: continue even if a repository was already processed
- `--calibrate`: capture automation assets while the browser is open

Extracted extensions are placed in the destination directory, one folder per
repository, while browser profile data remains in the browser's normal profile.

## Browser loading behavior

BEX opens the selected browser and navigates to the extensions page:
`brave://extensions`, `edge://extensions`, or `chrome://extensions`. It checks
that Developer mode is enabled, turns it on if needed, clicks **Load unpacked**,
and selects the extension folder. This keeps the extension installed in the
browser's regular profile.

If the selected browser is already open, BEX can use that existing signed-in
window. Otherwise it starts the browser normally, then uses the keyboard and
mouse to navigate to the extensions page automatically.

## Calibration

When the browser UI layout changes, run calibration to capture the current
button locations and save them to the local `coordinates.json` file:

```bash
python bex.py --calibrate
```

Keep the browser maximized and use the same display scaling and window layout
when running BEX. Recalibrate after changing browser windows, DPI scaling, or
monitor arrangement.

The script also uses the `assets/` directory for screenshot-based matching when
coordinates are unavailable.

## Running BEX

Run the tool after creating your local `bex.txt`:

```bash
python bex.py --browser brave
```

At startup BEX asks which browser to use unless `--browser` is supplied. The
selected browser should be visible and focused while BEX operates it. Keep the
desktop unlocked and do not use the mouse or keyboard during installation.

At the end, BEX prints a summary containing each repository, manifest version,
and the final status (`loaded`, `failed`, or `unverified`).

BEX is licensed under the [MIT License](./LICENSE).