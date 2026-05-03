use anyhow::Result;
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Source {
    Chrome,
    Safari,
    Firefox,
}

impl Source {
    pub fn name(self) -> &'static str {
        match self {
            Source::Chrome => "chrome",
            Source::Safari => "safari",
            Source::Firefox => "firefox",
        }
    }
}

/// File globs Booki's Python plugin actually reads. The watcher walks
/// each base directory, so we return *directories to watch* rather than
/// concrete files — Chrome rewrites its Bookmarks file atomically (which
/// often produces a CREATE+REMOVE pair) and Firefox writes a SQLite WAL,
/// so directory-level watching is more robust than per-file.
pub fn watch_targets(source: Source) -> Vec<PathBuf> {
    let home = match dirs::home_dir() {
        Some(h) => h,
        None => return vec![],
    };
    match source {
        Source::Chrome => chrome_dirs(&home),
        Source::Safari => safari_dirs(&home),
        Source::Firefox => firefox_dirs(&home),
    }
}

#[cfg(target_os = "macos")]
fn chrome_dirs(home: &std::path::Path) -> Vec<PathBuf> {
    profile_dirs_containing(
        home.join("Library/Application Support/Google/Chrome"),
        "Bookmarks",
    )
}
#[cfg(target_os = "linux")]
fn chrome_dirs(home: &std::path::Path) -> Vec<PathBuf> {
    profile_dirs_containing(home.join(".config/google-chrome"), "Bookmarks")
}
#[cfg(target_os = "windows")]
fn chrome_dirs(_home: &std::path::Path) -> Vec<PathBuf> {
    if let Some(local) = dirs::data_local_dir() {
        return profile_dirs_containing(local.join("Google/Chrome/User Data"), "Bookmarks");
    }
    vec![]
}

#[cfg(target_os = "macos")]
fn safari_dirs(home: &std::path::Path) -> Vec<PathBuf> {
    let p = home.join("Library/Safari");
    if p.join("Bookmarks.plist").exists() { vec![p] } else { vec![] }
}
#[cfg(not(target_os = "macos"))]
fn safari_dirs(_home: &std::path::Path) -> Vec<PathBuf> {
    vec![]
}

#[cfg(target_os = "macos")]
fn firefox_dirs(home: &std::path::Path) -> Vec<PathBuf> {
    profile_dirs_containing(
        home.join("Library/Application Support/Firefox/Profiles"),
        "places.sqlite",
    )
}
#[cfg(target_os = "linux")]
fn firefox_dirs(home: &std::path::Path) -> Vec<PathBuf> {
    profile_dirs_containing(home.join(".mozilla/firefox"), "places.sqlite")
}
#[cfg(target_os = "windows")]
fn firefox_dirs(_home: &std::path::Path) -> Vec<PathBuf> {
    if let Some(roaming) = dirs::config_dir() {
        return profile_dirs_containing(
            roaming.join("Mozilla/Firefox/Profiles"),
            "places.sqlite",
        );
    }
    vec![]
}

/// List immediate child directories under `base` that contain the named
/// bookmark file. Filters out caches, codecs, and other non-profile
/// subdirectories that browsers also drop into the same root.
fn profile_dirs_containing(base: PathBuf, marker: &str) -> Vec<PathBuf> {
    let mut out = vec![];
    let rd = match std::fs::read_dir(&base) {
        Ok(r) => r,
        Err(_) => return out,
    };
    for entry in rd.flatten() {
        let p = entry.path();
        if p.is_dir() && p.join(marker).exists() {
            out.push(p);
        }
    }
    out
}

/// Heuristic: ignore changes to ancillary files we know aren't bookmark
/// content (Chrome's `Bookmarks.bak`, Firefox's `places.sqlite-journal`,
/// lock files, log dirs, etc.).
pub fn is_relevant_change(path: &std::path::Path, source: Source) -> bool {
    let name = match path.file_name().and_then(|n| n.to_str()) {
        Some(n) => n,
        None => return false,
    };
    match source {
        Source::Chrome => name == "Bookmarks",
        Source::Safari => name == "Bookmarks.plist",
        // SQLite writes through .sqlite, .sqlite-wal, .sqlite-shm — all relevant.
        Source::Firefox => name.starts_with("places.sqlite"),
    }
}

/// Locate Booki's project root: prefer `$BOOKI_HOME`, else fall back to the
/// current working directory. The Python `booki` script is expected to live
/// at `<root>/booki`.
pub fn booki_root() -> Result<PathBuf> {
    if let Ok(v) = std::env::var("BOOKI_HOME") {
        return Ok(PathBuf::from(v));
    }
    Ok(std::env::current_dir()?)
}
