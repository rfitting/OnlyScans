#!/usr/bin/env python3
"""
OnlyScans - a single-file OBD-II scan tool with an AI mechanic.

Run it with no options (double-click, or `python onlyscans.py`) and it opens in your browser.
Package it as one Windows program with:  python onlyscans.py --build-exe
Everything below the app section is also usable from the command line (see --help).

Reads every trouble code an ELM327 / STN / vLinker adapter can reach.

Generic OBD-II, every 1996+ vehicle of any make (CAN, ISO 9141-2, KWP2000, J1850):
  Mode 01 PID 01   check-engine light, stored-code count, readiness monitors
  Mode 03 / 07 / 0A  stored, pending, permanent codes
  Mode 02          freeze frame
  Mode 09 PID 02   VIN  (also used to identify make and model year)

Module codes (ABS, airbag, body, cluster ...), by make and bus:
  Toyota/Lexus/Scion   11-bit CAN (~2008+): known module addresses
  Honda/Acura          11-bit CAN powertrain + 29-bit CAN (18DAxxF1) module discovery
  GM                   GMLAN (~2006+): UDS 0x19 and GMLAN 0xA9 on 0x241-0x25F;
                       Class 2 / J1850 VPW (~1999-2007): Class 2 0x19
  Chrysler/Dodge/Jeep/Ram  CAN (~2008+): 0x740-0x75F; J1850 VPW (~2002-2007)
  Other                standard UDS / KWP requests on the usual 11-bit addresses
--sweep probes every diagnostic address for modules not in the built-in lists.
Everything sent is a read-only request (read codes, tester-present, default session),
except mode 04, which is only sent with --clear.

Examples
  python onlyscans.py --list-ports
  python onlyscans.py --port auto
  python onlyscans.py --port COM4 --make honda --sweep --json report.json --csv codes.csv
  python onlyscans.py --port auto --probe
  python onlyscans.py --port auto --live --pids 0C,05,06,07 --seconds 10

Requires: pyserial (installed automatically on first run)
"""

import argparse
import csv
import datetime
import json
import re
import sys
import time

# ==========================================================================
# Reference data
# ==========================================================================

PROTOCOLS = {
    "1": "SAE J1850 PWM", "2": "SAE J1850 VPW", "3": "ISO 9141-2",
    "4": "ISO 14230-4 KWP (5-baud init)", "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN 11-bit 500k", "7": "ISO 15765-4 CAN 29-bit 500k",
    "8": "ISO 15765-4 CAN 11-bit 250k", "9": "ISO 15765-4 CAN 29-bit 250k",
    "A": "SAE J1939",
}
CAN_PROTOCOLS = {"6", "7", "8", "9"}

MAKES = {
    "toyota":   "Toyota / Lexus / Scion",
    "honda":    "Honda / Acura",
    "gm":       "GM (Chevrolet, GMC, Buick, Cadillac, Pontiac, Saturn, Oldsmobile)",
    "chrysler": "Chrysler / Dodge / Jeep / Ram",
    "ford":     "Ford / Lincoln / Mercury",
    "other":    "Other / unknown make",
}

# World manufacturer identifier (VIN chars 1-3) prefixes, longest match wins.
WMI = {
    "toyota": ["JT", "4T", "5T", "2T", "JTH", "JTJ", "JTK", "JTL"],
    "honda": ["JH", "1HG", "2HG", "3HG", "5FN", "5FP", "5J6", "5J8", "19U", "19X",
              "SHH", "SHS", "2HK", "2HN", "JHL", "JHM", "JH4"],
    "gm": ["1G", "2G", "3G", "KL", "6G", "1GT", "1GC", "1GN", "1GK", "1G6", "5GA",
           "5GZ", "5Y2"],
    "ford": ["1FA", "1FB", "1FC", "1FD", "1FM", "1FT", "2FA", "2FM", "2FT", "3FA", "3FM",
             "3FT", "1LN", "1ME", "2LM", "5LM", "NM0", "WF0", "1ZV", "MAJ"],
    "chrysler": ["1C", "2C", "3C", "1J", "1B", "2B", "3B", "1D", "2D", "3D", "2A4", "2A8",
                 "ZAC", "1A4", "1A8", "3A4", "3A8"],
}
# Exceptions where a shorter prefix would mislead
WMI_OVERRIDE = {"1GM": "gm", "5YJ": "other", "1HD": "other", "1HT": "other", "2HM": "other",
                "JF1": "other", "JF2": "other", "1YV": "other", "JHB": "other"}

YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"  # 1980..2009, then repeats from 2010


def make_from_vin(vin):
    if not vin or len(vin) < 3:
        return None
    w = vin[:3].upper()
    if w in WMI_OVERRIDE:
        return WMI_OVERRIDE[w]
    best, best_len = None, 0
    for make, prefixes in WMI.items():
        for p in prefixes:
            if w.startswith(p) and len(p) > best_len:
                best, best_len = make, len(p)
    return best or "other"


def year_from_vin(vin):
    if not vin or len(vin) != 17:
        return None
    c = vin[9].upper()
    if c not in YEAR_CODES:
        return None
    y = 1980 + YEAR_CODES.index(c)
    if c.isalpha() and vin[6].isalpha():   # North-American rule for 2010+ model years
        y += 30
    return y


# Physical-addressed modules on 11-bit CAN: (request id, reply id, name)
CAN11_MODULES = {
    "toyota": [(0x7E0, 0x7E8, "Engine (ECM)"), (0x7E1, 0x7E9, "Transmission (ECT)"),
               (0x7B0, 0x7B8, "ABS / VSC / TRAC"), (0x780, 0x788, "Airbag (SRS)"),
               (0x7C0, 0x7C8, "Combination meter"), (0x7A1, 0x7A9, "Electric power steering"),
               (0x7C4, 0x7CC, "Air conditioning")],
    "honda": [(0x7E0, 0x7E8, "Engine (PGM-FI)"), (0x7E1, 0x7E9, "Transmission")],
    "gm": [(0x7E0, 0x7E8, "Engine (ECM)"), (0x7E2, 0x7EA, "Transmission (TCM)")],
    "chrysler": [(0x7E0, 0x7E8, "Engine (PCM)"), (0x7E1, 0x7E9, "Transmission (TCM)")],
    "ford": [(0x7E0, 0x7E8, "Engine (PCM)"), (0x7E1, 0x7E9, "Transmission (TCM)"),
             (0x760, 0x768, "ABS"), (0x737, 0x73F, "Airbag (RCM)"),
             (0x730, 0x738, "Power steering (PSCM)"), (0x720, 0x728, "Instrument cluster (IPC)"),
             (0x726, 0x72E, "Body (BCM)"), (0x7D0, 0x7D8, "SYNC (APIM)"),
             (0x716, 0x71E, "Gateway (GWM)"), (0x736, 0x73E, "Parking aid (PAM)"),
             (0x706, 0x70E, "Camera (IPMA)"), (0x765, 0x76D, "Seat sensor (OCSM)")],
    "other": [(0x7E0, 0x7E8, "Engine"), (0x7E1, 0x7E9, "Transmission")],
}
# Ford medium-speed CAN (MS-CAN, OBD pins 3 and 11, 125 kbps): comfort and body modules.
FORD_MS_MODULES = [(0x733, 0x73B, "Climate control (HVAC)"), (0x740, 0x748, "Driver door (DDM)"),
                   (0x741, 0x749, "Passenger door (PDM)"), (0x7A7, 0x7AF, "Front controls (FCIM)"),
                   (0x727, 0x72F, "Audio (ACM)"), (0x731, 0x739, "Remote function (RFA)"),
                   (0x744, 0x74C, "Driver seat (DSM)"), (0x775, 0x77D, "Rear climate (RHVAC)")]

# GMLAN unacknowledged (UUDT) reply ids used by service 0xA9
GM_UUDT = {0x7E0: 0x5E8, 0x7E2: 0x5EA}

# J1850 module targets tried on pre-CAN GM (Class 2) and Chrysler vehicles
J1850_MODULES = {
    "gm": [(0x10, "Engine (PCM)"), (0x18, "Transmission"), (0x28, "ABS (EBCM)"),
           (0x58, "Airbag (SDM)"), (0x40, "Body (BCM)"), (0x60, "Instrument cluster"),
           (0x80, "Radio"), (0x99, "HVAC")],
    "chrysler": [(0x10, "Engine (PCM)"), (0x18, "Transmission"), (0x28, "ABS"),
                 (0x40, "Body (BCM)"), (0x60, "Instrument cluster"), (0x58, "Airbag (ORC)"),
                 (0x80, "Radio"), (0x98, "HVAC")],
}
J1850_HEADER = {"gm": "6C", "chrysler": "24"}

# (request, positive-response SID, label) tried in order on each module
REQ = {
    "can": [("19 02 FF", 0x59, "UDS 0x19"), ("18 00 FF 00", 0x58, "KWP 0x18"),
            ("13", 0x53, "KWP 0x13"), ("03", 0x43, "OBD 0x03 (physical)")],
    "gmlan": [("19 02 FF", 0x59, "UDS 0x19"), ("A9 81 12", 0x81, "GMLAN 0xA9")],
    "j1850_gm": [("19 D2 FF 00", 0x59, "Class 2 0x19"), ("19 FF FF 00", 0x59, "Class 2 0x19")],
    "j1850_chrysler": [("18 00 FF 00", 0x58, "KWP 0x18"), ("13", 0x53, "KWP 0x13"),
                       ("03", 0x43, "OBD 0x03 (physical)")],
}

NRC = {0x10: "general reject", 0x11: "service not supported",
       0x12: "sub-function not supported", 0x13: "bad message length",
       0x22: "conditions not correct", 0x31: "request out of range",
       0x33: "security access denied", 0x78: "response pending"}

DESC = {
    # Generic powertrain
    "P0010": "Camshaft position actuator circuit (bank 1)",
    "P0011": "Camshaft timing over-advanced (bank 1)",
    "P0012": "Camshaft timing over-retarded (bank 1)",
    "P0100": "Mass air flow (MAF) circuit", "P0101": "MAF circuit range/performance",
    "P0102": "MAF circuit low input", "P0103": "MAF circuit high input",
    "P0105": "Manifold absolute pressure (MAP) circuit", "P0106": "MAP range/performance",
    "P0110": "Intake air temperature circuit", "P0113": "Intake air temperature circuit high",
    "P0115": "Engine coolant temperature circuit",
    "P0116": "Engine coolant temperature range/performance",
    "P0117": "Engine coolant temperature circuit low",
    "P0118": "Engine coolant temperature circuit high",
    "P0120": "Throttle position sensor A circuit",
    "P0121": "Throttle position sensor A range/performance",
    "P0125": "Insufficient coolant temperature for closed-loop fuel control",
    "P0128": "Coolant thermostat (temp below regulating temperature)",
    "P0130": "O2 sensor circuit (bank 1 sensor 1)",
    "P0131": "O2 sensor circuit low voltage (bank 1 sensor 1)",
    "P0133": "O2 sensor slow response (bank 1 sensor 1)",
    "P0134": "O2 sensor no activity (bank 1 sensor 1)",
    "P0135": "O2 sensor heater circuit (bank 1 sensor 1)",
    "P0136": "O2 sensor circuit (bank 1 sensor 2)",
    "P0141": "O2 sensor heater circuit (bank 1 sensor 2)",
    "P0150": "O2 sensor circuit (bank 2 sensor 1)",
    "P0155": "O2 sensor heater circuit (bank 2 sensor 1)",
    "P0161": "O2 sensor heater circuit (bank 2 sensor 2)",
    "P0171": "System too lean (bank 1)", "P0172": "System too rich (bank 1)",
    "P0174": "System too lean (bank 2)", "P0175": "System too rich (bank 2)",
    "P0300": "Random/multiple cylinder misfire",
    "P0301": "Cylinder 1 misfire", "P0302": "Cylinder 2 misfire",
    "P0303": "Cylinder 3 misfire", "P0304": "Cylinder 4 misfire",
    "P0305": "Cylinder 5 misfire", "P0306": "Cylinder 6 misfire",
    "P0307": "Cylinder 7 misfire", "P0308": "Cylinder 8 misfire",
    "P0325": "Knock sensor 1 circuit (bank 1)", "P0330": "Knock sensor 2 circuit (bank 2)",
    "P0335": "Crankshaft position sensor A circuit", "P0340": "Camshaft position sensor circuit",
    "P0400": "EGR flow", "P0401": "EGR flow insufficient",
    "P0420": "Catalyst efficiency below threshold (bank 1)",
    "P0430": "Catalyst efficiency below threshold (bank 2)",
    "P0440": "EVAP system", "P0441": "EVAP incorrect purge flow",
    "P0442": "EVAP small leak detected", "P0446": "EVAP vent control circuit",
    "P0449": "EVAP vent solenoid circuit", "P0455": "EVAP large leak detected",
    "P0456": "EVAP very small leak detected",
    "P0457": "EVAP leak detected (fuel cap loose/off)",
    "P0500": "Vehicle speed sensor", "P0505": "Idle air control system",
    "P0506": "Idle speed lower than expected", "P0507": "Idle speed higher than expected",
    "P0560": "System voltage",
    "P0562": "System voltage low", "P0563": "System voltage high",
    "P0603": "Control module keep-alive memory error", "P0606": "Control module processor",
    "P0700": "Transmission control system (see transmission codes)",
    "P0705": "Transmission range sensor circuit",
    "P0710": "Transmission fluid temperature sensor circuit",
    "P0715": "Input/turbine speed sensor circuit", "P0720": "Output speed sensor circuit",
    "P0730": "Incorrect gear ratio",
    "P0741": "Torque converter clutch performance or stuck off",
    "P0750": "Shift solenoid A", "P0755": "Shift solenoid B",
    "P2195": "O2 / A-F sensor signal stuck lean (bank 1 sensor 1)",
    # Toyota-specific
    "P1135": "A/F sensor heater circuit (bank 1 sensor 1) [Toyota]",
    "P1300": "Igniter circuit [Toyota]", "P1349": "VVT system malfunction (bank 1) [Toyota]",
    "P1500": "Starter signal circuit [Toyota]",
    "P1600": "ECM BATT (backup power) circuit [Toyota]",
    "P1780": "Park/neutral position switch [Toyota]",
    "C0200": "Right front wheel speed sensor signal [Toyota]",
    "C0205": "Left front wheel speed sensor signal [Toyota]",
    "C0210": "Right rear wheel speed sensor signal [Toyota]",
    "C0215": "Left rear wheel speed sensor signal [Toyota]",
    "C1201": "Engine control system malfunction - VSC disabled [Toyota]",
    "C1203": "ECM communication circuit [Toyota]",
    "C1241": "Low battery positive voltage [Toyota]",
    "C1249": "Stop light switch circuit [Toyota]",
    # Network
    "U0100": "Lost communication with ECM/PCM", "U0101": "Lost communication with TCM",
    "U0121": "Lost communication with ABS module",
    "U0140": "Lost communication with body control module",
    "U0151": "Lost communication with airbag (restraints) module",
    "U0155": "Lost communication with instrument cluster",
}

P_SUBSYSTEM = {
    "1": "fuel and air metering", "2": "fuel and air metering (injector circuit)",
    "3": "ignition system or misfire", "4": "auxiliary emission controls",
    "5": "vehicle speed, idle control, auxiliary inputs",
    "6": "computer and output circuits", "7": "transmission",
    "8": "transmission", "9": "transmission", "A": "hybrid propulsion",
}
LETTER = {"P": "powertrain", "C": "chassis", "B": "body", "U": "network"}


def describe(code):
    base = code.split("-")[0]
    if base in DESC:
        return DESC[base]
    letter, origin = base[0], base[1]
    if letter == "P":
        generic = origin in "02" or (origin == "3" and base[2] in "4567")
    else:
        generic = origin in "03"
    scope = "generic" if generic else "manufacturer-specific"
    if letter == "P" and base[2] in P_SUBSYSTEM:
        return f"Not in local table ({scope} powertrain, {P_SUBSYSTEM[base[2]]})"
    return f"Not in local table ({scope} {LETTER.get(letter, '')} code)"


def decode_dtc(a, b):
    return f"{'PCBU'[a >> 6]}{(a >> 4) & 3}{a & 0xF:X}{b >> 4:X}{b & 0xF:X}"


def uds_status(st):
    flags = []
    if st & 0x01: flags.append("failing now")
    if st & 0x04: flags.append("pending")
    if st & 0x08: flags.append("confirmed")
    if st & 0x80: flags.append("warning lamp")
    return f"0x{st:02X}" + (f" ({', '.join(flags)})" if flags else "")


PRE_CAN_NOTES = {
    "toyota": (
        "Pre-CAN Toyota: only engine/emissions codes come over the OBD-II port. ABS and "
        "airbag codes are blink codes: key OFF, jumper TC to CG on the OBD port (pin 13 to "
        "pin 4), or Tc to E1 in the under-hood DLC1 if fitted, key ON engine off, then count "
        "the ABS/airbag light flashes (two-digit codes). Check the terminal names and code "
        "chart for your model year in the service manual."),
    "honda": (
        "Pre-CAN Honda/Acura: only engine/emissions codes come over OBD-II with this cable; "
        "Honda's ABS and SRS units use a proprietary protocol. Many models flash ABS and SRS "
        "codes instead: key OFF, jumper the 2-pin service check (SCS) connector, key ON, then "
        "count the warning-light flashes. Connector location and code chart vary by model, "
        "so check the service manual."),
    "gm": (
        "Pre-CAN GM: module codes were tried over Class 2 (J1850 VPW) and are experimental. "
        "GM vehicles from before about 1999-2000 keep ABS and airbag on an 8192-baud UART "
        "(ALDL) line, which ELM-type cables can't use."),
    "chrysler": (
        "Pre-CAN Chrysler/Dodge/Jeep: module codes were tried over J1850 VPW and are "
        "experimental. Vehicles from before about 2001-2004 run the body, cluster, ABS and "
        "airbag modules on the CCD bus, which ELM-type cables can't reach. Many 1996+ "
        "Chrysler products show engine codes on the odometer: turn the key ON-OFF-ON-OFF-ON "
        "without cranking."),
    "ford": (
        "Pre-CAN Ford/Lincoln/Mercury (J1850 PWM, roughly 1996-2004): engine/emissions codes "
        "are read; module codes on Ford's older SCP network use Ford-specific requests this "
        "version doesn't send."),
    "other": (
        "Pre-CAN vehicle: only engine/emissions codes are read. Module codes on pre-CAN "
        "vehicles use make-specific protocols."),
}

EXPERIMENTAL = {"GMLAN 0xA9", "Class 2 0x19"}


# ==========================================================================
# Adapter I/O
# ==========================================================================

class SerialAdapter:
    """ELM327 or STN11xx/STN21xx (OBDLink, vLinker) over USB serial or Bluetooth SPP."""
    # vLinker FS / OBDLink default to 115200; most ELM327 clones to 38400.
    BAUDS = (115200, 38400, 230400, 500000, 9600)

    def __init__(self, port, baud=None):
        import serial
        self.log, self.ser, self.baud, self.port = [], None, None, port
        for b in ([baud] if baud else self.BAUDS):
            try:
                s = serial.Serial(port, b, timeout=0.2)
            except serial.SerialException as e:
                sys.exit(f"Could not open {port}: {e}\n(If FORScan or another OBD program "
                         "is open, close it - only one program can hold the port.)")
            s.write(b"\r\r")
            time.sleep(0.3)
            s.reset_input_buffer()
            s.write(b"ATI\r")
            time.sleep(0.6)
            resp = s.read(512).decode(errors="ignore")
            if re.search(r"ELM|STN|OBDLink|vLinker|Vgate", resp, re.I):
                self.ser, self.baud = s, b
                break
            s.close()
        if not self.ser:
            sys.exit(f"No ELM327/STN adapter answered on {port} "
                     f"(tried baud {', '.join(map(str, self.BAUDS))}).")

    def pause(self, seconds):
        time.sleep(seconds)

    def raw(self, cmd, timeout=5.0):
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode("ascii"))
        buf, end = "", time.time() + timeout
        while time.time() < end:
            chunk = self.ser.read(self.ser.in_waiting or 1).decode(errors="ignore")
            if chunk:
                buf += chunk
                if ">" in buf:
                    break
        lines = [l.strip() for l in re.split(r"[\r\n]+", buf.replace(">", "")) if l.strip()]
        if lines and lines[0].replace(" ", "").upper() == cmd.replace(" ", "").upper():
            lines = lines[1:]  # echo
        self.log.append({"tx": cmd, "rx": lines})
        return lines

    def close(self):
        if self.ser:
            self.ser.close()


VLINKER_IDS = {(0x0403, 0x6015)}  # FTDI FT231X bridge used by the vLinker FS USB


def is_obd_adapter(p):
    text = " ".join(str(x) for x in (p.description, p.manufacturer, p.product) if x)
    return ((p.vid, p.pid) in VLINKER_IDS or
            bool(re.search(r"vLinker|vgate|OBDLink|ELM327|STN", text, re.I)))


def find_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        sys.exit("pyserial is not installed: pip install pyserial")
    return sorted(lp.comports(), key=lambda p: not is_obd_adapter(p))


def list_ports():
    ports = find_ports()
    devs = j2534_devices()
    for d in devs:
        note = "" if python_bits() in d["libs"] else \
            f" (driver is {'/'.join(f'{b}-bit' for b in d['libs']) or 'unknown'} only)"
        print(f"{J2534_PREFIX}{d['name']}\tJ2534 pass-thru: {d['name']}{note}\tOBD adapter")
    if not ports and not devs:
        print("No serial ports found")
    for p in ports:
        print(f"{p.device}\t{p.description}\t{'OBD adapter' if is_obd_adapter(p) else ''}")


def auto_port():
    hits = [p for p in find_ports() if is_obd_adapter(p)]
    if not hits and j2534_devices():
        return J2534_PREFIX + j2534_devices()[0]["name"]
    if not hits:
        sys.exit("No vLinker/OBD adapter found. Is it plugged in and is the FTDI driver "
                 "installed? Use --list-ports and pass --port explicitly.")
    return hits[0].device


# ==========================================================================
# J2534 pass-thru adapters (Mongoose-Plus, CarDAQ, OpenPort, ...)
# ==========================================================================
#
# A J2534 device is driven through its maker's Windows DLL instead of a serial port.
# J2534Adapter accepts the same ELM327-style commands the Scanner already sends (ATSH,
# ATCRA, ATSP, hex requests ...) and turns them into J2534 calls, then formats replies
# the way an ELM327 would. Everything above and below this section works unchanged.

J2534_PREFIX = "j2534:"
J2534_PROTO = {"J1850VPW": 1, "J1850PWM": 2, "ISO9141": 3, "ISO14230": 4, "CAN": 5,
               "ISO15765": 6, "ISO15765_PS": 0x8005}
J_SET_CONFIG, J_J1962_PINS = 2, 0x8001
J_CAN_29BIT_ID, J_ISO15765_FRAME_PAD = 0x100, 0x40
J_PASS_FILTER, J_FLOW_CONTROL_FILTER = 1, 3
J_READ_VBATT, J_FIVE_BAUD_INIT, J_FAST_INIT, J_CLEAR_RX_BUFFER = 3, 4, 5, 8
J_TX_MSG_TYPE, J_START_OF_MESSAGE, J_TX_DONE = 0x01, 0x02, 0x08
J_ERR_TIMEOUT, J_ERR_BUFFER_EMPTY = 0x09, 0x10

# ELM protocol number -> (J2534 protocol, connect flags, baud)
J2534_FOR_ELM = {
    "6": ("ISO15765", 0, 500000), "7": ("ISO15765", J_CAN_29BIT_ID, 500000),
    "8": ("ISO15765", 0, 250000), "9": ("ISO15765", J_CAN_29BIT_ID, 250000),
    "2": ("J1850VPW", 0, 10400), "1": ("J1850PWM", 0, 41600),
    "3": ("ISO9141", 0, 10400), "5": ("ISO14230", 0, 10400), "4": ("ISO14230", 0, 10400),
    "M": ("ISO15765_PS", 0, 125000),   # Ford MS-CAN on pins 3/11 (J2534-2)
}
J2534_CAN = {6, 0x8005}
J2534_SEARCH = ["6", "7", "8", "9", "2", "1", "3", "5"]
J2534_29BIT_ECUS = [0x10, 0x11, 0x18, 0x1A, 0x28, 0x58]   # functional-reply filters


class J2534Error(Exception):
    def __init__(self, what, code, detail=""):
        self.code = code
        super().__init__(f"{what} failed (J2534 error 0x{code:02X}{': ' + detail if detail else ''})")


def _pe_bits(path):
    """32 or 64 for a Windows DLL, from its PE header."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        off = int.from_bytes(head[0x3C:0x40], "little")
        return {0x14C: 32, 0x8664: 64}.get(int.from_bytes(head[off + 4:off + 6], "little"))
    except Exception:
        return None


def j2534_devices():
    """Installed J2534 v04.04 drivers, read from both the 32- and 64-bit registry views."""
    if os.name != "nt":
        return []
    import winreg
    found = {}
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\PassThruSupport.04.04",
                                  0, winreg.KEY_READ | view)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            vals = {}
            try:
                key = winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view)
                j = 0
                while True:
                    try:
                        n, v, _ = winreg.EnumValue(key, j)
                    except OSError:
                        break
                    vals[n.upper()] = v
                    j += 1
            except OSError:
                continue
            lib = vals.get("FUNCTIONLIBRARY")
            if not lib:
                continue
            name = str(vals.get("NAME") or sub)
            dev = found.setdefault(name, {"name": name, "vendor": str(vals.get("VENDOR", "")),
                                          "libs": {}, "protocols": set()})
            bits = _pe_bits(lib)
            if bits:
                dev["libs"][bits] = lib
            for p in J2534_PROTO:
                if vals.get(p) == 1:
                    dev["protocols"].add(p)
    return sorted(found.values(), key=lambda d: d["name"])


def python_bits():
    import struct
    return struct.calcsize("P") * 8


class J2534Api:
    """Thin ctypes binding for a J2534 v04.04 DLL."""

    def __init__(self, dll_path):
        import ctypes
        from ctypes import c_ulong, c_void_p, c_char_p, POINTER, byref, Structure, c_ubyte
        self.ct, self.byref, self.c_ulong = ctypes, byref, c_ulong

        class Msg(Structure):
            _fields_ = [("ProtocolID", c_ulong), ("RxStatus", c_ulong), ("TxFlags", c_ulong),
                        ("Timestamp", c_ulong), ("DataSize", c_ulong),
                        ("ExtraDataIndex", c_ulong), ("Data", c_ubyte * 4128)]

        class SByteArray(Structure):
            _fields_ = [("NumOfBytes", c_ulong), ("BytePtr", POINTER(c_ubyte))]

        self.Msg, self.SByteArray = Msg, SByteArray
        d = self.dll = ctypes.WinDLL(dll_path)
        sig = {
            "PassThruOpen": [c_void_p, POINTER(c_ulong)],
            "PassThruClose": [c_ulong],
            "PassThruConnect": [c_ulong, c_ulong, c_ulong, c_ulong, POINTER(c_ulong)],
            "PassThruDisconnect": [c_ulong],
            "PassThruReadMsgs": [c_ulong, POINTER(Msg), POINTER(c_ulong), c_ulong],
            "PassThruWriteMsgs": [c_ulong, POINTER(Msg), POINTER(c_ulong), c_ulong],
            "PassThruStartMsgFilter": [c_ulong, c_ulong, POINTER(Msg), POINTER(Msg),
                                       POINTER(Msg), POINTER(c_ulong)],
            "PassThruStopMsgFilter": [c_ulong, c_ulong],
            "PassThruIoctl": [c_ulong, c_ulong, c_void_p, c_void_p],
            "PassThruReadVersion": [c_ulong, c_char_p, c_char_p, c_char_p],
            "PassThruGetLastError": [c_char_p],
        }
        for fn, args in sig.items():
            f = getattr(d, fn)
            f.argtypes, f.restype = args, ctypes.c_long

    def _err(self, what, rc):
        buf = self.ct.create_string_buffer(256)
        try:
            self.dll.PassThruGetLastError(buf)
        except Exception:
            pass
        raise J2534Error(what, rc, buf.value.decode(errors="ignore").strip())

    def _msg(self, proto, data=b"", tx_flags=0):
        m = self.Msg()
        m.ProtocolID, m.TxFlags, m.DataSize = proto, tx_flags, len(data)
        for i, b in enumerate(data):
            m.Data[i] = b
        return m

    def open(self):
        dev = self.c_ulong()
        rc = self.dll.PassThruOpen(None, self.byref(dev))
        if rc:
            self._err("PassThruOpen", rc)
        return dev.value

    def close(self, dev):
        self.dll.PassThruClose(dev)

    def connect(self, dev, proto, flags, baud):
        ch = self.c_ulong()
        rc = self.dll.PassThruConnect(dev, proto, flags, baud, self.byref(ch))
        if rc:
            self._err("PassThruConnect", rc)
        return ch.value

    def disconnect(self, ch):
        self.dll.PassThruDisconnect(ch)

    def start_filter(self, ch, ftype, proto, mask, pattern, flow=None, tx_flags=0):
        fid = self.c_ulong()
        fc = self.byref(self._msg(proto, flow, tx_flags)) if flow is not None else None
        rc = self.dll.PassThruStartMsgFilter(ch, ftype, self.byref(self._msg(proto, mask, tx_flags)),
                                             self.byref(self._msg(proto, pattern, tx_flags)),
                                             fc, self.byref(fid))
        if rc:
            self._err("PassThruStartMsgFilter", rc)
        return fid.value

    def stop_filter(self, ch, fid):
        self.dll.PassThruStopMsgFilter(ch, fid)

    def write(self, ch, proto, data, tx_flags=0, timeout=500):
        n = self.c_ulong(1)
        rc = self.dll.PassThruWriteMsgs(ch, self.byref(self._msg(proto, data, tx_flags)),
                                        self.byref(n), timeout)
        if rc:
            self._err("PassThruWriteMsgs", rc)

    def read(self, ch, timeout):
        """One message as (rx_status, bytes), or None when nothing arrived."""
        m, n = self.Msg(), self.c_ulong(1)
        rc = self.dll.PassThruReadMsgs(ch, self.byref(m), self.byref(n), timeout)
        if rc in (J_ERR_TIMEOUT, J_ERR_BUFFER_EMPTY) or (rc == 0 and n.value == 0):
            return None
        if rc:
            self._err("PassThruReadMsgs", rc)
        return m.RxStatus, bytes(m.Data[:m.DataSize])

    def ioctl(self, ch, ioctl_id):
        rc = self.dll.PassThruIoctl(ch, ioctl_id, None, None)
        if rc:
            self._err(f"PassThruIoctl({ioctl_id})", rc)

    def set_config(self, ch, params):
        c = self.ct

        class SConfig(c.Structure):
            _fields_ = [("Parameter", c.c_ulong), ("Value", c.c_ulong)]

        class SConfigList(c.Structure):
            _fields_ = [("NumOfParams", c.c_ulong), ("ConfigPtr", c.POINTER(SConfig))]

        arr = (SConfig * len(params))(*[SConfig(k, v) for k, v in params])
        lst = SConfigList(len(params), c.cast(arr, c.POINTER(SConfig)))
        rc = self.dll.PassThruIoctl(ch, J_SET_CONFIG, self.byref(lst), None)
        if rc:
            self._err("SET_CONFIG", rc)

    def vbatt(self, dev):
        mv = self.c_ulong()
        rc = self.dll.PassThruIoctl(dev, J_READ_VBATT, None, self.byref(mv))
        if rc:
            self._err("READ_VBATT", rc)
        return mv.value

    def five_baud_init(self, ch, addr=0x33):
        c = self.ct
        inb, outb = (c.c_ubyte * 1)(addr), (c.c_ubyte * 2)()
        sin = self.SByteArray(1, c.cast(inb, c.POINTER(c.c_ubyte)))
        sout = self.SByteArray(2, c.cast(outb, c.POINTER(c.c_ubyte)))
        rc = self.dll.PassThruIoctl(ch, J_FIVE_BAUD_INIT, self.byref(sin), self.byref(sout))
        if rc:
            self._err("5-baud init", rc)
        return bytes(outb)

    def fast_init(self, ch, proto, data):
        out = self._msg(proto)
        rc = self.dll.PassThruIoctl(ch, J_FAST_INIT, self.byref(self._msg(proto, data)),
                                    self.byref(out))
        if rc:
            self._err("fast init", rc)
        return bytes(out.Data[:out.DataSize])

    def version(self, dev):
        fw, dll, api = (self.ct.create_string_buffer(80) for _ in range(3))
        if self.dll.PassThruReadVersion(dev, fw, dll, api):
            return "", "", ""
        return tuple(x.value.decode(errors="ignore") for x in (fw, dll, api))


def _j1850_crc(data):
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


class J2534Adapter:
    """Looks like an ELM327 to the Scanner; talks J2534 to the device."""

    def __init__(self, port, api=None, protocols=None):
        self.port, self.baud, self.log = port, "J2534", []
        self.name = port[len(J2534_PREFIX):]
        self.vendor, self.lib = "", "(test driver)"
        if api is None:
            dev = next((d for d in j2534_devices() if d["name"] == self.name), None)
            if not dev:
                sys.exit(f"The J2534 device '{self.name}' isn't installed on this PC. Install its "
                         "driver, plug it in, then click Refresh ports.")
            bits = python_bits()
            self.lib = dev["libs"].get(bits)
            if not self.lib:
                other = ", ".join(f"{b}-bit" for b in dev["libs"]) or "unknown"
                sys.exit(f"The {self.name} driver is {other} only, but this copy of OnlyScans is "
                         f"{bits}-bit, so Windows can't load it.")
            api, self.vendor = J2534Api(self.lib), dev["vendor"]
            protocols = dev["protocols"]
        self.api = api
        self.protocols = set(protocols or J2534_PROTO)
        try:
            self.dev = api.open()
        except J2534Error as e:
            sys.exit(f"Couldn't open {self.name}: {e}. Is it plugged into the PC and the vehicle?")
        self.fw = (api.version(self.dev) or ("",))[0]
        self.ch = None
        self._reset()

    # -- housekeeping -----------------------------------------------------------
    def _reset(self):
        self._disconnect()
        self.want, self.proto = None, None
        self.hdr, self.cra, self.cp, self.st_ms = None, None, 0x18, 200
        self.kline_last = 0.0

    def _disconnect(self):
        if self.ch is not None:
            try:
                self.api.disconnect(self.ch)
            except Exception:
                pass
        self.ch, self.proto, self.phys = None, None, {}

    def pause(self, seconds):
        time.sleep(seconds)

    def close(self):
        self._disconnect()
        try:
            self.api.close(self.dev)
        except Exception:
            pass

    # -- ELM-style entry point ---------------------------------------------------------
    def raw(self, cmd, timeout=5.0):
        c = cmd.upper().replace(" ", "")
        try:
            out = self._at(c[2:]) if c.startswith("AT") else self._request(c)
        except J2534Error as e:
            self._disconnect()
            out = [f"BUS ERROR ({e})"]
        self.log.append({"tx": cmd, "rx": out})
        return out

    def _at(self, a):
        if a in ("Z", "I", "WS", "D"):
            if a in ("Z", "WS", "D"):
                self._reset()
            return [f"J2534 {self.name}" + (f" fw {self.fw}" if self.fw else "")]
        if a == "@1":
            return [f"{self.vendor} {self.name}, driver {self.lib}".strip()]
        if a == "RV":
            try:
                return [f"{self.api.vbatt(self.dev) / 1000:.1f}V"]
            except J2534Error as e:
                return [f"? ({e})"]
        if a == "DPN":
            return ["A" + (self.proto or "0")]
        if a.startswith("SP") or a.startswith("TP"):
            p = a[2:].lstrip("A") or "0"
            if p not in J2534_FOR_ELM and p != "0":
                return ["?"]
            if p == "M" and not self._connect("M"):   # check the device can do MS-CAN now
                return ["?"]
            want = None if p == "0" else p
            if want != self.proto:
                self._disconnect()
            self.want = want
            return ["OK"]
        if a.startswith("SH"):
            self.hdr = a[2:]
        elif a.startswith("CRA"):
            self.cra = a[3:] or None
        elif a.startswith("CP"):
            self.cp = int(a[2:] or "18", 16)
        elif a.startswith("ST"):
            self.st_ms = max(int(a[2:] or "32", 16) * 4, 40)
        return ["OK"]

    def _request(self, c):
        try:
            data = bytes.fromhex(c)
        except ValueError:
            return ["?"]
        if self.ch is None:
            for p in ([self.want] if self.want else J2534_SEARCH):
                if self._connect(p):
                    lines = self._send(data)
                    if self.want or lines != ["NO DATA"]:
                        return (["SEARCHING..."] if not self.want else []) + lines
                    self._disconnect()
            return ["UNABLE TO CONNECT"]
        return self._send(data)

    # -- connecting ------------------------------------------------------------------
    def _connect(self, p):
        jname, flags, baud = J2534_FOR_ELM[p]
        if jname not in self.protocols and not jname.endswith("_PS"):
            return False
        jp = J2534_PROTO[jname]
        try:
            self.ch = self.api.connect(self.dev, jp, flags, baud)
            self.proto, self.jp, self.phys = p, jp, {}
            if jname == "ISO15765_PS":
                self.api.set_config(self.ch, [(J_J1962_PINS, 0x030B)])   # CAN on pins 3 and 11
            if jp in J2534_CAN:
                if flags & J_CAN_29BIT_ID:
                    pairs = [(0x18DAF100 | e, 0x18DA00F1 | (e << 8)) for e in J2534_29BIT_ECUS]
                else:
                    pairs = [(0x7E8 + i, 0x7E0 + i) for i in range(8)]
                for rx, tx in pairs:
                    self._fc_filter(rx, tx)
            else:
                self.api.start_filter(self.ch, J_PASS_FILTER, jp, b"\x00", b"\x00")
                if jname in ("ISO9141", "ISO14230"):
                    self._kline_init()
            return True
        except J2534Error:
            self._disconnect()
            return False

    def _kline_init(self):
        if self.jp == J2534_PROTO["ISO9141"]:
            self.api.five_baud_init(self.ch, 0x33)
        else:
            self.api.fast_init(self.ch, self.jp, bytes([0xC1, 0x33, 0xF1, 0x81]))
        self.kline_last = time.time()

    def _is29(self):
        return J2534_FOR_ELM[self.proto][1] & J_CAN_29BIT_ID

    def _fc_filter(self, rx, tx):
        size = 4
        tx_flags = J_ISO15765_FRAME_PAD | (J_CAN_29BIT_ID if self._is29() else 0)
        return self.api.start_filter(self.ch, J_FLOW_CONTROL_FILTER, self.jp,
                                     b"\xff" * size, rx.to_bytes(size, "big"),
                                     tx.to_bytes(size, "big"), tx_flags)

    def _ensure_phys(self, rx, tx):
        functional = {(0x7E8 + i, 0x7E0 + i) for i in range(8)} | {
            (0x18DAF100 | e, 0x18DA00F1 | (e << 8)) for e in J2534_29BIT_ECUS}
        if (rx, tx) in functional or (rx, tx) in self.phys:
            return
        while len(self.phys) >= 1:  # one module at a time keeps us well under the filter limit
            old = next(iter(self.phys))
            self.api.stop_filter(self.ch, self.phys.pop(old))
        self.phys[(rx, tx)] = self._fc_filter(rx, tx)

    # -- sending -----------------------------------------------------------------------
    def _send(self, data):
        if self.jp in J2534_CAN:
            return self._send_can(data)
        return self._send_line(data)

    def _send_can(self, data):
        is29 = self._is29()
        hdr = (self.hdr or "").upper()
        if is29:
            functional = len(hdr) != 6
            tx = 0x18DB33F1 if functional else (self.cp << 24) | int(hdr, 16)
            rx = int(self.cra, 16) if self.cra else (None if functional else
                                                     0x18DAF100 | ((tx >> 8) & 0xFF))
        else:
            tx = int(hdr, 16) if hdr and len(hdr) <= 3 else 0x7DF
            functional = tx == 0x7DF
            rx = int(self.cra, 16) if self.cra else (None if functional else tx + 8)
        if not functional and rx is not None:
            self._ensure_phys(rx, tx)
        self.api.ioctl(self.ch, J_CLEAR_RX_BUFFER)
        flags = J_ISO15765_FRAME_PAD | (J_CAN_29BIT_ID if is29 else 0)
        self.api.write(self.ch, self.jp, tx.to_bytes(4, "big") + data, flags)
        lines = []
        for payload in self._collect(150):
            rid, body = int.from_bytes(payload[:4], "big"), list(payload[4:])
            if rx is not None and rid != rx:
                continue
            lines += self._elm_can_lines(rid, body, is29)
        return lines or ["NO DATA"]

    def _send_line(self, data):
        jp = self.jp
        if jp in (J2534_PROTO["ISO9141"], J2534_PROTO["ISO14230"]):
            if time.time() - self.kline_last > 4.0:   # K-line link drops after ~5 s idle
                self._kline_init()
            if jp == J2534_PROTO["ISO14230"]:
                hdr = bytes([0xC0 | len(data), 0x33, 0xF1])
            else:
                hdr = bytes.fromhex(self.hdr) if self.hdr and len(self.hdr) == 6 else b"\x68\x6A\xF1"
        else:
            default = "686AF1" if jp == J2534_PROTO["J1850VPW"] else "616AF1"
            hdr = bytes.fromhex(self.hdr if self.hdr and len(self.hdr) == 6 else default)
        self.api.ioctl(self.ch, J_CLEAR_RX_BUFFER)
        self.api.write(self.ch, jp, hdr + data)
        self.kline_last = time.time()
        lines = []
        for msg in self._collect(300):
            b = list(msg)
            if len(b) < 4:
                continue
            if jp in (J2534_PROTO["ISO9141"], J2534_PROTO["ISO14230"]):
                if (sum(b[:-1]) & 0xFF) != b[-1]:
                    b.append(sum(b) & 0xFF)           # add the checksum the parser expects
            elif _j1850_crc(b[:-1]) != b[-1]:
                b.append(_j1850_crc(b))
            lines.append(" ".join(f"{x:02X}" for x in b))
        return lines or ["NO DATA"]

    def _collect(self, min_ms):
        """Read replies until the line has been quiet for the response timeout (ATST)."""
        window = max(self.st_ms, min_ms) / 1000
        quiet_until, hard_stop, out = time.time() + window, time.time() + 30, []
        while time.time() < min(quiet_until, hard_stop):
            got = self.api.read(self.ch, 20)
            if not got:
                continue
            status, data = got
            if status & J_TX_MSG_TYPE or status & J_TX_DONE:
                continue
            quiet_until = time.time() + window
            if status & J_START_OF_MESSAGE or not data:
                continue
            body = data[4:] if self.jp in J2534_CAN else data[3:]
            if len(body) >= 3 and body[0] == 0x7F and body[2] == 0x78:   # response pending
                quiet_until = time.time() + 5
            out.append(data)
        return out

    @staticmethod
    def _elm_can_lines(rid, payload, is29):
        """Re-frame a reassembled reply as ELM327 prints it with headers on (ATH1 ATS1)."""
        rid_s = " ".join(f"{x:02X}" for x in rid.to_bytes(4, "big")) if is29 else f"{rid:03X}"
        if len(payload) <= 7:
            frames = [[len(payload)] + payload]
        else:
            frames = [[0x10 | (len(payload) >> 8), len(payload) & 0xFF] + payload[:6]]
            rest, seq = payload[6:], 1
            while rest:
                frames.append([0x20 | (seq & 0xF)] + rest[:7])
                rest, seq = rest[7:], seq + 1
        return [rid_s + " " + " ".join(f"{x:02X}" for x in f + [0] * (8 - len(f))) for f in frames]


# ==========================================================================
# Scanner
# ==========================================================================

HEX_TOK = re.compile(r"[0-9A-Fa-f]{2,3}")


def data_frames(lines):
    """Hex-only lines from an ELM reply, as token lists."""
    out = []
    for line in lines:
        line = re.sub(r"^(SEARCHING\.\.\.|BUS INIT:[.\s]*OK)\s*", "", line.strip())
        toks = line.split()
        if toks and all(HEX_TOK.fullmatch(t) for t in toks):
            out.append(toks)
    return out


class Scanner:
    def __init__(self, io, say=lambda s: None):
        self.io, self.say = io, say
        self.protocol = None
        self.is_can = False
        self.id_tokens = 1

    def at(self, cmd, timeout=3.0):
        return self.io.raw(cmd, timeout)

    def connect(self):
        self.at("ATZ", 5)
        self.io.pause(1.0)
        for c in ("ATE0", "ATL0", "ATS1", "ATH1", "ATAT1", "ATSP0"):
            self.at(c)
        adapter = " ".join(self.at("ATI"))
        first = self.io.raw("0100", 25)  # protocol search; K-line init can take ~10 s
        dpn = "".join(self.at("ATDPN")).strip().upper()
        if len(dpn) == 2 and dpn[0] == "A":
            dpn = dpn[1]
        self._set_protocol_state(dpn or None)
        if not self.parse(first):
            joined = " / ".join(first) or "no reply"
            raise RuntimeError(
                f"Vehicle did not answer 0100 ({joined}). Check: key ON; adapter fully "
                "seated; vLinker FS switch set to HS-CAN (the MS-CAN position is for Fords "
                "only and hides the main CAN bus); for a J2534 tool, that it supports this "
                "vehicle's protocol (many dealer tools have no J1850 or K-line).")
        return adapter

    def _set_protocol_state(self, p):
        self.protocol = p
        self.is_can = p in CAN_PROTOCOLS
        self.id_tokens = 4 if p in ("7", "9") else 1

    # ---- response parsing -------------------------------------------------
    def parse(self, lines):
        """Return {ecu_address: [message_bytes, ...]} with ISO-TP reassembled."""
        frames = data_frames(lines)
        out = {}
        if self.is_can:
            n, partial = self.id_tokens, {}
            for t in frames:
                if len(t) <= n:
                    continue
                ecu = "".join(t[:n]).upper()
                b = [int(x, 16) for x in t[n:]]
                kind = b[0] >> 4
                if kind == 0:
                    out.setdefault(ecu, []).append(b[1:1 + (b[0] & 0xF)])
                elif kind == 1 and len(b) >= 2:
                    partial[ecu] = [((b[0] & 0xF) << 8) | b[1], b[2:]]
                elif kind == 2 and ecu in partial:
                    partial[ecu][1] += b[1:]
                    if len(partial[ecu][1]) >= partial[ecu][0]:
                        ln, data = partial.pop(ecu)
                        out.setdefault(ecu, []).append(data[:ln])
            for ecu, (_, data) in partial.items():  # truncated multi-frame
                out.setdefault(ecu, []).append(data)
        else:
            # K-line / J1850 with headers: [priority, target, source] data... checksum
            for t in frames:
                b = [int(x, 16) for x in t]
                if len(b) >= 5:
                    out.setdefault(f"{b[2]:02X}", []).append(b[3:-1])
        return out

    def query(self, req, timeout=5.0):
        return self.parse(self.io.raw(req, timeout))

    # ---- generic OBD-II ---------------------------------------------------
    def status(self):
        res = []
        for ecu, msgs in self.query("0101").items():
            for m in msgs:
                if len(m) >= 6 and m[0] == 0x41 and m[1] == 0x01:
                    a, b, c, d = m[2:6]
                    res.append({"ecu": ecu, "mil_on": bool(a & 0x80), "count": a & 0x7F,
                                "readiness": readiness(b, c, d)})
        return res

    def read_dtcs(self, mode):
        sid = int(mode, 16) + 0x40
        codes, answered = [], False
        for ecu, msgs in self.query(mode, 8).items():
            for m in msgs:
                if not m or m[0] != sid:
                    continue
                answered = True
                body = m[2:] if self.is_can else m[1:]  # CAN carries a count byte
                for i in range(0, len(body) - 1, 2):
                    if body[i] or body[i + 1]:
                        code = decode_dtc(body[i], body[i + 1])
                        if (ecu, code) not in codes:
                            codes.append((ecu, code))
        return codes, answered

    def supported(self, mode="01"):
        """{ecu: [pids]} supported in mode 01 (current data) or 02 (freeze frame 0)."""
        sid, off, sup, base = int(mode, 16) + 0x40, (3 if mode == "02" else 2), {}, 0
        while base <= 0xE0:
            more = False
            for ecu, msgs in self.query(f"{mode}{base:02X}" + ("00" if mode == "02" else "")).items():
                for m in msgs:
                    if len(m) >= off + 4 and m[0] == sid and m[1] == base:
                        bits = int.from_bytes(bytes(m[off:off + 4]), "big")
                        for i in range(32):
                            if bits & (1 << (31 - i)):
                                sup.setdefault(ecu, set()).add(base + i + 1)
                        more = more or bool(bits & 1)
            if not more:
                break
            base += 0x20
        return {e: sorted(p) for e, p in sup.items()}

    def read_pid(self, mode, pid):
        """[(ecu, name, value, unit)] for one PID from every ECU that answers."""
        sid, off, out = int(mode, 16) + 0x40, (3 if mode == "02" else 2), []
        req = f"{mode}{pid:02X}" + ("00" if mode == "02" else "")
        for ecu, msgs in self.query(req).items():
            m = next((m for m in msgs if len(m) > off and m[0] == sid and m[1] == pid), None)
            if m:
                out.append((ecu,) + decode_pid(pid, m[off:]))
        return out

    def _row(self, ecu, pid, name, value, unit):
        return {"ecu": ecu, "pid": f"{pid:02X}", "name": name, "value": value, "unit": unit,
                "display": display(value, unit)}

    def snapshot(self):
        """Read every supported mode 01 PID once."""
        self._sup01 = self.supported("01")
        pids = sorted(set().union(*self._sup01.values()) - SKIP_PIDS) if self._sup01 else []
        rows = []
        for pid in pids:
            for ecu, name, value, unit in self.read_pid("01", pid):
                rows.append(self._row(ecu, pid, name, value, unit))
        return rows

    def freeze_frame(self):
        trig = None
        for msgs in self.query("020200").values():
            for m in msgs:
                if len(m) >= 5 and m[0] == 0x42 and m[1] == 0x02 and (m[3] or m[4]):
                    trig = decode_dtc(m[3], m[4])
        if not trig:
            return None
        rows = [{"ecu": "", "pid": "02", "name": "Code that stored this frame", "value": trig,
                 "unit": "", "display": trig}]
        sup = self.supported("02")
        pids = sorted(set().union(*sup.values()) - SKIP_PIDS) if sup else \
            [0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0F, 0x11]
        for pid in pids:
            for ecu, name, value, unit in self.read_pid("02", pid)[:1]:
                rows.append(self._row(ecu, pid, name, value, unit))
        return rows

    def monitor_tests(self):
        """Mode 06 on-board monitor results (CAN only; older buses use maker formats)."""
        if not self.is_can:
            return None
        mids, base = set(), 0
        while base <= 0xE0:
            more = False
            for ecu, msgs in self.query(f"06{base:02X}").items():
                for m in msgs:
                    if len(m) >= 6 and m[0] == 0x46 and m[1] == base:
                        bits = int.from_bytes(bytes(m[2:6]), "big")
                        for i in range(32):
                            if bits & (1 << (31 - i)):
                                mids.add((ecu, base + i + 1))
                        more = more or bool(bits & 1)
            if not more:
                break
            base += 0x20
        rows = []
        for ecu, mid in sorted(mids):
            if mid % 0x20 == 0:
                continue
            msgs = self.query(f"06{mid:02X}").get(ecu, [])
            for m in msgs:
                if not m or m[0] != 0x46:
                    continue
                recs = m[1:]
                for i in range(0, len(recs) - 8, 9):
                    omid, tid, uas = recs[i:i + 3]
                    vals = [_u16(recs[i + 3 + k * 2:i + 5 + k * 2]) for k in range(3)]
                    if uas >= 0x80:  # signed unit/scaling ids
                        vals = [v - 65536 if v > 32767 else v for v in vals]
                    val, lo, hi = vals
                    test = (MISFIRE_TID.get(tid) if omid >= 0xA1 else None) or f"test 0x{tid:02X}"
                    rows.append({"ecu": ecu, "mid": f"{omid:02X}",
                                 "monitor": OBDMID.get(omid, f"Monitor 0x{omid:02X}"),
                                 "test": test, "unit_id": f"0x{uas:02X}", "value": val,
                                 "min": lo, "max": hi,
                                 "result": "pass" if lo <= val <= hi else "FAIL"})
        return rows

    def ecu_info(self):
        rows = []
        for pid, item, kind in ((0x0A, "ECU name", "ascii"), (0x04, "Calibration ID", "ascii"),
                                (0x06, "Calibration verification number (CVN)", "hex")):
            for ecu, msgs in self.query(f"09{pid:02X}", 8).items():
                parts = [m for m in msgs if len(m) >= 3 and m[0] == 0x49 and m[1] == pid]
                if not parts:
                    continue
                if self.is_can:
                    data = parts[0][3:]
                else:
                    data = [x for m in sorted(parts, key=lambda m: m[2]) for x in m[3:]]
                if kind == "ascii":
                    txt = "".join(chr(x) if 32 <= x < 127 else " " for x in data)
                    if pid == 0x04:
                        val = ", ".join(c for c in (txt[i:i + 16].strip()
                                                    for i in range(0, len(txt), 16)) if c)
                    else:
                        val = " ".join(txt.split())
                else:
                    val = ", ".join(bytes(data[i:i + 4]).hex().upper()
                                    for i in range(0, len(data) - 3, 4))
                if val:
                    rows.append({"ecu": ecu, "item": item, "value": val})
        return rows

    def live(self, pids=None, seconds=0, max_points=60):
        """Sample PIDs repeatedly for `seconds`; summary + downsampled series per PID."""
        sup = getattr(self, "_sup01", None) or self.supported("01")
        avail = set().union(*sup.values()) - SKIP_PIDS if sup else set()
        if pids:
            wanted = [p for p in pids if p in avail]
            missing = [f"{p:02X}" for p in pids if p not in avail]
        else:
            wanted, missing = [p for p in DEFAULT_LIVE if p in avail], []
        series, t0 = {p: [] for p in wanted}, time.time()
        while True:
            for p in wanted:
                got = self.read_pid("01", p)
                if got:
                    ecu, name, value, unit = got[0]
                    series[p].append((round(time.time() - t0, 2), value, name, unit))
            if time.time() - t0 >= seconds:
                break
        out = []
        for p, pts in series.items():
            if not pts:
                continue
            name, unit = pts[-1][2], pts[-1][3]
            nums = [v for _, v, _, _ in pts if isinstance(v, (int, float))]
            step = max(1, len(pts) // max_points)
            row = {"pid": f"{p:02X}", "name": name, "unit": unit, "samples": len(pts),
                   "last": pts[-1][1], "display": display(pts[-1][1], unit),
                   "series": [[t, v] for t, v, _, _ in pts[::step]]}
            if nums:
                row.update(min=min(nums), max=max(nums), mean=round(sum(nums) / len(nums), 2))
            out.append(row)
        return {"seconds": round(time.time() - t0, 1), "pids": out,
                "not_supported": missing,
                "supported": [f"{p:02X}" for p in sorted(avail)]}

    def vin(self):
        for msgs in self.query("0902", 8).values():
            parts = [m for m in msgs if len(m) >= 3 and m[0] == 0x49 and m[1] == 0x02]
            if not parts:
                continue
            if self.is_can:
                data = parts[0][3:]
            else:
                data = [x for m in sorted(parts, key=lambda m: m[2]) for x in m[3:]]
            s = "".join(chr(x) for x in data if 32 < x < 127)
            if len(s) >= 17:
                return s[-17:]
        return None

    def clear(self):
        r = self.query("04", 10)
        return any(m and m[0] == 0x44 for msgs in r.values() for m in msgs)

    # ---- module reads -----------------------------------------------------
    def _restore(self):
        if self.protocol in ("1", "2"):
            cmds = ["ATSH 686AF1" if self.protocol == "2" else "ATSH 616AF1"]
        else:
            cmds = ["ATSH 7DF", "ATCRA"]
        for c in cmds + ["ATST 32", "ATAT1"]:
            self.at(c)

    def _try_requests(self, entry, requests, uudt=None):
        """Send each read request until one is answered; fill entry in place."""
        for req, pos, label in requests:
            if label == "GMLAN 0xA9":
                if uudt is None:
                    continue
                codes = self._gmlan_a9(entry["_req"], entry["_rx"], uudt)
                if codes is not None:
                    entry.update(status="read OK", method=label, codes=codes)
                    return
                continue
            msgs = [m for ms in self.query(req, 4).values() for m in ms if m]
            if not msgs:
                continue
            ok = [m for m in msgs if m[0] == pos]
            if ok:
                entry["status"], entry["method"] = "read OK", label
                for m in ok:
                    for code, st in parse_module_reply(m, label, self.is_can):
                        if code not in [c["code"] for c in entry["codes"]]:
                            entry["codes"].append({"code": code, "status": st})
                return
            neg = next((m for m in msgs if m[0] == 0x7F and len(m) >= 3), None)
            if neg:
                entry["status"] = (f"responds, but rejected code requests "
                                   f"(last: {NRC.get(neg[2], f'NRC 0x{neg[2]:02X}')})")

    def _gmlan_a9(self, req, rx, uudt):
        """GMLAN readDiagnosticInformation 0xA9: DTCs come back as UUDT frames."""
        self.at(f"ATCRA {uudt:03X}")
        lines = self.io.raw("A9 81 12", 4)
        self.at(f"ATCRA {rx:03X}")
        codes, seen = [], False
        for t in data_frames(lines):
            b = [int(x, 16) for x in t[1:]]
            if len(b) >= 5 and b[0] == 0x81:
                seen = True
                h, l, ftb, st = b[1:5]
                if h or l:
                    codes.append({"code": decode_dtc(h, l) + (f"-{ftb:02X}" if ftb else ""),
                                  "status": f"0x{st:02X}"})
        return codes if seen else None

    def _new_entry(self, address, name, req=None, rx=None):
        return {"address": address, "module": name, "status": "not found", "method": None,
                "codes": [], "_req": req, "_rx": rx}

    def read_can11(self, modules, make):
        results = []
        self.at("ATST 96")  # ~600 ms reply window
        for req, rx, name in modules:
            self.at(f"ATSH {req:03X}")
            self.at(f"ATCRA {rx:03X}")
            e = self._new_entry(f"{req:03X}", name, req, rx)
            gmlan = make == "gm" and (0x240 <= req <= 0x25F or req in GM_UUDT)
            uudt = GM_UUDT.get(req, req + 0x300 if 0x240 <= req <= 0x25F else None)
            self._try_requests(e, REQ["gmlan"] + REQ["can"][1:] if gmlan else REQ["can"], uudt)
            results.append(e)
        self._restore()
        return results

    def read_ms_can(self, modules, sweep=False):
        """Ford MS-CAN (pins 3/11, 125 kbps). Returns None if the adapter can't do it."""
        saved = self.protocol
        if not isinstance(self.io, J2534Adapter) or self.at("ATSPM") != ["OK"]:
            return None                     # ELM-type adapters can't switch to pins 3/11
        try:
            mods = list(modules)
            if sweep:
                known = {m[0] for m in mods}
                ids = [a for a in range(0x700, 0x7F8) if a != 0x7DF]
                mods += [(r, x, f"MS-CAN module @ {r:03X}")
                         for r, x in self.sweep_can11(ids, lambda r: r + 8, "MS-CAN")
                         if r not in known]
            res = self.read_can11(mods, "ford")
            for r in res:
                r["module"] += " [MS-CAN]"
            return res
        finally:
            self.at(f"ATSP{saved}")
            self._set_protocol_state(saved)
            self._restore()

    def _enter_can29(self):
        if self.protocol in ("6", "8"):
            self.at("ATSP7" if self.protocol == "6" else "ATSP9")
        self.at("ATCP 18")
        saved = self.protocol
        self._set_protocol_state("7" if saved in ("6", "7") else "9")
        return saved

    def _leave_can29(self, saved):
        if saved != self.protocol:
            self.at(f"ATSP{saved}")
        self._set_protocol_state(saved)
        self._restore()

    def read_can29(self, targets):
        results = []
        saved = self._enter_can29()
        self.at("ATST 96")
        for tgt, name in targets:
            self.at(f"ATSH DA{tgt:02X}F1")
            self.at(f"ATCRA 18DAF1{tgt:02X}")
            e = self._new_entry(f"18DA{tgt:02X}F1", name)
            self._try_requests(e, REQ["can"][:3])
            results.append(e)
        self._leave_can29(saved)
        return results

    def read_j1850(self, targets, make):
        results = []
        hdr = J1850_HEADER[make]
        self.at("ATST 96")
        for tgt, name in targets:
            self.at(f"ATSH {hdr}{tgt:02X}F1")
            e = self._new_entry(f"{tgt:02X}", name)
            self._try_requests(e, REQ[f"j1850_{make}"])
            results.append(e)
        self._restore()
        return results

    # ---- discovery sweeps -------------------------------------------------
    def _alive(self, probes, timeout=1):
        for p in probes:
            if self.query(p, timeout):
                return True
        return False

    def sweep_can11(self, ids, rx_of, label):
        found = []
        self.at("ATST 19")  # ~100 ms
        for i, req in enumerate(ids):
            if i % 16 == 0:
                self.say(f"  {label}: {req:03X}-{ids[min(i + 15, len(ids) - 1)]:03X} ...")
            self.at(f"ATSH {req:03X}")
            self.at(f"ATCRA {rx_of(req):03X}")
            if self._alive(["3E 00", "10 01"]):
                found.append((req, rx_of(req)))
        self._restore()
        return found

    def sweep_can29(self):
        found = []
        saved = self._enter_can29()
        self.at("ATST 19")
        for tgt in range(0x00, 0x100):
            if tgt == 0xF1:
                continue
            if tgt % 32 == 0:
                self.say(f"  29-bit CAN: 18DA{tgt:02X}F1-18DA{min(tgt + 31, 255):02X}F1 ...")
            self.at(f"ATSH DA{tgt:02X}F1")
            self.at(f"ATCRA 18DAF1{tgt:02X}")
            if self._alive(["3E 00", "10 01"]):
                found.append(tgt)
        self._leave_can29(saved)
        return found

    def sweep_j1850(self, make):
        found = []
        hdr, (req, _, _) = J1850_HEADER[make], REQ[f"j1850_{make}"][0]
        self.at("ATST 19")
        for tgt in range(0x00, 0xFF):
            if tgt == 0xF1:
                continue
            if tgt % 32 == 0:
                self.say(f"  J1850 targets {tgt:02X}-{min(tgt + 31, 0xFE):02X} ...")
            self.at(f"ATSH {hdr}{tgt:02X}F1")
            if self.query(req, 1):
                found.append(tgt)
        self._restore()
        return found


def parse_module_reply(m, label, is_can):
    body, out = m[1:], []
    if label == "UDS 0x19":                       # 59 02 mask [H M L status]...
        recs = body[2:]
        for i in range(0, len(recs) - 3, 4):
            h, mid, low, st = recs[i:i + 4]
            if h or mid or low:
                out.append((decode_dtc(h, mid) + (f"-{low:02X}" if low else ""), uds_status(st)))
    elif label == "Class 2 0x19":                 # 59 [H L status] per frame
        for i in range(0, len(body) - 2, 3):
            h, low, st = body[i:i + 3]
            if h or low:
                out.append((decode_dtc(h, low), f"0x{st:02X}"))
    elif label == "KWP 0x18":                     # 58 count [H L status]...
        recs = body[1:]
        for i in range(0, len(recs) - 2, 3):
            h, low, st = recs[i:i + 3]
            if h or low:
                out.append((decode_dtc(h, low), f"0x{st:02X}"))
    else:                                         # 53 / 43 [count] [H L]...
        recs = body
        if recs and ((label.startswith("OBD") and is_can) or recs[0] * 2 == len(recs) - 1):
            recs = recs[1:]
        for i in range(0, len(recs) - 1, 2):
            if recs[i] or recs[i + 1]:
                out.append((decode_dtc(recs[i], recs[i + 1]), ""))
    return out


def readiness(b, c, d):
    def mon(name, sup, inc):
        return {"monitor": name,
                "status": ("incomplete" if inc else "complete") if sup else "n/a"}
    rows = [mon("Misfire", b & 0x01, b & 0x10), mon("Fuel system", b & 0x02, b & 0x20),
            mon("Components", b & 0x04, b & 0x40)]
    if b & 0x08:  # compression ignition (diesel)
        names = ["NMHC catalyst", "NOx/SCR", None, "Boost pressure", None,
                 "Exhaust gas sensor", "PM filter", "EGR/VVT"]
    else:
        names = ["Catalyst", "Heated catalyst", "EVAP", "Secondary air", "A/C refrigerant",
                 "O2 sensor", "O2 sensor heater", "EGR"]
    for bit, name in enumerate(names):
        if name:
            rows.append(mon(name, c & (1 << bit), d & (1 << bit)))
    return [r for r in rows if r["status"] != "n/a"]


def _u16(d):
    return d[0] * 256 + d[1]


def _s16(d):
    v = _u16(d)
    return v - 65536 if v > 32767 else v


def _pct(d):
    return round(d[0] * 100 / 255, 1)


def _trim(d):
    return round((d[0] - 128) * 100 / 128, 1)


def _km(d):
    return _u16(d)


FUEL_SYS = {1: "open loop (not warm yet)", 2: "closed loop", 4: "open loop (load/decel)",
            8: "open loop (system fault)", 16: "closed loop (feedback fault)"}
FUEL_TYPE = {1: "gasoline", 2: "methanol", 3: "ethanol", 4: "diesel", 5: "LPG", 6: "CNG",
             8: "electric", 9: "bifuel gasoline", 0x10: "hybrid gasoline",
             0x11: "hybrid ethanol", 0x12: "hybrid diesel", 0x13: "hybrid electric"}
O2_POS = ["B1S1", "B1S2", "B1S3", "B1S4", "B2S1", "B2S2", "B2S3", "B2S4"]


# pid: (name, unit, bytes, decoder) - SAE J1979 / ISO 15031-5 mode 01 and 02 formulas
PIDS = {
    0x03: ("Fuel system status", "", 2,
           lambda d: ", ".join(FUEL_SYS.get(x, f"0x{x:02X}") for x in d[:2] if x) or "n/a"),
    0x04: ("Calculated engine load", "%", 1, _pct),
    0x05: ("Coolant temp", "°C", 1, lambda d: d[0] - 40),
    0x06: ("Short-term fuel trim bank 1", "%", 1, _trim),
    0x07: ("Long-term fuel trim bank 1", "%", 1, _trim),
    0x08: ("Short-term fuel trim bank 2", "%", 1, _trim),
    0x09: ("Long-term fuel trim bank 2", "%", 1, _trim),
    0x0A: ("Fuel pressure (gauge)", "kPa", 1, lambda d: d[0] * 3),
    0x0B: ("Intake manifold pressure (MAP)", "kPa", 1, lambda d: d[0]),
    0x0C: ("Engine RPM", "rpm", 2, lambda d: round(_u16(d) / 4)),
    0x0D: ("Vehicle speed", "km/h", 1, lambda d: d[0]),
    0x0E: ("Ignition timing advance (cyl 1)", "° BTDC", 1, lambda d: d[0] / 2 - 64),
    0x0F: ("Intake air temp", "°C", 1, lambda d: d[0] - 40),
    0x10: ("Mass air flow (MAF)", "g/s", 2, lambda d: round(_u16(d) / 100, 2)),
    0x11: ("Throttle position", "%", 1, _pct),
    0x1F: ("Run time since engine start", "s", 2, _u16),
    0x21: ("Distance driven with check-engine light on", "km", 2, _km),
    0x22: ("Fuel rail pressure (relative to manifold)", "kPa", 2,
           lambda d: round(_u16(d) * 0.079, 1)),
    0x23: ("Fuel rail pressure (gauge)", "kPa", 2, lambda d: _u16(d) * 10),
    0x2C: ("Commanded EGR", "%", 1, _pct),
    0x2D: ("EGR error", "%", 1, _trim),
    0x2E: ("Commanded EVAP purge", "%", 1, _pct),
    0x2F: ("Fuel tank level", "%", 1, _pct),
    0x30: ("Warm-ups since codes cleared", "", 1, lambda d: d[0]),
    0x31: ("Distance since codes cleared", "km", 2, _km),
    0x32: ("EVAP system vapor pressure", "Pa", 2, lambda d: _s16(d) / 4),
    0x33: ("Barometric pressure", "kPa", 1, lambda d: d[0]),
    0x42: ("Control module (battery) voltage", "V", 2, lambda d: round(_u16(d) / 1000, 2)),
    0x43: ("Absolute load", "%", 2, lambda d: round(_u16(d) * 100 / 255, 1)),
    0x44: ("Commanded air-fuel ratio (lambda)", "", 2, lambda d: round(_u16(d) * 2 / 65536, 3)),
    0x45: ("Relative throttle position", "%", 1, _pct),
    0x46: ("Ambient air temp", "°C", 1, lambda d: d[0] - 40),
    0x47: ("Absolute throttle position B", "%", 1, _pct),
    0x48: ("Absolute throttle position C", "%", 1, _pct),
    0x49: ("Accelerator pedal position D", "%", 1, _pct),
    0x4A: ("Accelerator pedal position E", "%", 1, _pct),
    0x4B: ("Accelerator pedal position F", "%", 1, _pct),
    0x4C: ("Commanded throttle actuator", "%", 1, _pct),
    0x4D: ("Time run with check-engine light on", "min", 2, _u16),
    0x4E: ("Time since codes cleared", "min", 2, _u16),
    0x51: ("Fuel type", "", 1, lambda d: FUEL_TYPE.get(d[0], f"0x{d[0]:02X}")),
    0x52: ("Ethanol fuel", "%", 1, _pct),
    0x59: ("Fuel rail absolute pressure", "kPa", 2, lambda d: _u16(d) * 10),
    0x5A: ("Relative accelerator pedal position", "%", 1, _pct),
    0x5B: ("Hybrid battery remaining", "%", 1, _pct),
    0x5C: ("Engine oil temp", "°C", 1, lambda d: d[0] - 40),
    0x5E: ("Engine fuel rate", "L/h", 2, lambda d: round(_u16(d) / 20, 2)),
    0x61: ("Driver demand torque", "%", 1, lambda d: d[0] - 125),
    0x62: ("Actual engine torque", "%", 1, lambda d: d[0] - 125),
    0x63: ("Engine reference torque", "Nm", 2, _u16),
    0xA6: ("Odometer", "km", 4, lambda d: round(int.from_bytes(bytes(d[:4]), "big") / 10, 1)),
}
for _i, _pos in enumerate(O2_POS):
    PIDS[0x14 + _i] = (f"O2 sensor {_pos} voltage (narrow-band)", "V", 2,
                       lambda d: round(d[0] / 200, 3))
    PIDS[0x24 + _i] = (f"O2 sensor {_pos} lambda (wide-band)", "lambda", 4,
                       lambda d: round(_u16(d) * 2 / 65536, 3))
    PIDS[0x34 + _i] = (f"O2 sensor {_pos} lambda (wide-band, current type)", "lambda", 4,
                       lambda d: round(_u16(d) * 2 / 65536, 3))
for _pid, _pos in ((0x3C, "B1S1"), (0x3D, "B2S1"), (0x3E, "B1S2"), (0x3F, "B2S2")):
    PIDS[_pid] = (f"Catalyst temp {_pos}", "°C", 2, lambda d: round(_u16(d) / 10 - 40))

OBD_STANDARD = {1: "OBD-II (CARB)", 2: "OBD (federal EPA)", 3: "OBD and OBD-II", 4: "OBD-I",
                5: "not OBD compliant", 6: "EOBD (Europe)", 7: "EOBD and OBD-II",
                8: "EOBD and OBD", 9: "EOBD, OBD and OBD-II", 10: "JOBD (Japan)",
                11: "JOBD and OBD-II", 12: "JOBD and EOBD", 13: "JOBD, EOBD and OBD-II"}


def _o2_present(d):
    names = [p for i, p in enumerate(O2_POS) if d[0] & (1 << i)]
    return ", ".join(names) or "none"


def _monitors_this_cycle(d):
    rows = readiness(d[1], d[2], d[3])
    todo = [r["monitor"] for r in rows if r["status"] == "incomplete"]
    return ("not yet complete this drive: " + ", ".join(todo)) if todo else \
        "all enabled monitors complete this drive"


PIDS[0x13] = ("O2 sensors present", "", 1, _o2_present)
PIDS[0x1C] = ("OBD standard", "", 1, lambda d: OBD_STANDARD.get(d[0], f"type {d[0]}"))
PIDS[0x41] = ("Monitor status this drive cycle", "", 4, _monitors_this_cycle)
PIDS[0x53] = ("EVAP system absolute vapor pressure", "kPa", 2, lambda d: round(_u16(d) / 200, 2))

SKIP_PIDS = {0x00, 0x01, 0x02, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0}
DEFAULT_LIVE = [0x0C, 0x0D, 0x05, 0x04, 0x03, 0x06, 0x07, 0x08, 0x09, 0x0B, 0x10, 0x0E, 0x0F,
                0x11, 0x14, 0x15, 0x18, 0x19, 0x24, 0x25, 0x34, 0x35, 0x38, 0x42, 0x44, 0x2F]


def decode_pid(pid, d):
    """-> (name, value, unit). Unknown PIDs come back as raw hex."""
    if pid in PIDS:
        name, unit, n, fn = PIDS[pid]
        if len(d) >= n:
            try:
                return name, fn(d), unit
            except Exception:
                pass
    return f"PID {pid:02X} (not decoded)", " ".join(f"{x:02X}" for x in d), "raw hex"


def display(value, unit):
    if isinstance(value, (int, float)):
        if unit == "°C":
            return f"{value} °C ({round(value * 9 / 5 + 32)} °F)"
        if unit == "km/h":
            return f"{value} km/h ({round(value * 0.621)} mph)"
        if unit == "km":
            return f"{value} km ({round(value * 0.621)} mi)"
        if unit == "kPa":
            return f"{value} kPa ({round(value * 0.145, 1)} psi)"
    return f"{value} {unit}".strip()


# Mode 06 (CAN) on-board monitor IDs
OBDMID = {0x21: "Catalyst bank 1", 0x22: "Catalyst bank 2", 0x31: "EGR bank 1",
          0x32: "EGR bank 2", 0x35: "VVT bank 1", 0x36: "VVT bank 2",
          0x39: "EVAP 0.150 in leak", 0x3A: "EVAP 0.090 in leak", 0x3B: "EVAP 0.040 in leak",
          0x3C: "EVAP 0.020 in leak", 0x3D: "EVAP purge flow", 0x61: "Heated catalyst bank 1",
          0x62: "Heated catalyst bank 2", 0x71: "Secondary air 1", 0x72: "Secondary air 2",
          0x81: "Fuel system bank 1", 0x82: "Fuel system bank 2", 0xA1: "Misfire (general)"}
for _i, _pos in enumerate(O2_POS):
    OBDMID[0x01 + _i] = f"O2 sensor {_pos}"
    OBDMID[0x41 + _i] = f"O2 sensor heater {_pos}"
for _c in range(1, 13):
    OBDMID[0xA1 + _c] = f"Misfire cylinder {_c}"
MISFIRE_TID = {0x0B: "misfire count, average of last 10 drive cycles",
               0x0C: "misfire count, last/current drive cycle"}


# ==========================================================================
# Scan run
# ==========================================================================

def plan_and_read_modules(sc, make, sweep, report, say):
    """Choose module addresses for this make + bus, run discovery, read codes."""
    notes = report["notes"]
    p = sc.protocol
    results = []

    if p in ("6", "8"):  # 11-bit CAN
        modules = list(CAN11_MODULES.get(make, CAN11_MODULES["other"]))
        known = {m[0] for m in modules}
        extra = []
        if make == "gm":
            say("Looking for GM (GMLAN) modules 241-25F ...")
            extra += sc.sweep_can11(list(range(0x241, 0x260)), lambda r: r + 0x400, "GMLAN")
        if make == "chrysler":
            say("Looking for Chrysler modules 740-75F ...")
            extra += sc.sweep_can11(list(range(0x740, 0x760)), lambda r: r - 0x280, "Chrysler")
        if sweep:
            say("Sweeping 11-bit CAN addresses 700-7F7 (~1-2 min) ...")
            ids = [a for a in range(0x700, 0x7F8) if a != 0x7DF]
            extra += sc.sweep_can11(ids, lambda r: r + 8, "11-bit CAN")
        for req, rx in extra:
            if req not in known:
                known.add(req)
                modules.append((req, rx, f"Module @ {req:03X}"))
        say(f"Reading {len(modules)} modules on 11-bit CAN ...")
        results += sc.read_can11(modules, make)

        if make == "ford":
            say("Reading Ford MS-CAN modules (pins 3/11) ...")
            ms = sc.read_ms_can(FORD_MS_MODULES, sweep)
            if ms is None:
                notes.append("Ford MS-CAN modules (climate, doors, audio and other comfort "
                             "modules) weren't read: this adapter can't switch to OBD pins 3/11. "
                             "They need a J2534 tool that supports MS-CAN, such as the Ford VCM "
                             "II, VCM 3 or VCMM.")
            else:
                results += ms

        if make == "honda" or (sweep and make == "other"):
            say("Looking for 29-bit CAN modules (Honda style, ~1-2 min) ...")
            tgts = sc.sweep_can29()
            if tgts:
                results += sc.read_can29([(t, f"Module @ 18DA{t:02X}F1") for t in tgts])
            elif make == "honda":
                notes.append("No 29-bit CAN modules answered. On some Hondas the ABS/SRS "
                             "units are not reachable from the OBD port with this cable.")
        if make == "gm":
            notes.append("GM module codes on GMLAN vehicles use the GMLAN 0xA9 request "
                         "where UDS isn't supported; that path is experimental.")
        if make == "chrysler":
            notes.append("Chrysler/Jeep vehicles from about 2018 on have a security gateway; "
                         "some modules refuse requests unless the gateway is bypassed or "
                         "unlocked. Engine codes and clearing are not affected.")

    elif p in ("7", "9"):  # 29-bit CAN as main bus
        say("Looking for 29-bit CAN modules ...")
        tgts = sc.sweep_can29() if sweep or make in ("honda", "other") else [0x10, 0x18]
        results += sc.read_can29([(t, f"Module @ 18DA{t:02X}F1") for t in tgts])

    elif p == "2" and make in ("gm", "chrysler"):  # J1850 VPW
        targets = list(J1850_MODULES[make])
        if sweep:
            say("Sweeping J1850 module targets 00-FE ...")
            known = {t for t, _ in targets}
            targets += [(t, f"Module @ {t:02X}") for t in sc.sweep_j1850(make) if t not in known]
        say(f"Reading {len(targets)} modules on J1850 VPW (experimental) ...")
        j = sc.read_j1850(targets, make)
        for r in j:
            if r["method"]:
                r["method"] += " (J1850)"
        results += j
        notes.append(PRE_CAN_NOTES[make])

    else:
        notes.append(PRE_CAN_NOTES.get(make, PRE_CAN_NOTES["other"]))

    for r in results:
        r.pop("_req", None)
        r.pop("_rx", None)
        if r["method"] and (r["method"] in EXPERIMENTAL or r["method"].endswith("(J1850)")):
            r["method"] += " (experimental)"
    if p in CAN_PROTOCOLS and not sweep:
        notes.append("Turn on Sweep to look for modules that aren't in the "
                     "built-in list for this make.")
    return results


def run_scan(io, args, say=print, sc=None):
    """Full scan. Pass `sc` (an already-connected Scanner) to reuse an open session."""
    log_start = len(io.log)
    connected = sc is not None
    if sc is None:
        sc = Scanner(io, say)
    sc.say = say
    now = datetime.datetime.now().isoformat(timespec="seconds")
    report = {"scanned_at": now, "adapter": None, "protocol": None, "vin": None,
              "vehicle": {"make": None, "make_name": None, "make_source": None, "year": None},
              "mil": [], "readiness": [], "codes": [], "modules": [],
              "freeze_frame": [], "live_snapshot": [], "supported_pids": {},
              "monitor_tests": [], "ecu_info": [], "notes": [], "raw_log": []}
    try:
        say("Connecting (protocol search can take 10-20 s on older vehicles) ...")
        if not connected:
            sc.adapter_id = sc.connect()
        report["adapter"] = sc.adapter_id
        report["protocol"] = PROTOCOLS.get(sc.protocol, sc.protocol)
        say(f"Adapter: {report['adapter']}   Protocol: {report['protocol']}")

        vin = sc.vin()
        report["vin"] = vin
        vin_make = make_from_vin(vin)
        if args.make != "auto":
            make, source = args.make, "selected"
        elif vin_make:
            make, source = vin_make, "VIN"
        else:
            make, source = "other", "unknown"
            report["notes"].append("The vehicle didn't report a VIN, so its make is unknown. "
                                   "Pick the make and scan again to read its modules.")
        report["vehicle"] = {"make": make, "make_name": MAKES[make], "make_source": source,
                             "year": year_from_vin(vin)}
        if args.make != "auto" and vin_make and vin_make != args.make:
            report["notes"].append(f"The VIN looks like {MAKES[vin_make]}, but you picked "
                                   f"{MAKES[args.make]}. The scan used your choice.")

        for s in sc.status():
            report["mil"].append({"ecu": s["ecu"], "mil_on": s["mil_on"], "count": s["count"]})
            if not report["readiness"]:
                report["readiness"] = s["readiness"]

        def add(source, module, address, kind, code, status=""):
            report["codes"].append({"source": source, "module": module, "address": address,
                                    "type": kind, "code": code, "status": status,
                                    "description": describe(code)})

        ecu_names = {"7E8": "Engine", "7E9": "Transmission", "7EA": "Transmission",
                     "10": "Engine", "11": "Engine", "18": "Transmission",
                     "18DAF110": "Engine", "18DAF118": "Transmission"}
        for mode, kind in (("03", "stored"), ("07", "pending"), ("0A", "permanent")):
            codes, answered = sc.read_dtcs(mode)
            if not answered:
                report["notes"].append(f"Mode {mode} ({kind} codes) not supported or no reply.")
            for ecu, code in codes:
                add("OBD-II", ecu_names.get(ecu, f"ECU {ecu}"), ecu, kind, code)

        report["freeze_frame"] = sc.freeze_frame() or []
        say("Reading live sensor data, monitor tests and ECU info ...")
        report["live_snapshot"] = sc.snapshot()
        report["supported_pids"] = {e: [f"{p:02X}" for p in ps]
                                    for e, ps in getattr(sc, "_sup01", {}).items()}
        mt = sc.monitor_tests()
        report["monitor_tests"] = mt or []
        if mt is None:
            report["notes"].append("Monitor test results (Mode 06) are only decoded on CAN "
                                   "vehicles; older vehicles use maker-specific formats.")
        report["ecu_info"] = sc.ecu_info()

        if not args.no_modules:
            report["modules"] = plan_and_read_modules(sc, make, args.sweep, report, say)
            for m in report["modules"]:
                for c in m["codes"]:
                    add("Module", m["module"], m["address"], "module DTC", c["code"], c["status"])
            generic = {c["code"] for c in report["codes"] if c["source"] == "OBD-II"}
            if any(c["code"].split("-")[0] in generic for m in report["modules"] for c in m["codes"]):
                report["notes"].append("Module codes can repeat the engine's generic codes; "
                                       "that's the same fault read two ways.")

        if args.clear:
            if args.yes or input("Clear codes? This also resets readiness monitors. "
                                 "Type YES: ").strip() == "YES":
                ok = sc.clear()
                report["notes"].append("Codes cleared." if ok else
                                       "Clear request not acknowledged.")
    except RuntimeError as e:
        report["notes"].append(str(e))
        say(f"ERROR: {e}")
    finally:
        report["raw_log"] = [{"tx": e["tx"], "rx": " | ".join(e["rx"])}
                             for e in io.log[log_start:]]
    return report


def print_report(r):
    line = "-" * 78
    v = r["vehicle"]
    print(f"\n{line}\nScan {r['scanned_at']}   VIN: {r['vin'] or 'not reported'}")
    print(f"Vehicle: {v['make_name']} {v['year'] or ''} ({v['make_source']})   "
          f"Protocol: {r['protocol']}")
    for m in r["mil"]:
        print(f"ECU {m['ecu']}: check-engine light {'ON' if m['mil_on'] else 'off'}, "
              f"{m['count']} stored code(s) reported")
    print(line)
    if r["codes"]:
        print(f"{'CODE':<10}{'TYPE':<12}{'MODULE':<22}DESCRIPTION")
        for c in r["codes"]:
            print(f"{c['code']:<10}{c['type']:<12}{c['module'][:21]:<22}{c['description']}")
            if c["status"]:
                print(f"{'':<44}status {c['status']}")
    else:
        print("No codes found.")
    if r["modules"]:
        print(f"{line}\nModules")
        for m in r["modules"]:
            via = f" via {m['method']}" if m["method"] else ""
            print(f"  {m['address']:<9}{m['module']:<26}{m['status']}{via}, "
                  f"{len(m['codes'])} code(s)")
    if r["freeze_frame"]:
        print(f"{line}\nFreeze frame")
        for f in r["freeze_frame"]:
            print(f"  {f['name']:<48}{f['display']}")
    if r.get("live_snapshot"):
        print(f"{line}\nLive data (snapshot)")
        for f in r["live_snapshot"]:
            print(f"  {f['ecu']:<5}{f['name']:<48}{f['display']}")
    if r.get("monitor_tests"):
        fails = [t for t in r["monitor_tests"] if t["result"] == "FAIL"]
        print(f"{line}\nMonitor tests: {len(r['monitor_tests'])} run, {len(fails)} failed")
        for t in fails:
            print(f"  FAIL {t['monitor']} - {t['test']}: {t['value']} (limits {t['min']}-{t['max']})")
    if r["readiness"]:
        print(f"{line}\nReadiness: " +
              ", ".join(f"{x['monitor']} {x['status']}" for x in r["readiness"]))
    for n in r["notes"]:
        print(f"NOTE: {n}")
    print(line)


def write_csv(r, path):
    cols = ["scanned_at", "vin", "make", "year", "source", "module", "address", "type",
            "code", "status", "description"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in r["codes"]:
            w.writerow({"scanned_at": r["scanned_at"], "vin": r["vin"],
                        "make": r["vehicle"]["make_name"], "year": r["vehicle"]["year"], **c})


# ==========================================================================
# OnlyScans app: local web interface, persistent adapter session, AI mechanic
# ==========================================================================

import contextlib
import hashlib
import os
import secrets
import subprocess
import threading
import traceback
import types
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = "dev"   # the release build replaces this with the release tag, e.g. "1.2.0"
APP_PORT = 8765
UPDATE_API = os.environ.get("ONLYSCANS_UPDATE_URL",
                            "https://api.github.com/repos/rfitting/OnlyScans/releases/latest")
UPDATE_EXIT_CODE = 42   # tells the launcher to restart into the new OnlyScans.exe


def parse_version(v):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None
AI_MODEL = "claude-sonnet-5-5"     # latest Sonnet; temperature/top_p/top_k are rejected
AI_URL = os.environ.get("ONLYSCANS_API_URL", "https://api.anthropic.com/v1/messages")
AI_MAX_TOKENS = 16000              # room for adaptive thinking + the answer
AI_STEPS = 10                      # max tool round-trips per question
PRICE_IN, PRICE_OUT = 2 / 1e6, 10 / 1e6   # USD per token, Sonnet 5.5


def config_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = os.path.join(base, "OnlyScans")
    os.makedirs(d, exist_ok=True)
    return d


def load_config():
    try:
        with open(os.path.join(config_dir(), "config.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(os.path.join(config_dir(), "config.json"), "w") as f:
        json.dump(cfg, f, indent=2)


def ensure_pyserial():
    """Install pyserial on first run if it's missing (not needed in the packaged .exe)."""
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        return
    except ImportError:
        if getattr(sys, "frozen", False):
            raise
    print("First run: installing pyserial (needed to talk to the adapter) ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "--quiet", "pyserial"])
    import importlib
    import site
    usp = site.getusersitepackages()
    if usp not in sys.path:
        sys.path.append(usp)
    importlib.invalidate_caches()


def make_adapter(port, baud=None):
    if port.lower().startswith(J2534_PREFIX):
        return J2534Adapter(port)
    return SerialAdapter(port, baud)


def open_adapter(port):
    try:
        return make_adapter(port)
    except SystemExit as e:  # SerialAdapter reports problems via sys.exit for the CLI
        raise RuntimeError(str(e))


class Status:
    """What the app is doing right now; the browser polls it to show progress."""
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = 0
        self.message = ""

    def say(self, msg):
        with self.lock:
            self.message = str(msg).strip()

    @contextlib.contextmanager
    def task(self, msg):
        with self.lock:
            self.busy += 1
            self.message = msg
        try:
            yield
        finally:
            with self.lock:
                self.busy -= 1
                if not self.busy:
                    self.message = ""

    def get(self):
        with self.lock:
            return {"busy": self.busy > 0, "message": self.message}


class AdapterSession:
    """Keeps one open, initialised connection to the adapter between actions."""
    def __init__(self):
        self.lock = threading.RLock()
        self.port = self.io = self.sc = None

    def close(self):
        if self.io:
            try:
                self.io.close()
            except Exception:
                pass
        self.port = self.io = self.sc = None

    def scanner(self, port, say, fresh=False):
        if fresh or self.sc is None or self.port != port:
            self.close()
            io = open_adapter(port)
            try:
                sc = Scanner(io, say)
                sc.adapter_id = sc.connect()
            except Exception:
                io.close()
                raise
            self.port, self.io, self.sc = port, io, sc
        self.sc.say = say
        return self.sc


# ---- AI mechanic -----------------------------------------------------------

AI_SYSTEM = """You are the AI mechanic inside OnlyScans, an OBD-II scan app used by a hands-on DIY
mechanic who does their own repairs. Below is the complete result of the most recent scan as JSON.
It contains:
- vehicle: make and model year (from the VIN or picked by the user), protocol, VIN.
- mil / codes: check-engine status and every code - generic stored, pending and permanent codes
  plus codes read directly from modules (ABS, airbag, body...).
- modules: each module tried and how it answered.
- freeze_frame: conditions when the first code was stored.
- live_snapshot: one reading of every supported sensor at scan time. Check engine RPM (PID 0C)
  to see whether the engine was running; key-on/engine-off values are not meaningful for most
  sensors.
- monitor_tests: Mode 06 self-test results. The pass/FAIL result is reliable; values are raw with
  an SAE unit/scaling id. Misfire rows are misfire counts per cylinder.
- readiness, ecu_info (calibration IDs, useful for TSB/reflash checks), supported_pids, notes
  from the app, and the raw adapter log.

Tools (all read-only; you can never clear codes or command anything on the vehicle):
- read_live_data samples sensors from the vehicle right now. Use it to confirm a diagnosis: O2
  switching, fuel trims at idle vs 2500 rpm, warm-up behaviour, charging voltage, MAF at idle.
  When the test needs the vehicle in a particular state (engine running and warm, holding
  2500 rpm), first ask the user to set it up and tell you when it's ready, then call the tool in
  your next turn. Request only the PIDs you need (1-6 for a time series); each PID slows the
  sample rate.
- run_scan re-runs the full scan, for example after the user drives, starts the engine or makes
  a repair.

How to answer:
- Ground every claim in the data and cite it (code, sensor value, test result, module status).
  Say plainly when the data can't answer something.
- Diagnose before parts: list the likely causes from most to least likely, how to confirm each
  (test, tool, expected value), then the fix. Mention well-known failure patterns for this
  make/model year when relevant, and how confident you are.
- Tie related evidence together (for example a lean code with positive fuel trims and a failing
  catalyst test), and note when a module code is the same fault as a generic code.
- Module results marked (experimental), or modules named only 'Module @ <address>', may be
  misidentified; treat them with caution and say so.
- Safety first: flag anything affecting brakes, steering, airbags, fuel leaks or overheating, and
  say whether the vehicle looks OK to drive. For airbag work, tell them to disconnect the battery
  and wait as the service manual specifies.
- Don't recommend clearing codes until the cause is understood; clearing also resets readiness
  monitors.
- Give temperatures, speeds and pressures in US units (F, mph, psi), with metric in parentheses
  when useful.
- Be concise and practical: short sections, numbered steps for procedures, part names and specs
  where useful. Ask for symptoms when they would change the answer."""

TOOLS = [
    {"name": "read_live_data",
     "description": (
         "Read live sensor data from the vehicle right now through the OBD adapter (SAE mode 01 "
         "PIDs, decoded, with min/max/mean and a time series). Read-only. Usually instant on an "
         "open connection, then it samples for the requested time. Each PID takes about 0.1-0.3 s "
         "per pass, so ask for 1-6 PIDs when you need a time series. supported_pids in the scan "
         "data lists what this vehicle supports. Common PIDs: 0C rpm, 0D speed, 05 coolant, "
         "04 load, 06/07 short/long fuel trim bank 1, 08/09 bank 2, 0B MAP, 10 MAF, 0E timing, "
         "0F intake air temp, 11 throttle, 14/15 O2 B1S1/B1S2 voltage, 18 O2 B2S1, 24/34 "
         "wide-band O2 B1S1, 42 module voltage, 44 commanded lambda, 2F fuel level."),
     "input_schema": {"type": "object", "properties": {
         "pids": {"type": "array", "items": {"type": "string"},
                  "description": "Mode 01 PIDs as two hex digits, e.g. [\"0C\",\"06\",\"07\",\"14\"]. "
                                 "Omit for a default set of key engine sensors."},
         "seconds": {"type": "number",
                     "description": "How long to sample, 0-30. 0 takes a single reading of each PID."}},
         "required": []}},
    {"name": "run_scan",
     "description": (
         "Re-run the full read-only scan with the app's current settings: codes, modules, freeze "
         "frame, live snapshot, monitor tests, readiness. Use after the user drives, starts the "
         "engine or makes a repair. Never clears codes. Takes 20 s to a few minutes; the app's "
         "results update."),
     "input_schema": {"type": "object", "properties": {}, "required": []}},
]


def ai_context(report):
    if not report:
        return ("No scan has been run yet. Answer general questions, and suggest running a scan "
                "(or use run_scan) for vehicle-specific advice.")
    r = dict(report)
    log = [e for e in r.get("raw_log", [])
           if not (e["tx"].upper().startswith("AT") and e["rx"] in ("OK", ""))
           and e["rx"] not in ("NO DATA", "?")]
    r["raw_log"] = log[:600]
    return "Scan data (JSON):\n" + json.dumps(r, separators=(",", ":"), default=str)


def ai_error(status, msg):
    return {
        401: "The API key was rejected. Check that it was pasted in full and hasn't been revoked.",
        403: f"This API key isn't allowed to use {AI_MODEL}. {msg}",
        404: f"Model {AI_MODEL} wasn't found for this account. {msg}",
        429: "Rate limit or spending limit reached on your Anthropic account. Wait a minute or "
             "check billing at console.anthropic.com.",
        529: "Anthropic's API is overloaded right now. Try again in a minute.",
    }.get(status, f"API error {status}: {msg}")


def strip_thinking(messages):
    """Remove reasoning blocks from finished earlier turns.

    Each thinking block is sealed to the exact system prompt it was made with. The scan data
    lives in the system prompt and changes whenever a new scan runs, so resending old
    thinking blocks would be rejected. The API allows dropping them from earlier turns."""
    out = []
    for m in messages:
        c = m.get("content")
        if m.get("role") == "assistant" and isinstance(c, list):
            c = [b for b in c if b.get("type") not in ("thinking", "redacted_thinking")]
            if not c:
                c = [{"type": "text", "text": "(no text)"}]
            m = {**m, "content": c}
        out.append(m)
    return out


def drop_none(x):
    if isinstance(x, dict):
        return {k: drop_none(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [drop_none(v) for v in x]
    return x


def claude_request(key, system_text, messages):
    body = {"model": AI_MODEL, "max_tokens": AI_MAX_TOKENS, "tools": TOOLS,
            "system": [{"type": "text", "text": system_text,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": messages}
    req = urllib.request.Request(
        AI_URL, data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError(ai_error(e.code, msg))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach the Anthropic API ({e.reason}). "
                           "Check the internet connection.")


def usage_cost(u):
    tin = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) * 1.25 +
           u.get("cache_read_input_tokens", 0) * 0.05)
    return tin * PRICE_IN + u.get("output_tokens", 0) * PRICE_OUT


def answer_text(content, stop_reason):
    text = "\n\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
    if stop_reason == "max_tokens":
        text += "\n\n*(Reply cut off at the length limit. Ask me to continue.)*"
    return text or "*(No answer text came back. Try asking again.)*"


# ---- application state -------------------------------------------------------

class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.status = Status()
        self.session = AdapterSession()
        self.report = None
        self.last_settings = None
        self.chat_hist = []
        self.chat_lock = threading.Lock()
        cfg = load_config()
        self.api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        self.key_source = ("saved" if cfg.get("api_key") else
                           "environment" if self.api_key else "")
        self.update = None
        self.update_lock = threading.Lock()

    # -- config / key
    def config(self):
        cfg = load_config()
        return {"has_key": bool(self.api_key), "key_source": self.key_source,
                "key_ok": self.api_key.startswith("sk-ant-"),
                "remembered": bool(cfg.get("api_key")), "guide_seen": bool(cfg.get("guide_seen")),
                "model": AI_MODEL, "config_dir": config_dir(), "version": APP_VERSION,
                "update": self.update}

    # -- updates
    def check_update(self):
        """Ask GitHub for the latest release; remember it if it's newer than this copy."""
        current = parse_version(APP_VERSION)
        if not current:          # development build: nothing to compare against
            return None
        try:
            req = urllib.request.Request(UPDATE_API, headers={
                "User-Agent": f"OnlyScans/{APP_VERSION}", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                rel = json.loads(r.read())
        except Exception:
            return None          # offline or GitHub unreachable: try again next launch
        latest = parse_version(rel.get("tag_name"))
        asset = next((a for a in rel.get("assets", []) if a.get("name") == "OnlyScans.exe"), None)
        if latest and latest > current and asset and not rel.get("draft") and not rel.get("prerelease"):
            self.update = {"version": ".".join(map(str, latest)),
                           "url": asset.get("browser_download_url"), "size": asset.get("size"),
                           "digest": asset.get("digest") or "", "page": rel.get("html_url"),
                           "notes": (rel.get("body") or "")[:6000],
                           "can_install": bool(os.environ.get("ONLYSCANS_EXE_PATH"))}
        return self.update

    def install_update(self):
        """Download the new OnlyScans.exe, check it, swap it in, then restart via the launcher."""
        u, exe = self.update, os.environ.get("ONLYSCANS_EXE_PATH")
        if not u:
            raise RuntimeError("No update is available.")
        if not exe:
            raise RuntimeError("Self-update only works in OnlyScans.exe. Download the new version "
                               f"from {u['page']}")
        if not self.update_lock.acquire(blocking=False):
            raise RuntimeError("An update is already being installed.")
        new, old = exe + ".new", exe + ".old"
        try:
            with self.status.task(f"Downloading OnlyScans {u['version']} ..."):
                req = urllib.request.Request(u["url"], headers={"User-Agent": f"OnlyScans/{APP_VERSION}"})
                h = hashlib.sha256()
                with urllib.request.urlopen(req, timeout=120) as r, open(new, "wb") as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        h.update(chunk)
                        f.write(chunk)
                size = os.path.getsize(new)
                with open(new, "rb") as f:
                    is_exe = f.read(2) == b"MZ" or os.name != "nt"   # Windows programs start "MZ"
                if not is_exe or (u.get("size") and size != u["size"]):
                    raise RuntimeError("The download was incomplete or isn't a Windows program. "
                                       "Nothing was changed; try again later.")
                if u["digest"].startswith("sha256:") and h.hexdigest() != u["digest"][7:].lower():
                    raise RuntimeError("The download didn't match GitHub's checksum. Nothing was "
                                       "changed; try again later.")
                self.status.say("Installing the update ...")
                try:
                    os.chmod(new, os.stat(exe).st_mode)   # keep it runnable (matters off Windows)
                except OSError:
                    pass
                try:
                    if os.path.exists(old):
                        os.remove(old)
                    os.replace(exe, old)          # Windows allows renaming a running program
                    try:
                        os.replace(new, exe)
                    except OSError:
                        os.replace(old, exe)      # put the original back
                        raise
                except OSError:
                    raise RuntimeError(
                        f"OnlyScans can't replace itself in {os.path.dirname(exe)}. Move "
                        "OnlyScans.exe to a folder you own, like Documents or the Desktop, and try "
                        f"again, or download the new version from {u['page']}")
        except Exception:
            try:
                os.remove(new)
            except OSError:
                pass
            self.update_lock.release()
            raise
        def restart():
            self.session.close()
            os._exit(UPDATE_EXIT_CODE)
        threading.Timer(1.0, restart).start()   # let this reply reach the browser first
        return {"version": u["version"]}

    def set_key(self, key, remember):
        key = (key or "").strip()
        if key:
            self.api_key, self.key_source = key, "entered"
        cfg = load_config()
        if remember and self.api_key:
            cfg["api_key"] = self.api_key
            self.key_source = "saved"
        elif not remember:
            cfg.pop("api_key", None)
            if self.key_source == "saved":
                self.key_source = "entered"
        save_config(cfg)
        return self.config()

    def guide_seen(self):
        cfg = load_config()
        cfg["guide_seen"] = True
        save_config(cfg)
        return {"ok": True}

    # -- adapter actions
    def ports(self):
        out = []
        for d in j2534_devices():
            ok = python_bits() in d["libs"]
            out.append({"device": J2534_PREFIX + d["name"], "obd": ok,
                        "description": f"J2534 pass-thru ({d['vendor'] or d['name']})" + (
                            "" if ok else f" - driver is {'/'.join(f'{b}-bit' for b in d['libs'])}"
                                          f" only, can't load in this {python_bits()}-bit app")})
        return out + [{"device": p.device, "description": p.description or "",
                       "obd": is_obd_adapter(p)} for p in find_ports()]

    def probe(self, port):
        if not port:
            raise RuntimeError("Pick an adapter port first.")
        with self.status.task("Checking the adapter ..."), self.session.lock:
            self.session.close()
            io = open_adapter(port)
            try:
                return {"port": port, "baud": io.baud, "id": " ".join(io.raw("ATI")),
                        "detail": " ".join(io.raw("AT@1")),
                        "voltage": " ".join(io.raw("ATRV"))}
            finally:
                io.close()

    def scan(self, s):
        port = s.get("port") or ""
        if not port:
            raise RuntimeError("Pick an adapter port first (step 1).")
        args = types.SimpleNamespace(
            make=s.get("make") or "auto", sweep=bool(s.get("sweep")),
            no_modules=not s.get("modules", True), clear=bool(s.get("clear")), yes=True)
        label = "Reading, then clearing codes ..." if args.clear else "Reading codes and vehicle data ..."
        with self.status.task(label), self.session.lock:
            try:
                sc = self.session.scanner(port, self.status.say, fresh=True)
                report = run_scan(sc.io, args, self.status.say, sc=sc)
            except RuntimeError:
                self.session.close()
                raise
            except Exception as e:
                self.session.close()
                raise RuntimeError(f"Lost contact with the adapter: {e}")
        self.report = report
        self.last_settings = {k: v for k, v in s.items() if k != "clear"}
        return report

    def live(self, port, pids=None, seconds=0.0):
        if not port:
            raise RuntimeError("Pick an adapter port first (step 1).")
        with self.status.task("Reading live data ..."), self.session.lock:
            for attempt in (0, 1):
                try:
                    sc = self.session.scanner(port, self.status.say, fresh=attempt == 1)
                    if seconds:
                        self.status.say(f"Reading live data for {seconds:g} s - keep the vehicle steady ...")
                    res = sc.live(pids, seconds)
                except RuntimeError:
                    self.session.close()
                    raise
                except Exception as e:
                    self.session.close()
                    if attempt:
                        raise RuntimeError(f"Lost contact with the adapter: {e}")
                    continue
                if res["pids"] or attempt:
                    return {"adapter": sc.adapter_id,
                            "protocol": PROTOCOLS.get(sc.protocol, sc.protocol), "live": res}

    # -- AI
    def exec_tool(self, name, inp, settings):
        port = (settings or {}).get("port") or ""
        if not port:
            return {"content": "No adapter port is selected in the app. Ask the user to pick it in step 1.",
                    "is_error": True, "label": "Couldn't reach the vehicle: no port selected"}
        try:
            if name == "read_live_data":
                pids = [str(p).upper() for p in (inp.get("pids") or [])
                        if re.fullmatch(r"[0-9A-Fa-f]{2}", str(p))]
                secs = max(0.0, min(float(inp.get("seconds") or 0), 30.0))
                label = "Read live data{}: {}".format(f" for {secs:g} s" if secs else "",
                                                      ", ".join(pids) or "default sensors")
                res = self.live(port, [int(p, 16) for p in pids] or None, secs)
                return {"content": json.dumps(res, default=str), "is_error": False, "label": label}
            if name == "run_scan":
                s = dict(self.last_settings or settings)
                s.update(port=port, clear=False)
                rep = self.scan(s)
                return {"content": ai_context(rep), "is_error": False, "rescanned": True,
                        "label": "Re-scanned the vehicle (results updated)"}
        except Exception as e:
            return {"content": f"Error: {e}", "is_error": True, "label": f"{name} failed: {e}"}
        return {"content": f"Unknown tool: {name}", "is_error": True, "label": f"Unknown tool {name}"}

    def chat(self, question, settings):
        question = (question or "").strip()
        if not question:
            raise RuntimeError("Type a question first.")
        if not self.api_key:
            raise RuntimeError("Paste your Anthropic API key in the box at the top right first.")
        with self.chat_lock, self.status.task("AI mechanic is thinking ..."):
            msgs = strip_thinking(self.chat_hist) + [{"role": "user", "content": question}]
            # One system prompt for the whole question: a re-scan mid-answer reaches the AI
            # through the tool result, and the prompt must not change while thinking blocks
            # from this question are still being sent back.
            system_text = AI_SYSTEM + "\n\n" + ai_context(self.report)
            steps, cost, rescanned = [], 0.0, False
            try:
                for _ in range(AI_STEPS):
                    self.status.say("AI mechanic is thinking ...")
                    out = claude_request(self.api_key, system_text, msgs)
                    cost += usage_cost(out.get("usage", {}))
                    content = drop_none(out.get("content", []))
                    msgs.append({"role": "assistant", "content": content})
                    if out.get("stop_reason") != "tool_use":
                        self.chat_hist = msgs
                        return {"text": answer_text(content, out.get("stop_reason")),
                                "steps": steps, "cost": cost,
                                "report": self.report if rescanned else None}
                    results = []
                    for b in content:
                        if b.get("type") != "tool_use":
                            continue
                        r = self.exec_tool(b.get("name"), b.get("input") or {}, settings)
                        steps.append(r["label"])
                        rescanned = rescanned or r.get("rescanned", False)
                        results.append({"type": "tool_result", "tool_use_id": b["id"],
                                        "content": r["content"], "is_error": r["is_error"]})
                    msgs.append({"role": "user", "content": results})
                raise RuntimeError(f"Stopped after {AI_STEPS} steps without a final answer. "
                                   "Try a narrower question.")
            except RuntimeError as e:
                return {"error": str(e), "steps": steps,
                        "report": self.report if rescanned else None}

    def chat_reset(self):
        with self.chat_lock:
            self.chat_hist = []
        return {"ok": True}


# ---- local web server ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "OnlyScans"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            data = json.dumps(body, default=str).encode()
        else:
            data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):  # blocks DNS-rebinding tricks from other websites
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _authorized(self):  # every API call must carry the per-launch token from the page
        return self._host_ok() and secrets.compare_digest(
            self.headers.get("X-OnlyScans-Token", ""), self.app.token)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            return self._send(200, page_html(self.app.token), "text/html")
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        routes = {"/api/ports": self.app.ports, "/api/status": self.app.status.get,
                  "/api/config": self.app.config, "/api/report": lambda: self.app.report,
                  "/api/update": lambda: self.app.update}
        fn = routes.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            self._send(200, {"data": fn()})
        except Exception as e:
            self._send(200, {"error": str(e)})

    def do_POST(self):
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad request"})
        a = self.app
        routes = {
            "/api/probe": lambda: a.probe(body.get("port")),
            "/api/scan": lambda: a.scan(body.get("settings") or {}),
            "/api/live": lambda: a.live((body.get("port") or ""), None, 0),
            "/api/key": lambda: a.set_key(body.get("key"), bool(body.get("remember"))),
            "/api/guide_seen": a.guide_seen,
            "/api/chat": lambda: a.chat(body.get("question"), body.get("settings") or {}),
            "/api/chat/reset": a.chat_reset,
            "/api/update/install": a.install_update,
        }
        fn = routes.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            self._send(200, {"data": fn()})
        except RuntimeError as e:
            self._send(200, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(200, {"error": f"Unexpected error: {e}"})


def page_html(token):
    guide = json.dumps(GUIDE_MD).replace("</", "<\\/")
    return (PAGE.replace("__TOKEN__", token).replace("__GUIDE__", guide)
            .replace("__MODEL__", AI_MODEL))


def serve(http_port=APP_PORT, open_browser=True):
    app = App()
    Handler.app = app
    threading.Thread(target=app.check_update, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", http_port), Handler)
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port busy: pick any free one
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print("=" * 60)
    print(f" OnlyScans {APP_VERSION} is running at {url}")
    print(" Your browser should open on its own. If it doesn't, open that address.")
    print(" Keep this window open while you use OnlyScans; close it to quit.")
    print("=" * 60)
    if open_browser:
        threading.Timer(0.8, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.session.close()


def build_exe():
    """Package this file into a single OnlyScans.exe with PyInstaller (run on Windows)."""
    here = os.path.dirname(os.path.abspath(__file__))
    print("Installing PyInstaller and pyserial ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "--quiet",
                           "pyinstaller", "pyserial"])
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--onefile", "--console",
                           "--name", "OnlyScans", "--hidden-import", "serial.tools.list_ports",
                           "--distpath", here, "--workpath", os.path.join(here, "build"),
                           "--specpath", os.path.join(here, "build"), os.path.abspath(__file__)])
    print(f"\nDone: {os.path.join(here, 'OnlyScans.exe' if os.name == 'nt' else 'OnlyScans')}")
    print("You can delete the 'build' folder. Copy OnlyScans.exe anywhere and double-click it.")


# ---- command line ----------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="OnlyScans. Run with no options to open the app in your browser.")
    ap.add_argument("--http-port", type=int, default=APP_PORT, help="app: local web port")
    ap.add_argument("--no-browser", action="store_true", help="app: don't open the browser")
    ap.add_argument("--build-exe", action="store_true", help="package into OnlyScans.exe")
    ap.add_argument("--version", action="version", version=f"OnlyScans {APP_VERSION}")
    ap.add_argument("--port", help="command-line scan: serial port (COM4, /dev/ttyUSB0) or 'auto'")
    ap.add_argument("--baud", type=int, help="adapter baud (auto-detected if omitted)")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--probe", action="store_true",
                    help="check the adapter only: ID, firmware, voltage at the OBD port")
    ap.add_argument("--make", default="auto", choices=["auto"] + list(MAKES),
                    help="vehicle make (default: from the VIN)")
    ap.add_argument("--sweep", action="store_true", help="probe every diagnostic address")
    ap.add_argument("--no-modules", action="store_true", help="generic OBD-II only")
    ap.add_argument("--clear", action="store_true", help="clear codes after reading")
    ap.add_argument("--yes", action="store_true", help="skip the clear confirmation prompt")
    ap.add_argument("--json", help="write full report as JSON")
    ap.add_argument("--csv", help="write codes as CSV")
    ap.add_argument("--quiet", action="store_true", help="no console report")
    ap.add_argument("--live", action="store_true",
                    help="sample live sensor data instead of scanning codes")
    ap.add_argument("--pids", help="live: comma-separated mode 01 PIDs in hex, e.g. 0C,0D,05")
    ap.add_argument("--seconds", type=float, default=0, help="live: sampling time (max 60)")
    args = ap.parse_args()

    if args.build_exe:
        return build_exe()
    ensure_pyserial()
    if args.list_ports:
        return list_ports()
    if not args.port:  # no command-line job: run the app
        return serve(args.http_port, not args.no_browser)

    port = auto_port() if args.port == "auto" else args.port
    print(f"Using port {port}", file=sys.stderr)
    io = make_adapter(port, args.baud)
    say = (lambda *_: None) if args.quiet else (lambda s: print(s, file=sys.stderr))

    if args.probe:
        info = {"port": io.port, "baud": io.baud, "id": " ".join(io.raw("ATI")),
                "detail": " ".join(io.raw("AT@1")), "voltage": " ".join(io.raw("ATRV"))}
        io.close()
        if args.json:
            with open(args.json, "w") as f:
                json.dump({"probe": info}, f, indent=2)
        for k, v in info.items():
            print(f"{k:<8}{v}")
        return

    if args.live:
        try:
            sc = Scanner(io, say)
            out = {"adapter": sc.connect()}
            out["protocol"] = PROTOCOLS.get(sc.protocol, sc.protocol)
            pids = [int(x, 16) for x in args.pids.split(",") if x.strip()] if args.pids else None
            out["live"] = sc.live(pids, max(0.0, min(args.seconds, 60.0)))
        except RuntimeError as e:
            out = {"error": str(e)}
        finally:
            io.close()
        if args.json:
            with open(args.json, "w") as f:
                json.dump(out, f, indent=2, default=str)
        if not args.quiet:
            for row in out.get("live", {}).get("pids", []):
                rng = f"  (min {row['min']}, max {row['max']})" if "min" in row else ""
                print(f"{row['name']:<48}{row['display']}{rng}")
            if "error" in out:
                print("ERROR:", out["error"])
        return

    try:
        report = run_scan(io, args, say)
    finally:
        io.close()
    if not args.quiet:
        print_report(report)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
    if args.csv:
        write_csv(report, args.csv)


# ---- the interface (served to your browser; no internet needed except for the AI) ----

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OnlyScans</title>
<style>
:root { --bg:#f4f6f8; --panel:#fff; --ink:#1f2933; --muted:#6b7785; --line:#dde3e9;
        --brand:#1f3a5f; --brand2:#2c5d8f; --ok:#1e7d4f; --warn:#b26b00; --bad:#b02a37;
        --soft:#eef2f6; font-size:15px; }
* { box-sizing:border-box; }
body { margin:0; font-family:"Segoe UI",system-ui,-apple-system,Roboto,sans-serif; color:var(--ink); background:var(--bg); }
button, input, select, textarea { font:inherit; }
button { cursor:pointer; border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:6px; padding:.4rem .75rem; }
button:hover:not(:disabled) { border-color:var(--brand2); }
button:disabled { opacity:.5; cursor:default; }
button.primary { background:var(--brand); border-color:var(--brand); color:#fff; font-weight:600; }
button.primary:hover:not(:disabled) { background:var(--brand2); }
button.danger { color:var(--bad); border-color:#e9b8bd; }
button.small { font-size:.85rem; padding:.3rem .6rem; }
select, input[type=password], input[type=text], textarea { width:100%; border:1px solid var(--line); border-radius:6px; padding:.45rem .55rem; background:#fff; }
label.check { display:flex; gap:.45rem; align-items:flex-start; margin:.35rem 0; font-size:.93rem; }
header { display:flex; align-items:center; gap:.8rem; padding:.55rem 1rem; background:var(--panel); border-bottom:1px solid var(--line); height:56px; }
header h1 { font-size:1.2rem; margin:0 .4rem 0 0; }
header .key { margin-left:auto; display:flex; align-items:center; gap:.5rem; font-size:.9rem; }
header .key input[type=password] { width:260px; }
.badge { font-size:.75rem; padding:.15rem .5rem; border-radius:10px; color:#fff; white-space:nowrap; }
.b-none { background:#8a96a3; } .b-ok { background:var(--ok); } .b-warn { background:var(--warn); }
.ver { font-size:.75rem; color:var(--muted); }
#upd { display:none; background:#e8f3ec; border-bottom:1px solid #b9dcc6; padding:.45rem 1rem; font-size:.9rem; align-items:center; gap:.7rem; }
#upd.on { display:flex; }
#upd b { color:var(--ok); }
#upd .sp { margin-left:auto; display:flex; gap:.4rem; }
#busy { position:fixed; top:56px; left:0; right:0; background:var(--brand); color:#fff; padding:.35rem 1rem; font-size:.9rem; z-index:5; display:none; }
#busy.on { display:block; }
.spin { display:inline-block; width:.85em; height:.85em; border:2px solid currentColor; border-right-color:transparent; border-radius:50%; animation:sp .8s linear infinite; vertical-align:-1px; margin-right:.45rem; }
@keyframes sp { to { transform:rotate(360deg); } }
main { display:grid; grid-template-columns:280px minmax(0,1fr) 430px; height:calc(100vh - 56px); }
aside.left { background:var(--panel); border-right:1px solid var(--line); padding:1rem; overflow-y:auto; }
aside.left h3 { font-size:.95rem; margin:1.1rem 0 .5rem; } aside.left h3:first-child { margin-top:0; }
aside.left .field { margin:.45rem 0; } aside.left .field span { display:block; font-size:.85rem; color:var(--muted); margin-bottom:.2rem; }
.row { display:flex; gap:.4rem; flex-wrap:wrap; }
#scanBtn { width:100%; margin-top:.5rem; padding:.55rem; }
section.center { padding:1rem; overflow-y:auto; }
.boxes { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.75rem; }
.box { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:.8rem .9rem; }
.box .t { font-size:.82rem; color:var(--muted); }
.box .v { font-size:1.35rem; font-weight:600; margin-top:.2rem; overflow-wrap:anywhere; }
.box .s { font-size:.78rem; color:var(--muted); margin-top:.15rem; }
.v.on { color:var(--bad); } .v.off { color:var(--ok); }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; margin-top:.9rem; }
.tabs { display:flex; flex-wrap:wrap; border-bottom:1px solid var(--line); padding:0 .4rem; }
.tabs button { border:none; border-bottom:2px solid transparent; border-radius:0; background:none; padding:.6rem .7rem; color:var(--muted); }
.tabs button.on { color:var(--brand); border-bottom-color:var(--brand); font-weight:600; }
.tabs .n { font-size:.72rem; background:var(--soft); border-radius:8px; padding:0 .35rem; margin-left:.25rem; }
.tabs .n.bad { background:#f8d7da; color:var(--bad); }
.pane { padding:.8rem; display:none; } .pane.on { display:block; }
.pane .tools { margin-bottom:.6rem; display:flex; gap:.5rem; align-items:center; font-size:.85rem; color:var(--muted); }
table.grid { width:100%; border-collapse:collapse; font-size:.88rem; }
table.grid th { text-align:left; background:var(--soft); position:sticky; top:0; }
table.grid th, table.grid td { padding:.38rem .5rem; border-bottom:1px solid var(--line); vertical-align:top; }
td.fail { color:var(--bad); font-weight:700; } td.pass { color:var(--ok); }
.empty { color:var(--muted); padding:1.2rem .4rem; }
aside.chat { background:var(--panel); border-left:1px solid var(--line); display:flex; flex-direction:column; padding:.9rem; min-height:0; }
aside.chat h2 { font-size:1.05rem; margin:0 0 .5rem; }
.quick { display:flex; flex-wrap:wrap; gap:.35rem; margin-bottom:.6rem; }
.quick button { font-size:.82rem; padding:.28rem .55rem; color:var(--brand); border-color:#b9c8d8; }
#log { flex:1; overflow-y:auto; min-height:0; padding-right:.2rem; }
.msg { border-radius:9px; padding:.55rem .75rem; margin-bottom:.6rem; font-size:.92rem; line-height:1.45; }
.msg.user { background:var(--brand); color:#fff; margin-left:2.2rem; white-space:pre-wrap; }
.msg.ai { background:var(--soft); border:1px solid var(--line); margin-right:.4rem; }
.msg.err { background:#fdecea; border:1px solid #f5c2c7; color:#842029; }
.msg.ai h1,.msg.ai h2,.msg.ai h3,.msg.ai h4 { font-size:1rem; margin:.6rem 0 .3rem; }
.msg p { margin:.35rem 0; } .msg ul,.msg ol { margin:.3rem 0; padding-left:1.3rem; }
.msg table { border-collapse:collapse; font-size:.84rem; margin:.4rem 0; }
.msg td,.msg th { border:1px solid var(--line); padding:.2rem .4rem; }
.msg pre { background:#fff; border:1px solid var(--line); border-radius:6px; padding:.5rem; overflow-x:auto; }
code { background:#e7ecf1; border-radius:4px; padding:0 .25rem; font-size:.88em; }
pre code { background:none; padding:0; }
.steps { font-size:.76rem; color:var(--muted); margin-bottom:.35rem; }
.meta { font-size:.72rem; color:var(--muted); margin-top:.4rem; }
.note { text-align:center; color:var(--muted); font-size:.8rem; margin:.4rem 0 .8rem; }
#q { resize:vertical; margin-top:.5rem; }
.send { display:flex; gap:.4rem; margin-top:.45rem; }
#modal { position:fixed; inset:0; background:rgba(20,30,40,.45); display:none; align-items:flex-start; justify-content:center; z-index:10; padding:3vh 1rem; }
#modal.on { display:flex; }
#modal .sheet { background:#fff; border-radius:10px; max-width:980px; width:100%; max-height:94vh; overflow-y:auto; padding:1.3rem 1.6rem; position:relative; }
#modal .x { position:sticky; float:right; top:0; font-size:1.3rem; line-height:1; padding:.2rem .55rem; }
.guide h1 { font-size:1.5rem; margin-top:0; } .guide h2 { font-size:1.2rem; margin-top:1.5rem; border-bottom:1px solid var(--line); padding-bottom:.25rem; }
.guide h3 { font-size:1.02rem; margin-top:1rem; } .guide li { margin:.2rem 0; }
.guide table { border-collapse:collapse; width:100%; font-size:.9rem; margin:.5rem 0 1rem; }
.guide th, .guide td { border:1px solid var(--line); padding:.35rem .55rem; text-align:left; vertical-align:top; }
.guide th { background:var(--soft); }
@media (max-width:1150px) { main { grid-template-columns:250px minmax(0,1fr); } aside.chat { grid-column:1 / -1; height:70vh; } main { height:auto; } }
</style>
</head>
<body>
<header>
  <h1>OnlyScans</h1>
  <span class="ver" id="ver"></span>
  <button class="small" id="guideBtn">&#128214; Guide</button>
  <div class="key">
    <input type="password" id="key" placeholder="Anthropic API key (sk-ant-...)" autocomplete="off">
    <label class="check" style="margin:0"><input type="checkbox" id="remember"> Remember</label>
    <span id="keyBadge" class="badge b-none">No key</span>
  </div>
</header>
<div id="upd"><span>&#11014;&#65039; <b id="updVer"></b> is available.</span>
  <a href="#" id="updNotes">What's new</a>
  <span class="sp"><button class="small" id="updLater">Later</button><button class="small primary" id="updBtn">Update now</button></span></div>
<div id="busy"><span class="spin"></span><span id="busyMsg">Working ...</span></div>
<main>
  <aside class="left">
    <h3>1. Connect</h3>
    <div class="field"><span>Adapter port</span><select id="port"></select></div>
    <div class="row"><button class="small act" id="refreshBtn">Refresh ports</button>
      <button class="small act" id="probeBtn">Test adapter</button></div>
    <h3>2. Scan</h3>
    <div class="field"><span>Vehicle make</span>
      <select id="make">
        <option value="auto">Auto (from VIN)</option>
        <option value="toyota">Toyota / Lexus / Scion</option>
        <option value="honda">Honda / Acura</option>
        <option value="gm">GM: Chevrolet, GMC, Buick, Cadillac, Pontiac, Saturn</option>
        <option value="chrysler">Chrysler / Dodge / Jeep / Ram</option>
        <option value="ford">Ford / Lincoln / Mercury</option>
        <option value="other">Other make</option>
      </select></div>
    <label class="check"><input type="checkbox" id="modules" checked> Module scan (ABS, airbag, body, trans...)</label>
    <label class="check"><input type="checkbox" id="sweep"> Sweep every module address (adds 1-3 min)</label>
    <button class="primary act" id="scanBtn">Read all codes</button>
    <h3>3. Save / clear</h3>
    <div class="row"><button class="small" id="csvBtn">Codes (CSV)</button>
      <button class="small" id="jsonBtn">Full report (JSON)</button></div>
    <div style="margin-top:.5rem"><button class="small danger act" id="clearBtn">Clear codes...</button></div>
  </aside>

  <section class="center">
    <div class="boxes">
      <div class="box"><div class="t">Check engine light</div><div class="v" id="bMil">-</div></div>
      <div class="box"><div class="t">Codes found</div><div class="v" id="bCodes">-</div></div>
      <div class="box"><div class="t">Vehicle</div><div class="v" id="bVeh">-</div><div class="s" id="bProto"></div></div>
      <div class="box"><div class="t">VIN</div><div class="v" id="bVin" style="font-size:.95rem;letter-spacing:.02em">-</div></div>
    </div>
    <div class="card">
      <div class="tabs" id="tabs"></div>
      <div id="panes"></div>
    </div>
  </section>

  <aside class="chat">
    <h2>AI mechanic</h2>
    <div class="quick" id="quick"></div>
    <div id="log"></div>
    <textarea id="q" rows="3" placeholder="Ask about this vehicle, or describe symptoms..."></textarea>
    <div class="send"><button class="primary" id="sendBtn">Send</button>
      <button class="small" id="resetBtn">New chat</button></div>
  </aside>
</main>
<div id="modal"><div class="sheet"><button class="x" id="modalX" title="Close">&times;</button><div id="modalBody"></div></div></div>

<script>
const TOKEN = "__TOKEN__", GUIDE = __GUIDE__, MODEL = "__MODEL__";
const $ = id => document.getElementById(id);
let report = null, chat = [], waiting = false, inflight = 0, pollTimer = null;

// ---------- helpers ----------
function esc(s) { return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function inline(s) {  // s is already escaped
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
       .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<em>$2</em>")
       .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => "<code>" + codes[i] + "</code>");
}
function md(src) {  // small, safe markdown renderer: escapes everything first
  const L = esc(src).replace(/\r/g, "").split("\n"), out = [];
  const isList = l => /^\s*([-*]|\d+\.)\s+/.test(l);
  const isBlock = l => /^(#{1,4}\s|```|\s*\|)/.test(l) || isList(l);
  let i = 0;
  while (i < L.length) {
    const l = L[i];
    if (/^```/.test(l)) { const b = []; i++; while (i < L.length && !/^```/.test(L[i])) b.push(L[i++]); i++;
      out.push("<pre><code>" + b.join("\n") + "</code></pre>"); continue; }
    const h = l.match(/^(#{1,4})\s+(.*)/);
    if (h) { const n = h[1].length; out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
    if (/^\s*\|/.test(l)) {
      const rows = []; while (i < L.length && /^\s*\|/.test(L[i])) rows.push(L[i++]);
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      let t = "<table>";
      rows.forEach((r, k) => { if (k === 1 && /^[\s|:\-]+$/.test(r)) return;
        const tag = k === 0 ? "th" : "td"; t += "<tr>" + cells(r).map(c => `<${tag}>${inline(c)}</${tag}>`).join("") + "</tr>"; });
      out.push(t + "</table>"); continue;
    }
    if (isList(l)) {
      const st = []; let t = "";
      while (i < L.length && (isList(L[i]) || (st.length && /^\s{2,}\S/.test(L[i])))) {
        const m = L[i].match(/^(\s*)([-*]|\d+\.)\s+(.*)/);
        if (!m) { t += " " + inline(L[i].trim()); i++; continue; }
        const ind = m[1].length, type = /\d/.test(m[2]) ? "ol" : "ul";
        if (!st.length || ind > st[st.length - 1].ind) { t += `<${type}>`; st.push({ ind, type }); }
        else { while (st.length > 1 && ind < st[st.length - 1].ind) t += `</li></${st.pop().type}>`; t += "</li>"; }
        t += "<li>" + inline(m[3]); i++;
      }
      while (st.length) t += `</li></${st.pop().type}>`;
      out.push(t); continue;
    }
    if (!l.trim()) { i++; continue; }
    const p = []; while (i < L.length && L[i].trim() && !isBlock(L[i])) p.push(L[i++].trim());
    out.push("<p>" + inline(p.join(" ")) + "</p>");
  }
  return out.join("\n");
}
async function api(path, body) {
  const opt = { headers: { "X-OnlyScans-Token": TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ error: "Bad response from OnlyScans (" + r.status + ")" }));
  if (j.error) throw new Error(j.error);
  return j.data;
}
async function busy(fn) {  // run a long action with the progress bar and buttons locked
  inflight++; setBusy();
  try { return await fn(); }
  catch (e) { alert(e.message); }
  finally { inflight--; setBusy(); }
}
function setBusy() {
  document.querySelectorAll(".act").forEach(b => b.disabled = inflight > 0);
  if (inflight > 0 && !pollTimer) {
    $("busy").classList.add("on");
    pollTimer = setInterval(async () => { try { const s = await api("/api/status"); if (s.message) $("busyMsg").textContent = s.message; } catch (e) {} }, 600);
  } else if (inflight === 0 && pollTimer) {
    clearInterval(pollTimer); pollTimer = null; $("busy").classList.remove("on"); $("busyMsg").textContent = "Working ...";
  }
}
function settings(extra) {
  return Object.assign({ port: $("port").value, make: $("make").value,
                         modules: $("modules").checked, sweep: $("sweep").checked }, extra || {});
}
function openModal(html) { $("modalBody").innerHTML = html; $("modal").classList.add("on"); $("modal").querySelector(".sheet").scrollTop = 0; }
function closeModal() { $("modal").classList.remove("on"); }
function download(name, text, type) {
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function stamp() { const d = new Date(), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`; }

// ---------- results ----------
const EMPTY = "Run a scan to see results. New here? Click Guide at the top.";
const TABS = [
  ["codes", "All codes"], ["modules", "Modules"], ["live", "Live data"], ["ff", "Freeze frame"],
  ["mode06", "Monitor tests"], ["ready", "Readiness"], ["ecu", "ECU info"], ["notes", "Notes"], ["raw", "Raw log"]];
function table(cols, rows, empty, cellClass) {
  if (!rows || !rows.length) return `<div class="empty">${esc(empty)}</div>`;
  return "<table class='grid'><thead><tr>" + cols.map(c => `<th>${esc(c[1])}</th>`).join("") + "</tr></thead><tbody>" +
    rows.map(r => "<tr>" + cols.map(c => { const v = typeof c[0] === "function" ? c[0](r) : r[c[0]];
      const cls = cellClass ? cellClass(c[0], v) : ""; return `<td${cls ? ` class="${cls}"` : ""}>${esc(v)}</td>`; }).join("") + "</tr>").join("") +
    "</tbody></table>";
}
function buildTabs() {
  $("tabs").innerHTML = TABS.map(([k, n], i) => `<button data-k="${k}" class="${i ? "" : "on"}">${n}<span class="n" id="n_${k}" hidden></span></button>`).join("");
  $("panes").innerHTML = TABS.map(([k], i) => `<div class="pane ${i ? "" : "on"}" id="p_${k}"></div>`).join("");
  $("tabs").onclick = e => { const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("on", x === b));
    document.querySelectorAll(".pane").forEach(p => p.classList.toggle("on", p.id === "p_" + b.dataset.k)); };
}
function count(k, n, bad) { const el = $("n_" + k); if (!el) return; el.hidden = !n; el.textContent = n; el.classList.toggle("bad", !!bad); }
function vehicleText(r) {
  const v = r.vehicle || {}; const short = { toyota: "Toyota", honda: "Honda", gm: "GM", chrysler: "Chrysler/Jeep", ford: "Ford" };
  if (!v.make || v.make === "other") return v.make_source === "selected" ? "Other make" : "Make unknown";
  return (short[v.make] + " " + (v.year || "")).trim();
}
function render() {
  const r = report;
  if (!r) {
    TABS.forEach(([k]) => { $("p_" + k).innerHTML = `<div class="empty">${EMPTY}</div>`; count(k, 0); });
    return;
  }
  const mil = r.mil || [];
  const on = mil.some(m => m.mil_on);
  $("bMil").textContent = !mil.length ? "unknown" : on ? "ON - " + mil.reduce((a, m) => a + m.count, 0) + " code(s)" : "off";
  $("bMil").className = "v " + (on ? "on" : mil.length ? "off" : "");
  $("bCodes").textContent = (r.codes || []).length;
  $("bVeh").textContent = vehicleText(r);
  $("bProto").textContent = r.protocol || "no connection";
  $("bVin").textContent = r.vin || "not reported";

  $("p_codes").innerHTML = table([["code", "Code"], ["type", "Type"], ["module", "Module"], ["description", "Description"], ["status", "Status"], ["address", "Address"]],
    r.codes, "No codes found.");
  count("codes", (r.codes || []).length);
  $("p_modules").innerHTML = table([["address", "Address"], ["module", "Module"], ["status", "Status"], [m => m.method || "", "Method"], [m => (m.codes || []).length, "Codes"]],
    r.modules, "No module scan for this vehicle - see Notes.");
  $("p_live").innerHTML = `<div class="tools"><button class="small act" id="liveBtn">Read live now</button>
      <span>${r.live_taken ? "Read at " + esc(r.live_taken) : "Taken during the scan"}. Start the engine for meaningful readings.</span></div>` +
    table([["name", "Sensor"], ["display", "Value"], ["pid", "PID"], ["ecu", "ECU"]], r.live_snapshot,
      "No live data reported. Older vehicles may support only a few sensors.");
  $("liveBtn").onclick = readLive;
  $("p_ff").innerHTML = table([["name", "Value"], ["display", "Reading"], ["pid", "PID"]], r.freeze_frame, "No freeze frame stored.");
  const mt = (r.monitor_tests || []).slice().sort((a, b) => (a.result === "FAIL" ? 0 : 1) - (b.result === "FAIL" ? 0 : 1));
  $("p_mode06").innerHTML = table([["result", "Result"], ["monitor", "Monitor"], ["test", "Test"], ["value", "Value"], ["min", "Min"], ["max", "Max"], ["unit_id", "Unit ID"], ["ecu", "ECU"]],
    mt, "No monitor test results (CAN vehicles only).", (k, v) => k === "result" ? (v === "FAIL" ? "fail" : "pass") : "");
  const fails = mt.filter(t => t.result === "FAIL").length;
  count("mode06", fails, fails > 0);
  $("p_ready").innerHTML = table([["monitor", "Monitor"], ["status", "Status"]], r.readiness, "Not reported.");
  $("p_ecu").innerHTML = table([["item", "Item"], ["value", "Value"], ["ecu", "ECU"]], r.ecu_info, "Not reported.");
  $("p_notes").innerHTML = (r.notes || []).length ? "<ul>" + r.notes.map(n => `<li>${esc(n)}</li>`).join("") + "</ul>" : `<div class="empty">No notes.</div>`;
  count("notes", (r.notes || []).length);
  $("p_raw").innerHTML = table([["tx", "Sent"], ["rx", "Reply"]], r.raw_log, "Empty.");
  setBusy();
}
function setReport(r) {
  const had = !!report; report = r; render();
  if (chat.length) { chat.push({ role: "note", text: "New scan loaded " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + " - answers now use this scan." }); renderChat(); }
}

// ---------- actions ----------
async function loadPorts() {
  const ports = await api("/api/ports").catch(e => { alert(e.message); return []; });
  const sel = $("port"), prev = sel.value;
  sel.innerHTML = ports.length ? ports.map(p => `<option value="${esc(p.device)}">${esc(p.device)} - ${esc(p.description)}${p.obd ? "  [OBD adapter]" : ""}</option>`).join("")
                               : `<option value="">No ports found - plug in the vLinker</option>`;
  if (ports.some(p => p.device === prev)) sel.value = prev;
}
async function scan(extra) { const r = await busy(() => api("/api/scan", { settings: settings(extra) })); if (r) setReport(r); }
async function readLive() {
  const res = await busy(() => api("/api/live", { port: $("port").value }));
  if (!res || !report) return;
  report.live_snapshot = res.live.pids.map(p => ({ name: p.name, display: p.display, pid: p.pid, ecu: "" }));
  report.live_taken = new Date().toLocaleTimeString();
  render();
}
async function probe() {
  const r = await busy(() => api("/api/probe", { port: $("port").value }));
  if (r) openModal(`<h2>Adapter check</h2><pre>${esc(Object.entries(r).map(([k, v]) => k.padEnd(9) + v).join("\n"))}</pre>
    <p>Voltage shows only when the adapter is plugged into the vehicle: about 12.2-12.8 V with the key on, 13.5-14.7 V with the engine running.</p>`);
}
function clearCodes() {
  if (!$("port").value) return alert("Pick an adapter port first (step 1).");
  if (confirm("Clear all codes?\n\nThis erases the stored codes and freeze frame and resets the readiness monitors. Save the codes first. Leave the engine off with the key ON."))
    scan({ clear: true });
}
function saveCsv() {
  if (!report) return alert("Run a scan first.");
  const cols = ["code", "type", "module", "address", "status", "source", "description"];
  const q = v => '"' + String(v ?? "").replace(/"/g, '""') + '"';
  const lines = [["scanned_at", "vin", ...cols].map(q).join(",")].concat(
    (report.codes || []).map(c => [report.scanned_at, report.vin, ...cols.map(k => c[k])].map(q).join(",")));
  download(`codes_${stamp()}.csv`, lines.join("\r\n"), "text/csv");
}
function saveJson() { if (!report) return alert("Run a scan first."); download(`scan_${stamp()}.json`, JSON.stringify(report, null, 2), "application/json"); }

// ---------- API key ----------
function showKey(cfg) {
  const b = $("keyBadge");
  if (!cfg.has_key) { b.className = "badge b-none"; b.textContent = "No key"; }
  else if (!cfg.key_ok) { b.className = "badge b-warn"; b.textContent = "Check key"; }
  else { b.className = "badge b-ok"; b.textContent = cfg.key_source === "saved" ? "Key saved" : cfg.key_source === "environment" ? "Key from environment" : "Key set"; }
  $("remember").checked = cfg.remembered;
  $("key").placeholder = cfg.has_key ? "Key in use - paste to replace" : "Anthropic API key (sk-ant-...)";
}
async function sendKey() {
  try { const cfg = await api("/api/key", { key: $("key").value, remember: $("remember").checked }); $("key").value = ""; showKey(cfg); }
  catch (e) { alert(e.message); }
}

// ---------- AI chat ----------
const QUICK = {
  "Explain my codes": "Explain what each code in this scan means for this vehicle, grouped by system.",
  "What should I check first?": "Based on all the data, what should I check first? Give me the diagnostic steps in order, with what each test should show.",
  "Safe to drive?": "Is this vehicle safe to drive with these codes? What could get worse, and how urgent is each issue?",
  "Check live data": "Look at the live sensor data and tell me if anything looks out of range. If you need the engine running or warmed up first, tell me what to do."
};
function renderChat() {
  const log = $("log");
  if (!chat.length && !waiting) {
    log.innerHTML = `<div class="note" style="margin-top:2rem">${report ? "Scan loaded. Ask a question or tap a quick prompt above." : "Run a scan, then ask me about it. I can also read live data from the vehicle while we talk."}</div>`;
    return;
  }
  const steps = s => (s && s.length) ? `<div class="steps">${s.map(x => "&#9881; " + esc(x)).join("<br>")}</div>` : "";
  log.innerHTML = chat.map(m =>
    m.role === "user" ? `<div class="msg user">${esc(m.text)}</div>` :
    m.role === "ai" ? `<div class="msg ai">${steps(m.steps)}${md(m.text)}<div class="meta">${esc(MODEL)} - about $${m.cost.toFixed(3)}</div></div>` :
    m.role === "err" ? `<div class="msg err">${steps(m.steps)}&#9888; ${esc(m.text)}</div>` :
    `<div class="note">${esc(m.text)}</div>`).join("") +
    (waiting ? `<div class="msg ai" style="color:var(--muted)"><span class="spin"></span>Looking at the data...</div>` : "");
  log.scrollTop = log.scrollHeight;
}
async function ask(q) {
  q = (q || "").trim();
  if (!q || waiting) return;
  chat.push({ role: "user", text: q }); $("q").value = ""; waiting = true; renderChat();
  inflight++; setBusy();
  try {
    const r = await api("/api/chat", { question: q, settings: settings() });
    if (r.error) chat.push({ role: "err", text: r.error, steps: r.steps });
    else chat.push({ role: "ai", text: r.text, steps: r.steps, cost: r.cost });
    if (r.report) { report = r.report; render(); }
  } catch (e) { chat.push({ role: "err", text: e.message }); }
  finally { waiting = false; inflight--; setBusy(); renderChat(); }
}

// ---------- updates ----------
let update = null;
function showUpdate(u) {
  update = u;
  if (!u) return;
  $("updVer").textContent = "OnlyScans " + u.version;
  $("updBtn").textContent = u.can_install ? "Update now" : "Download";
  $("upd").classList.add("on");
}
$("updLater").onclick = () => $("upd").classList.remove("on");
$("updNotes").onclick = e => { e.preventDefault(); if (update) openModal(`<h2>What's new in ${esc(update.version)}</h2>${md(update.notes || "See the release page.")}<p><a href="${esc(update.page)}" target="_blank" rel="noopener">Release page</a></p>`); };
$("updBtn").onclick = async () => {
  if (!update) return;
  if (!update.can_install) { window.open(update.url, "_blank"); return; }
  if (inflight || waiting) return alert("Wait for the current scan or AI answer to finish first.");
  if (!confirm(`Update to OnlyScans ${update.version}?\n\nIt downloads the new version (about 10 MB), then restarts and opens in a new browser tab.`)) return;
  const r = await busy(() => api("/api/update/install", {}));
  if (r) openModal(`<h2>Updating to ${esc(r.version)}</h2><p>OnlyScans is restarting with the new version. It will open in a new browser tab in a few seconds, and you can close this one.</p>`);
};
function pollUpdate(tries) {   // the check runs in the background right after launch
  api("/api/update").then(u => { if (u) showUpdate(u); else if (tries > 0) setTimeout(() => pollUpdate(tries - 1), 3000); }).catch(() => {});
}

// ---------- start ----------
buildTabs(); render(); renderChat();
$("quick").innerHTML = Object.keys(QUICK).map(k => `<button>${esc(k)}</button>`).join("");
$("quick").onclick = e => { const b = e.target.closest("button"); if (b) ask(QUICK[b.textContent]); };
$("sendBtn").onclick = () => ask($("q").value);
$("q").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("q").value); } });
$("resetBtn").onclick = async () => { if (waiting) return; await api("/api/chat/reset", {}); chat = []; renderChat(); };
$("refreshBtn").onclick = () => busy(loadPorts);
$("probeBtn").onclick = probe;
$("scanBtn").onclick = () => scan();
$("clearBtn").onclick = clearCodes;
$("csvBtn").onclick = saveCsv;
$("jsonBtn").onclick = saveJson;
$("key").addEventListener("change", sendKey);
$("remember").addEventListener("change", sendKey);
$("guideBtn").onclick = () => openModal(`<div class="guide">${md(GUIDE)}</div>`);
$("modalX").onclick = closeModal;
$("modal").onclick = e => { if (e.target.id === "modal") closeModal(); };
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
loadPorts();
pollUpdate(4);
api("/api/report").then(r => { if (r) { report = r; render(); renderChat(); } }).catch(() => {});
api("/api/config").then(cfg => {
  showKey(cfg);
  $("ver").textContent = cfg.version === "dev" ? "dev build" : "v" + cfg.version;
  if (!cfg.guide_seen) { $("guideBtn").click(); api("/api/guide_seen", {}); }
}).catch(() => {});
</script>
</body>
</html>
"""


GUIDE_MD = r"""# OnlyScans guide

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
"""


if __name__ == "__main__":
    main()
