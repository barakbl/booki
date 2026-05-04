use crate::api::Client;
use anyhow::{anyhow, Context, Result};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

/// Manages the lifecycle of `python -m booki web` (well, `./booki web` since
/// Booki ships an executable script, not a module). We own the process: spawn
/// on launch, SIGTERM on quit.
pub struct ServerProc {
    pub root: PathBuf,
    pub child: Option<Child>,
}

impl ServerProc {
    pub fn new(root: PathBuf) -> Self {
        Self { root, child: None }
    }

    pub fn is_running(&mut self) -> bool {
        match self.child.as_mut() {
            Some(c) => c.try_wait().ok().flatten().is_none(),
            None => false,
        }
    }

    /// Spawn the server. Idempotent — if our child is alive, no-op. If
    /// something else is already listening on the port (separate user-run
    /// `booki web`), we skip spawning and adopt that.
    pub fn ensure_running(&mut self, client: &Client) -> Result<()> {
        if self.is_running() { return Ok(()); }
        if client.health() {
            log::info!("server already up; not spawning");
            return Ok(());
        }

        let booki = self.booki_executable()?;
        // Prefer the repo's own .venv when it's there. The booki script's
        // shebang is `#!/usr/bin/env python3`, which would otherwise resolve
        // to whichever system python is on PATH — typically not the one
        // with fastapi/uvicorn installed when launched outside an
        // activated shell (autostart, GUI relaunch, etc.).
        let mut cmd = match self.venv_python() {
            Some(py) => {
                log::info!("spawning {} {} web (via .venv)", py.display(), booki.display());
                let mut c = Command::new(py);
                c.arg(&booki).arg("web");
                c
            }
            None => {
                log::info!("spawning {} web (via shebang)", booki.display());
                let mut c = Command::new(&booki);
                c.arg("web");
                c
            }
        };
        // Capture stdout/stderr instead of /dev/null'ing them — silent
        // failures (port-already-bound, missing fastapi, …) were
        // impossible to diagnose otherwise. Each line gets re-logged
        // through env_logger so it shows up alongside the manager's own
        // log, prefixed so it's clear what's what.
        let mut child = cmd
            .current_dir(&self.root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .stdin(Stdio::null())
            .spawn()
            .with_context(|| format!("spawn {}", booki.display()))?;
        if let Some(out) = child.stdout.take() {
            std::thread::Builder::new().name("booki-stdout".into()).spawn(move || {
                for line in BufReader::new(out).lines().map_while(Result::ok) {
                    relog_booki_line(&line);
                }
            }).ok();
        }
        if let Some(err) = child.stderr.take() {
            std::thread::Builder::new().name("booki-stderr".into()).spawn(move || {
                for line in BufReader::new(err).lines().map_while(Result::ok) {
                    relog_booki_line(&line);
                }
            }).ok();
        }
        self.child = Some(child);

        // Give the child a moment to crash if it's going to (port collision
        // is the obvious one). Detect early-exit and surface a clear error
        // instead of waiting the full 10s.
        std::thread::sleep(Duration::from_millis(500));
        if let Some(c) = self.child.as_mut() {
            if let Ok(Some(status)) = c.try_wait() {
                self.child = None;
                return Err(anyhow!("booki web exited immediately (status: {})", status));
            }
        }

        // Wait up to ~10s for /api/health to come up.
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if client.health() { return Ok(()); }
            // Bail early if the child already died.
            if let Some(c) = self.child.as_mut() {
                if let Ok(Some(status)) = c.try_wait() {
                    self.child = None;
                    return Err(anyhow!("booki web exited (status: {})", status));
                }
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        Err(anyhow!("server did not become healthy in time"))
    }

    /// Stop the running server — works for *both* our child and an
    /// adopted server (one started outside the manager). Tries the
    /// HTTP shutdown endpoint first; falls back to SIGKILL on the
    /// child if we still have one.
    pub fn shutdown(&mut self, client: &Client) {
        // Best-effort HTTP shutdown — picks up adopted servers too.
        if client.health() {
            if let Err(e) = client.shutdown() {
                log::debug!("api shutdown returned: {}", e);
            }
            // Wait briefly for the listener to release the port.
            let deadline = Instant::now() + Duration::from_secs(3);
            while Instant::now() < deadline {
                if !client.health() { break; }
                std::thread::sleep(Duration::from_millis(150));
            }
        }
        // Hard-kill our own child if it's still around (covers the case
        // where the API endpoint is broken or a hung worker survived).
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }

    fn booki_executable(&self) -> Result<PathBuf> {
        // The repo ships an executable script named `booki` at the root.
        let p = self.root.join("booki");
        if !p.exists() {
            return Err(anyhow!("booki entrypoint not found at {}", p.display()));
        }
        Ok(p)
    }

    /// Return the path to a Python interpreter inside the Booki repo's
    /// own `.venv/`, if one's there. None otherwise — caller falls back
    /// to the dispatcher script's shebang.
    ///
    /// Conventional layouts:
    ///   * `<root>/.venv/bin/python3`        (Linux / macOS, modern)
    ///   * `<root>/.venv/bin/python`         (older venv layouts)
    ///   * `<root>/.venv/Scripts/python.exe` (Windows)
    fn venv_python(&self) -> Option<PathBuf> {
        let venv = self.root.join(".venv");
        if !venv.is_dir() { return None; }
        let candidates: &[&[&str]] = if cfg!(windows) {
            &[&["Scripts", "python.exe"], &["Scripts", "python3.exe"]]
        } else {
            &[&["bin", "python3"], &["bin", "python"]]
        };
        for parts in candidates {
            let mut p = venv.clone();
            for part in *parts { p = p.join(part); }
            if p.is_file() { return Some(p); }
        }
        None
    }
}

impl Drop for ServerProc {
    fn drop(&mut self) {
        // Manager process is exiting; we don't have the Client reference
        // here so just SIGKILL our child (if any). Adopted servers stay
        // running, which is the correct behavior — we didn't start them.
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

#[allow(dead_code)]
pub fn url_in_browser(url: &str) -> Result<()> {
    open_url(url)
}

#[cfg(target_os = "macos")]
fn open_url(url: &str) -> Result<()> {
    Command::new("open").arg(url).spawn()?;
    Ok(())
}
#[cfg(target_os = "linux")]
fn open_url(url: &str) -> Result<()> {
    Command::new("xdg-open").arg(url).spawn()?;
    Ok(())
}
#[cfg(target_os = "windows")]
fn open_url(url: &str) -> Result<()> {
    Command::new("cmd").args(["/C", "start", "", url]).spawn()?;
    Ok(())
}

#[allow(dead_code)]
pub fn touch_path_string(p: &Path) -> String {
    p.display().to_string()
}

/// Re-emit a line of booki/uvicorn output through the manager's logger
/// at the level it actually represents — so a normal "Started server"
/// INFO doesn't masquerade as a manager-level WARN. Format we expect:
///
///   `HH:MM:SS LEVEL    logger.name  message`
///
/// We sniff the second whitespace-separated token. Lines without a
/// recognizable level default to INFO unless they smell like a Python
/// traceback or a raised-exception suffix, in which case they go to
/// WARN so genuine failures still pop visually.
fn relog_booki_line(line: &str) {
    let lvl = detect_log_level(line);
    match lvl {
        log::Level::Error => log::error!("[booki] {}", line),
        log::Level::Warn  => log::warn!("[booki] {}", line),
        log::Level::Info  => log::info!("[booki] {}", line),
        log::Level::Debug => log::debug!("[booki] {}", line),
        log::Level::Trace => log::trace!("[booki] {}", line),
    }
}

fn detect_log_level(line: &str) -> log::Level {
    let trimmed = line.trim_start();
    // Common Python-logger format: "HH:MM:SS LEVEL  logger  msg".
    let mut parts = trimmed.split_whitespace();
    if let Some(first) = parts.next() {
        // First token looks like an HH:MM:SS timestamp → level is the next.
        let ts_shape = first.len() == 8
            && first.bytes().enumerate().all(|(i, b)| match i {
                2 | 5 => b == b':',
                _ => b.is_ascii_digit(),
            });
        if ts_shape {
            if let Some(level_tok) = parts.next() {
                if let Some(l) = parse_level(level_tok) { return l; }
            }
        }
        // No timestamp — maybe the level is the very first token (uvicorn's
        // -reload child sometimes prints lines without one).
        if let Some(l) = parse_level(first) { return l; }
    }
    // Heuristic for stack traces + raised exceptions — these almost always
    // want WARN+ even though they have no level prefix.
    if trimmed.starts_with("Traceback")
        || trimmed.contains("Error:")
        || trimmed.contains("Exception:") {
        return log::Level::Warn;
    }
    log::Level::Info
}

fn parse_level(tok: &str) -> Option<log::Level> {
    match tok {
        "TRACE"           => Some(log::Level::Trace),
        "DEBUG"           => Some(log::Level::Debug),
        "INFO"            => Some(log::Level::Info),
        "WARN" | "WARNING" => Some(log::Level::Warn),
        "ERROR" | "CRITICAL" | "FATAL" => Some(log::Level::Error),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_uvicorn_info() {
        assert_eq!(detect_log_level(
            "18:14:38 INFO    uvicorn.error  Started server process [56684]"),
            log::Level::Info);
    }

    #[test]
    fn detects_uvicorn_error() {
        assert_eq!(detect_log_level(
            "18:06:18 ERROR   uvicorn.error  [Errno 48] address already in use"),
            log::Level::Error);
    }

    #[test]
    fn traceback_lines_go_to_warn() {
        assert_eq!(detect_log_level("Traceback (most recent call last):"),
            log::Level::Warn);
        assert_eq!(detect_log_level("ModuleNotFoundError: No module named 'fastapi'"),
            log::Level::Warn);
    }

    #[test]
    fn frame_lines_default_to_info() {
        // The "  File ..." part of a traceback isn't urgent on its own.
        assert_eq!(detect_log_level(r#"  File "/Users/x/web.py", line 32, in <module>"#),
            log::Level::Info);
    }

    #[test]
    fn untimestamped_info_token() {
        assert_eq!(detect_log_level("INFO     uvicorn started"),
            log::Level::Info);
    }
}
