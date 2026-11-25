# Changelog

## v1.1.0 - 2026-11-24

- **J2534 dealer tools supported:** Opus IVS Mongoose-Plus (including the Toyota MFC3), Ford VCM II, VCM 3 and VCMM, Honda GNA600, and any other J2534 v04.04 pass-thru device. They appear in the port list as `j2534:<device name>` once their driver is installed, and work for scanning, live data and the AI mechanic's tools. The app finds the vehicle's protocol on its own: CAN 11/29-bit, ISO 9141-2, ISO 14230 and J1850, as far as the tool supports them.
- **Ford / Lincoln / Mercury:** detected from the VIN. Module codes from the main CAN bus (engine, transmission, ABS, airbag, power steering, cluster, body, SYNC, gateway, parking aid, camera, seat sensor), plus the MS-CAN comfort modules (climate, doors, audio, front controls, seats) through J2534 tools that support MS-CAN.
- The bundled Python is now 32-bit so it can load 32-bit J2534 drivers. Nothing changes for the vLinker.
- Every build now scans through a stand-in J2534 driver on Windows, to test the J2534 path.
- Known gap: GM module codes on GMLAN vehicles still need the vLinker. Over J2534 those modules show "responds" without codes.

## v1.0.1 - 2026-11-23

- Fixed: the AI mechanic failed with "API error 400 ... Invalid `signature` in `thinking` block" on any question asked after a new scan, or when it re-scanned the vehicle mid-answer. Reasoning blocks from earlier turns are no longer resent, and the instructions stay fixed for the whole of each question.
- Live data now decodes four more sensors that showed as raw hex: O2 sensors present (PID 13), OBD standard (1C), monitor status this drive cycle (41), and EVAP absolute vapor pressure (53).

## v1.0.0 - 2026-11-23

First release.

- Single `OnlyScans.exe` for Windows 10/11. Nothing else to install. Opens in your browser.
- Generic OBD-II for any 1996+ vehicle: stored, pending and permanent codes, full freeze frame, live data for every supported sensor (SAE J1979 decoding, US and metric units), Mode 06 monitor tests with pass/FAIL, readiness monitors, VIN, ECU calibration IDs.
- Module codes (ABS, airbag, body...) for Toyota/Lexus/Scion, Honda/Acura, GM and Chrysler/Dodge/Jeep/Ram on CAN vehicles, plus experimental reads on GM Class 2 and Chrysler J1850 vehicles.
- Make and model year read from the VIN, or picked by hand.
- AI mechanic (Claude Sonnet 5.5 with your own Anthropic API key). It reads all the scan data, can sample live data and re-scan through read-only tools, and can never clear codes.
- Built-in guide, CSV/JSON export, clear codes with confirmation.
- Tested around the Vgate vLinker FS USB. Works with ELM327/STN adapters.
