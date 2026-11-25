# OnlyScans guide

OnlyScans reads trouble codes and sensor data from any 1996-or-newer car or truck through either a vLinker FS USB (or another ELM327-type) adapter, or a J2534 dealer tool such as the Mongoose-Plus, Ford VCM II / VCM 3 / VCMM or Honda GNA600. It also has an AI mechanic that reads all of that data and suggests what to check and fix.

## Starting OnlyScans

- **OnlyScans.exe:** double-click it. A black console window opens, then the app opens in your browser.
- **onlyscans.py:** double-click it, or run `python onlyscans.py`. The first run installs `pyserial` on its own (it needs an internet connection that one time).
- **Make the .exe:** run `python onlyscans.py --build-exe` once on a Windows PC that has Python. `OnlyScans.exe` appears next to `onlyscans.py`, and it then runs on any Windows PC with nothing else installed.

Keep the console window open while you use the app; closing it quits OnlyScans. If the browser doesn't open, go to the address shown in the console window (normally `http://127.0.0.1:8765`).

## Which adapter

- **vLinker FS / ELM327-type adapters** appear in the port list as a COM port marked **[OBD adapter]**.
- **J2534 dealer tools** appear as **j2534:** followed by the device name, for example `j2534:Mongoose-Plus` or `j2534:VCM II`. Install the tool's own driver first:
  - **Mongoose-Plus:** the Opus IVS driver package.
  - **Ford VCM II:** its driver only comes with Ford IDS.
  - **VCM 3:** Ford's VCM 3 driver.
  - **VCMM:** the Bosch J2534 VCI driver.
  - **GNA600:** the Honda/Bosch J2534 driver.

  OnlyScans uses 32-bit drivers, which is what most of these tools install. A tool that only has a 64-bit driver shows a note in the port list.

| | vLinker / ELM327 | J2534 tool |
|---|---|---|
| Engine codes, live data, freeze frame, monitor tests | Yes | Yes |
| Module codes on CAN (Toyota, Honda, Chrysler, Ford) | Yes | Yes, usually faster and more reliable |
| GM module codes on GMLAN | Yes | Not yet (GM sends them in a format the J2534 channel doesn't pass) |
| Ford MS-CAN modules (climate, doors, audio) | No | Yes, if the tool supports MS-CAN (VCM II, VCM 3, VCMM) |
| Older K-line and J1850 vehicles | Yes | Only if the tool has those protocols. Many single-make tools are CAN plus K-line only |

## Before you scan

1. **Plug in the adapter.** The OBD port is under the dash on the driver's side, usually above the pedals. Connect the other end to a USB port on the laptop.
2. **vLinker only: set its switch to HS-CAN.** The MS-CAN position is only for Fords, and it hides the main bus on everything else. J2534 tools have no switch; OnlyScans selects the pins itself.
3. **Turn the key to ON.** For codes, engine off is fine. For live sensor data, start the engine.
4. **Close FORScan, Techstream, IDS/FDRS, HDS** or any other OBD program. Only one program can use the adapter at a time.

If the adapter isn't listed in step 1 below, install the FTDI VCP driver from ftdichip.com, then unplug the adapter and plug it back in.

## 1. Connect

- **Adapter port:** J2534 tools are listed first (as **j2534:**...), then the vLinker, marked **[OBD adapter]**. Click **Refresh ports** if you plugged it in after opening the app.
- **Test adapter:** checks that the adapter answers and reads the battery voltage at the OBD port.
  - Key on, engine off: about 12.2–12.8 V.
  - Engine running: about 13.5–14.7 V.
  - No voltage means the adapter isn't fully seated, or the fuse that feeds the OBD port is blown.

## 2. Scan

- **Vehicle make:** *Auto* reads the make from the VIN. Many vehicles from before about 2005, like a 1996 T100, don't report a VIN. If the Vehicle box says "Make unknown", pick the make and scan again. The make decides which module addresses and request types are tried.
- **Module scan:** also asks the ABS, airbag, transmission, body and other modules for their own codes. Turn it off for a quick engine-only scan.
- **Sweep:** checks every diagnostic address for modules the built-in lists don't cover, such as a 4WD, trailer or seat module. It adds 1–3 minutes. Use it the first time you scan a vehicle, or when a warning light is on and the normal scan doesn't explain it.
- **Read all codes:** reads stored, pending and permanent codes, the freeze frame, readiness monitors, live sensor values, monitor test results, ECU info and the modules. Older vehicles take 10–20 seconds just to connect. Leave the key on and the adapter plugged in until the results appear.

### What each make gets

| Vehicle | Engine codes, freeze frame, readiness, live data | Module codes (ABS, airbag, body...) |
|---|---|---|
| Any make, 1996+ | Yes | Standard requests on CAN vehicles (about 2008+) |
| Toyota / Lexus / Scion | Yes | CAN: known addresses. Older: ABS/airbag are blink codes, and the steps appear in Notes |
| Honda / Acura | Yes | CAN: automatic module search. Older: proprietary or blink codes |
| GM | Yes | CAN: automatic module search. About 1999–2007: experimental. Older: not reachable |
| Chrysler / Dodge / Jeep / Ram | Yes | CAN: automatic module search. About 2002–2007: experimental. Earlier body modules are on the CCD bus and not reachable. 2018+: the security gateway may block some modules |
| Ford / Lincoln / Mercury | Yes | CAN: known modules on the main bus (engine, ABS, airbag, cluster, body, SYNC and more). MS-CAN comfort modules with a J2534 tool that supports MS-CAN. Older J1850 PWM Fords: engine codes only |

*Experimental* means the requests follow the published formats but haven't been confirmed on a real vehicle yet.

## 3. Read the results

**Top boxes**

- **Check engine light:** what the engine computer reports, and how many confirmed codes it's holding.
- **Codes found:** all codes from the engine and every module. The same fault can be counted twice when it's read both as a generic code and straight from a module.
- **Vehicle:** make and model year from the VIN (or your pick), and the protocol the vehicle uses to talk. CAN is most vehicles from 2008 on. ISO 9141 / KWP is older Asian and European vehicles. J1850 VPW is older GM and Chrysler.
- **VIN:** as reported by the engine computer.

**Tabs**

- **All codes:** every code, with its type.
  - *stored*: a confirmed fault; it turns on the warning light.
  - *pending*: seen once. Another drive cycle will confirm it or drop it, so pending codes catch problems early.
  - *permanent*: can't be cleared with a scan tool. It clears itself once the system re-tests and passes.
  - *module DTC*: read straight from a module such as the ABS or airbag unit.

  For codes outside the built-in table, the app still shows the system the code belongs to. Look these up by code number together with your model and year.
- **Modules:** every module that was tried.
  - *read OK*: the module answered. 0 codes means it has no faults stored.
  - *not found*: the module isn't on this vehicle, or isn't at that address.
  - *rejected*: the module is there but refused the request.
  - *Module @ ...*: found by the search or Sweep but not in the built-in list. Its codes tell you the system: C = chassis/ABS, B = body/airbag, U = network.
- **Live data:** one reading of every sensor the engine computer supports, taken during the scan. Click **Read live now** for fresh readings at any time, for example after starting the engine. Check the RPM row to see whether the engine was running. Readings with the key on and the engine off aren't meaningful for most sensors.
- **Freeze frame:** a snapshot of engine conditions at the moment the first code was stored. Use it to reproduce the fault: cold or warm engine, idle or highway, under load or not.
  - *Fuel trims:* positive means the computer is adding fuel, which points to a lean condition such as a vacuum leak or a dirty MAF sensor. A total beyond about ±10% is worth chasing.
- **Monitor tests:** the car's own self-test results (Mode 06), each with its limits and a pass/FAIL result. A FAIL here can show a problem before it sets a code. The misfire rows count misfires per cylinder, which tells you which cylinder to look at. Only available on CAN vehicles.
- **Readiness:** the emissions self-tests. *complete* means the test has run since codes were last cleared. Clearing codes or disconnecting the battery sets them back to incomplete, and they need a few days of mixed driving to finish. On a used vehicle you're looking at, several incomplete monitors can mean someone cleared codes recently.
- **ECU info:** controller names, calibration IDs and CVNs. Dealers and TSBs use calibration IDs to check whether a computer needs a software update.
- **Notes:** anything the scan couldn't do and why, such as the blink-code steps for older Toyotas and Hondas.
- **Raw log:** every command sent to the adapter and every reply. It's mostly useful when a module won't read.

## 4. AI mechanic

The panel on the right is an AI mechanic that uses Claude (`claude-sonnet-5-5`, the latest Sonnet).

**Set it up once**

1. Go to console.anthropic.com, then **API keys**, then **Create key**.
2. Paste the key (it starts with `sk-ant-`) into the box at the top right of the app. The badge next to it turns green.
3. Tick **Remember** to save the key on this computer, so you don't paste it every time. It's stored as plain text in `AppData\Roaming\OnlyScans` in your user folder, and unticking the box deletes it. Without Remember, the key is forgotten when you close OnlyScans.

**What it can see and do**

- It gets everything from the latest scan: codes, module results, freeze frame, live data snapshot, monitor tests, readiness, ECU info, notes and the raw adapter log.
- **It can read live data from the vehicle itself** while you talk to it. For example, it can watch the O2 sensor switch, check fuel trims at idle and then at 2500 rpm, or follow coolant temperature as the engine warms up. It will tell you what state to put the vehicle in first (engine running, warmed up, holding a steady RPM). Keep the adapter plugged in and the port selected.
- It can **re-run the scan** after you drive, start the engine or make a repair. The tabs update when it does.
- It **cannot clear codes** or change anything on the vehicle. Everything it sends is read-only. Clearing stays a manual button.

**Using it**

- Tap a quick prompt, or ask your own question. Describe symptoms: noises, smells, when it happens (cold start, highway, turning) and recent repairs. That context sharpens the diagnosis a lot.
- **Enter** sends, and **Shift+Enter** starts a new line. **New chat** starts over.
- Each reply shows what it read from the vehicle and roughly what the reply cost. Usage is billed to your Anthropic account at $2 per million input tokens and $10 per million output tokens, which usually works out to a few cents per question. Live-data checks add a little.
- It only sees what the adapter reports. It can be wrong, so confirm with the tests it suggests before buying parts.

## 5. Save and clear

- **Codes (CSV):** one row per code, to open in Excel or read into R. It goes to your browser's Downloads folder.
- **Full report (JSON):** everything from the scan, including live data and the raw log. Save this before you clear anything.
- **Clear codes:** fix the cause first. Clearing erases the codes and freeze frame and resets the readiness monitors. If the fault is still there, the light comes back. Permanent codes don't clear this way.

## Updates

When a new version is released, OnlyScans shows a green bar at the top: **OnlyScans x.y.z is available**.

- **What's new:** shows the release notes.
- **Update now:** downloads the new version (about 10 MB) and checks it against GitHub's published size and checksum. If anything doesn't match, nothing is changed. It then swaps itself out and restarts, and the app opens in a new browser tab, so you can close the old one.
- **Later:** hides the bar until the next launch.

Your settings and saved API key carry over. The check happens once at each launch and is skipped quietly when there's no internet. If OnlyScans.exe is in a folder Windows won't let it change, such as Program Files, it tells you so: move it to Documents or the Desktop. The version you're running is shown next to the name at the top left.

## Troubleshooting

| Problem | Try |
|---|---|
| Browser didn't open | Open the address shown in the console window, normally `http://127.0.0.1:8765` |
| No ports listed | Install the FTDI VCP driver, replug the adapter, then click Refresh ports |
| "Could not open port" | Close FORScan or any other OBD program |
| J2534 tool isn't listed | Install its driver (for a VCM II, that means installing Ford IDS), plug it in, then click Refresh ports |
| J2534 tool says "driver is 64-bit only" | That driver can't load in OnlyScans. Check the maker's site for a 32-bit J2534 driver |
| "Unable to connect" with a J2534 tool on an older vehicle | The tool may not have that vehicle's protocol (J1850 or K-line). Use the vLinker for that vehicle |
| "Vehicle did not answer" | Key ON, adapter pushed fully in, vLinker switch on HS-CAN |
| Test adapter shows no voltage | Reseat the adapter, then check the fuse that feeds the OBD port |
| Make unknown | The vehicle doesn't report a VIN. Pick the make and scan again |
| A module says "rejected" or reads odd codes | Save the JSON report. Its raw log shows exactly what the module said |
| AI says the key was rejected | Re-paste the full key, and check billing at console.anthropic.com |
| AI can't reach the vehicle | Select the adapter port and keep the key ON |
| Windows warns about OnlyScans.exe | It's unsigned because you built it yourself. Click **More info**, then **Run anyway** |
