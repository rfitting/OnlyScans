# OnlyScans

[![Build OnlyScans.exe](https://github.com/rfitting/OnlyScans/actions/workflows/build.yml/badge.svg)](https://github.com/rfitting/OnlyScans/actions/workflows/build.yml)

**A free OBD-II scan tool for Windows that comes as one file, with a built-in AI mechanic.**

Plug a USB OBD adapter into your car, double-click `OnlyScans.exe`, and it opens in your browser. It reads:

- trouble codes: stored, pending, permanent, and codes from modules like the ABS and airbag units
- live sensor data
- freeze frame
- the car's own self-test results (Mode 06)
- readiness monitors
- ECU info

The optional AI mechanic reads all of that data and tells you what to check and fix, in order. It can also watch live sensor data from the car while you talk to it.

## Download

**[Download OnlyScans.exe](https://github.com/rfitting/OnlyScans/releases/latest/download/OnlyScans.exe)** (Windows 10/11, about 11 MB). All versions are on the [Releases page](https://github.com/rfitting/OnlyScans/releases).

1. Double-click `OnlyScans.exe`.
2. Windows SmartScreen will say "Windows protected your PC", because the file isn't code-signed. Click **More info**, then **Run anyway**. You only need to do this once.
3. OnlyScans opens in your browser, and the guide opens on first launch. Keep the black window open while you use the app; closing it quits OnlyScans.

Nothing else needs to be installed, apart from the USB driver for your adapter.

**Full instructions:** [GUIDE.md](GUIDE.md). The same guide is under the Guide button in the app.

## What you need

- **A Windows 10 or 11 PC.**
- **A USB OBD-II adapter** that speaks the ELM327 command set. It was built and tested around the **Vgate vLinker FS USB**, which needs the FTDI VCP driver and its switch set to **HS-CAN**. OBDLink and other ELM327/STN adapters also work.
- **For the AI mechanic (optional):** an [Anthropic API key](https://console.anthropic.com). Usage is billed to your own account, usually a few cents per question.

## What it reads

| Vehicle | Engine codes, freeze frame, readiness, live data | Module codes (ABS, airbag, body...) |
|---|---|---|
| Any make, 1996+ | Yes | Standard requests on CAN vehicles (about 2008+) |
| Toyota / Lexus / Scion | Yes | CAN: known module addresses. Older: blink-code steps shown |
| Honda / Acura | Yes | CAN: automatic module search. Older: proprietary/blink codes |
| GM | Yes | CAN: automatic module search. About 1999–2007: experimental |
| Chrysler / Dodge / Jeep / Ram | Yes | CAN: automatic module search. About 2002–2007: experimental. Earlier CCD-bus modules aren't reachable with this type of adapter |

*Experimental* means the requests follow the published formats but haven't been confirmed on a real vehicle yet. Reports from real vehicles are welcome (see [Reporting a problem](#reporting-a-problem)).

**Safety:** everything OnlyScans sends to the vehicle is a read-only request, except **Clear codes**, which always asks you to confirm first. The AI mechanic's tools are read-only and can never clear codes or command anything.

## AI mechanic and privacy

- Your API key is only sent to `api.anthropic.com`. If you tick **Remember**, it's saved as plain text in `%APPDATA%\OnlyScans\config.json` on your own PC.
- When you ask the AI a question, your scan data goes to Anthropic, including the VIN.
- Nothing else leaves your computer. Scanning works with no internet connection.
- The interface only listens on `127.0.0.1`, and every request needs a token that changes each launch, so other websites can't send it commands.

## How it's built

```
onlyscans.py                  the whole app: OBD engine, local web server, interface, guide, AI mechanic
scripts/build_payload.py      bundles a standalone Windows Python + pyserial + onlyscans.py
launcher/                     small Rust program that carries that bundle inside OnlyScans.exe,
                              unpacks it once to %LOCALAPPDATA%\OnlyScans and starts the app
.github/workflows/build.yml   builds OnlyScans.exe on GitHub's Windows machines
GUIDE.md                      user guide
CHANGELOG.md                  what changed in each version
```

`onlyscans.py` uses only Python's standard library plus `pyserial`. The bundled Python comes from [python-build-standalone](https://github.com/astral-sh/python-build-standalone).

### Making a release

The `.exe` is built automatically by GitHub Actions, so nobody needs build tools on their own PC.

- **Every push to `main`:** builds `OnlyScans.exe`. Open the **Actions** tab, open the run, and download it under **Artifacts**.
- **New version:** update `CHANGELOG.md`, then go to **Releases → Draft a new release**, create a tag like `v1.1.0`, and publish. The workflow attaches `OnlyScans.exe` to the release, and the download link above always points to the newest one.
- **On demand:** **Actions → Build OnlyScans.exe → Run workflow**.

### Running from source (Windows, macOS or Linux)

```
python onlyscans.py              # opens the app in your browser (installs pyserial on first run)
python onlyscans.py --help       # command-line scans, live data, adapter check
```

### Building the .exe on your own PC (optional)

You need Python 3.10+ and [Rust](https://rustup.rs).

```powershell
python scripts\build_payload.py
cd launcher
$env:ONLYSCANS_PAYLOAD = (Resolve-Path ..\build\payload.tar.gz).Path
$env:ONLYSCANS_PAYLOAD_ID = (Get-Content ..\build\payload.id -Raw).Trim()
cargo build --release
copy target\release\onlyscans.exe ..\OnlyScans.exe
```

To update the bundled Python, change `PBS_TAG` and `PY_VERSION` at the top of `scripts/build_payload.py` to a newer Windows `install_only` build.

## Reporting a problem

Open an [issue](https://github.com/rfitting/OnlyScans/issues) with:

- the vehicle's year, make, model and engine;
- the adapter you used;
- what happened;
- the **Full report (JSON)** export, which has a raw log of everything the adapter sent and received. Delete the VIN from the file first if you'd rather not share it.

## Disclaimer

OnlyScans is a hobby project. It is not affiliated with Toyota, Honda, GM, Stellantis, Vgate or Anthropic. Diagnostic suggestions, including the AI's, can be wrong, so confirm with proper tests before replacing parts. Work on brakes, airbags and fuel systems at your own risk, following the service manual.

## License

[MIT](LICENSE)
