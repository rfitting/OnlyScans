//! OnlyScans.exe - a tiny launcher. It carries Windows Python, pyserial and onlyscans.py
//! inside itself, unpacks them once to %LOCALAPPDATA%\OnlyScans, and runs the app, which
//! opens in your browser.
//!
//! Build: python scripts/build_payload.py, then set ONLYSCANS_PAYLOAD (path to
//! build/payload.tar.gz) and ONLYSCANS_PAYLOAD_ID (contents of build/payload.id) and run
//! `cargo build --release` in this folder. See README.md.

use std::env;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{exit, Command};

static PAYLOAD: &[u8] = include_bytes!(env!("ONLYSCANS_PAYLOAD"));
const PAYLOAD_ID: &str = env!("ONLYSCANS_PAYLOAD_ID");

fn pause_and_exit(code: i32) -> ! {
    // Keep the window open so the user can read the error (but never block a CI build).
    if env::var_os("CI").is_some() {
        exit(code);
    }
    print!("\nPress Enter to close this window...");
    let _ = io::stdout().flush();
    let mut s = String::new();
    let _ = io::stdin().read_line(&mut s);
    exit(code);
}

fn fail(msg: &str) -> ! {
    eprintln!("\nOnlyScans couldn't start: {msg}");
    pause_and_exit(1);
}

fn unpack(target: &Path) -> io::Result<()> {
    let partial = target.with_extension("partial");
    if partial.exists() {
        fs::remove_dir_all(&partial)?;
    }
    fs::create_dir_all(&partial)?;
    let gz = flate2::read::GzDecoder::new(PAYLOAD);
    tar::Archive::new(gz).unpack(&partial)?;
    fs::write(partial.join(".complete"), PAYLOAD_ID)?;
    if target.exists() {
        fs::remove_dir_all(target)?;
    }
    fs::rename(&partial, target)
}

fn remove_old_runtimes(base: &Path, keep: &Path) {
    if let Ok(entries) = fs::read_dir(base) {
        for e in entries.flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().to_string();
            if p != keep && name.starts_with("runtime-") && p.is_dir() {
                let _ = fs::remove_dir_all(&p);
            }
        }
    }
}

fn main() {
    let base: PathBuf = env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(env::temp_dir)
        .join("OnlyScans");
    let runtime = base.join(format!("runtime-{PAYLOAD_ID}"));

    if !runtime.join(".complete").exists() {
        println!("Setting up OnlyScans (first run only, takes a few seconds)...");
        if let Err(e) = fs::create_dir_all(&base).and_then(|_| unpack(&runtime)) {
            fail(&format!("unpacking into {} failed: {e}", base.display()));
        }
        remove_old_runtimes(&base, &runtime);
    }

    let python = runtime.join("python").join("python.exe");
    let script = runtime.join("onlyscans.py");
    let status = Command::new(&python)
        .arg(&script)
        .args(env::args_os().skip(1))
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONUTF8", "1")
        .env("ONLYSCANS_EXE", "1")
        .status();

    match status {
        Ok(s) if s.success() => {}
        Ok(s) => {
            eprintln!("\nOnlyScans stopped unexpectedly (exit code {}).", s.code().unwrap_or(-1));
            pause_and_exit(s.code().unwrap_or(1));
        }
        Err(e) => fail(&format!("couldn't run {}: {e}", python.display())),
    }
}
