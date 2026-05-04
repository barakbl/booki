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
        log::info!("spawning {} web", booki.display());
        // Capture stdout/stderr instead of /dev/null'ing them — silent
        // failures (port-already-bound, missing fastapi, …) were
        // impossible to diagnose otherwise. Each line gets re-logged
        // through env_logger so it shows up alongside the manager's own
        // log, prefixed so it's clear what's what.
        let mut child = Command::new(&booki)
            .arg("web")
            .current_dir(&self.root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .stdin(Stdio::null())
            .spawn()
            .with_context(|| format!("spawn {}", booki.display()))?;
        if let Some(out) = child.stdout.take() {
            std::thread::Builder::new().name("booki-stdout".into()).spawn(move || {
                for line in BufReader::new(out).lines().map_while(Result::ok) {
                    log::info!("[booki] {}", line);
                }
            }).ok();
        }
        if let Some(err) = child.stderr.take() {
            std::thread::Builder::new().name("booki-stderr".into()).spawn(move || {
                for line in BufReader::new(err).lines().map_while(Result::ok) {
                    log::warn!("[booki] {}", line);
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
