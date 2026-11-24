# OnlyScans guide

OnlyScans reads trouble codes and sensor data from any 1996-or-newer car or truck through a vLinker FS USB (or another ELM327-type) adapter. It also has an AI mechanic that reads all of that data and suggests what to check and fix.

## Starting OnlyScans

- **OnlyScans.exe:** double-click it. A black console window opens, then the app opens in your browser.
- **onlyscans.py:** double-click it, or run `python onlyscans.py`. The first run installs `pyserial` on its own (it needs an internet connection that one time).
- **Make the .exe:** run `python onlyscans.py --build-exe` once on a Windows PC that has Python. `OnlyScans.exe` appears next to `onlyscans.py`, and it then runs on any Windows PC with nothing else installed.

Keep the console window open while you use the app; closing it quits OnlyScans. If the browser doesn't open, go to the address shown in the console window (normally `http://127.0.0.1:8765`).

## Before you scan

1. **Plug in the adapter.** The OBD port is under the dash on the driver's side, usually above the pedals. Connect the other end to a USB port on the laptop.
2. **Set the vLinker switch to HS-CAN.** The MS-CAN position is only for Fords, and it hides the main bus on everything else.
3. **Turn the key to ON.** For codes, engine off is fine. For live sensor data, start the engine.
4. **Close FORScan** or any other OBD program. Only one program can use the adapter at a time.

If the adapter isn't listed in step 1 below, install the FTDI VCP driver from ftdichip.com, then unplug the adapter and plug it back in.

## 1. Connect

- **Adapter port:** the vLinker is listed first and marked **[OBD adapter]**. Click **Refresh ports** if you plugged it in after opening the app.
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

## Troubleshooting

| Problem | Try |
|---|---|
| Browser didn't open | Open the address shown in the console window, normally `http://127.0.0.1:8765` |
| No ports listed | Install the FTDI VCP driver, replug the adapter, then click Refresh ports |
| "Could not open port" | Close FORScan or any other OBD program |
| "Vehicle did not answer" | Key ON, adapter pushed fully in, vLinker switch on HS-CAN |
| Test adapter shows no voltage | Reseat the adapter, then check the fuse that feeds the OBD port |
| Make unknown | The vehicle doesn't report a VIN. Pick the make and scan again |
| A module says "rejected" or reads odd codes | Save the JSON report. Its raw log shows exactly what the module said |
| AI says the key was rejected | Re-paste the full key, and check billing at console.anthropic.com |
| AI can't reach the vehicle | Select the adapter port and keep the key ON |
| Windows warns about OnlyScans.exe | It's unsigned because you built it yourself. Click **More info**, then **Run anyway** |
