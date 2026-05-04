//! Manager-owned settings, persisted under `$XDG_CONFIG_HOME/booki-manager/`.
//!
//! Keeps the manager's own preferences (which Booki checkout to talk to,
//! and any future per-tray-app knobs) separate from `config.toml` — that
//! file lives next to whichever Booki this manager points at and the
//! manager doesn't own its lifecycle.
//!
//! Lookup order for the active Booki path (see `paths::booki_root`):
//!
//!   1. `$BOOKI_HOME` env var          — highest priority, lets a power user
//!                                       launch the manager pointing at a
//!                                       one-off clone for testing.
//!   2. `Settings::booki_home`          — what the user picked from the
//!                                       tray's "Pick Booki folder…" item;
//!                                       what the autostart path uses.
//!   3. `std::env::current_dir()`       — last-resort fallback, useful
//!                                       only when running the binary from
//!                                       inside a checkout.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Settings {
    /// Path to the Booki checkout the manager talks to. `None` until the
    /// user picks one (or `BOOKI_HOME` is set in the environment).
    #[serde(default)]
    pub booki_home: Option<PathBuf>,
}

/// Where on disk the settings live.
pub fn path() -> PathBuf {
    let base = dirs::config_dir()
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join(".config"));
    base.join("booki-manager").join("settings.json")
}

/// Read settings from disk. Missing or unreadable file → defaults — never
/// returns an error so a fresh install just works.
pub fn load() -> Settings {
    let p = path();
    let Ok(text) = fs::read_to_string(&p) else { return Settings::default() };
    serde_json::from_str(&text).unwrap_or_default()
}

/// Persist settings, creating the parent directory if needed.
pub fn save(s: &Settings) -> Result<()> {
    let p = path();
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("create {}", parent.display()))?;
    }
    let text = serde_json::to_string_pretty(s).context("serialize settings")?;
    fs::write(&p, text).with_context(|| format!("write {}", p.display()))?;
    Ok(())
}

/// `true` when `path` looks like a Booki checkout — has both the
/// dispatcher script and a `config.toml` next to it. Used to validate
/// what the user picks from the folder dialog before saving it.
pub fn looks_like_booki(path: &Path) -> bool {
    path.is_dir()
        && path.join("booki").is_file()
        && path.join("config.toml").is_file()
}
