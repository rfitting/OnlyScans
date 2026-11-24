# Changelog

## v1.0.0 - 2026-11-23

First release.

- Single `OnlyScans.exe` for Windows 10/11. Nothing else to install. Opens in your browser.
- Generic OBD-II for any 1996+ vehicle: stored, pending and permanent codes, full freeze frame, live data for every supported sensor (SAE J1979 decoding, US and metric units), Mode 06 monitor tests with pass/FAIL, readiness monitors, VIN, ECU calibration IDs.
- Module codes (ABS, airbag, body...) for Toyota/Lexus/Scion, Honda/Acura, GM and Chrysler/Dodge/Jeep/Ram on CAN vehicles, plus experimental reads on GM Class 2 and Chrysler J1850 vehicles.
- Make and model year read from the VIN, or picked by hand.
- AI mechanic (Claude Sonnet 5.5 with your own Anthropic API key). It reads all the scan data, can sample live data and re-scan through read-only tools, and can never clear codes.
- Built-in guide, CSV/JSON export, clear codes with confirmation.
- Tested around the Vgate vLinker FS USB. Works with ELM327/STN adapters.
