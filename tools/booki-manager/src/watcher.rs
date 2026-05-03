use crate::paths::{is_relevant_change, watch_targets, Source};
use anyhow::Result;
use crossbeam_channel::Sender;
use notify::RecursiveMode;
use notify_debouncer_full::{new_debouncer, DebouncedEvent};
use std::time::Duration;

/// Event emitted when a debounced bookmark-file change is observed.
#[derive(Debug, Clone)]
pub struct ChangeEvent {
    pub source: Source,
}

/// Spawn the watcher. Holds the debouncer alive in a background thread —
/// returns immediately. Drop the returned guard to stop watching.
pub fn spawn(sources: Vec<Source>, out: Sender<ChangeEvent>) -> Result<WatcherHandle> {
    let (tx, rx) = std::sync::mpsc::channel();

    let mut debouncer = new_debouncer(Duration::from_secs(2), None, tx)?;

    // Accumulate (path, source) pairs so we can map a fired event back to
    // which source's directory it came from.
    let mut watched: Vec<(std::path::PathBuf, Source)> = vec![];
    for src in sources {
        for dir in watch_targets(src) {
            log::info!("watching {:?} for {:?}", dir, src);
            if let Err(e) = debouncer.watch(&dir, RecursiveMode::Recursive) {
                log::warn!("watch {} failed: {}", dir.display(), e);
                continue;
            }
            watched.push((dir, src));
        }
    }
    if watched.is_empty() {
        log::warn!("no source directories found to watch");
    }

    let handle = std::thread::Builder::new()
        .name("booki-manager-watcher".into())
        .spawn(move || {
            // Move the debouncer into the thread so it stays alive.
            let _keep = debouncer;
            for batch in rx {
                let events = match batch {
                    Ok(evs) => evs,
                    Err(errs) => {
                        for e in errs { log::warn!("watch error: {}", e); }
                        continue;
                    }
                };
                for src in detect_relevant(&events, &watched) {
                    log::info!("debounced change for {:?}", src);
                    let _ = out.send(ChangeEvent { source: src });
                }
            }
        })?;

    Ok(WatcherHandle { _join: Some(handle) })
}

fn detect_relevant(
    events: &[DebouncedEvent],
    watched: &[(std::path::PathBuf, Source)],
) -> Vec<Source> {
    let mut hit: std::collections::BTreeSet<&'static str> = Default::default();
    let mut out = vec![];
    for ev in events {
        for path in &ev.paths {
            // Map the path back to its source via the longest prefix match.
            let src = watched.iter()
                .filter(|(dir, _)| path.starts_with(dir))
                .max_by_key(|(dir, _)| dir.as_os_str().len())
                .map(|(_, s)| *s);
            let src = match src { Some(s) => s, None => continue };
            if !is_relevant_change(path, src) { continue; }
            if hit.insert(src.name()) {
                out.push(src);
            }
        }
    }
    out
}

pub struct WatcherHandle {
    _join: Option<std::thread::JoinHandle<()>>,
}
