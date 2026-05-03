use std::sync::{Arc, Mutex};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    Idle,
    Syncing,
    ServerDown,
    Error,
}

#[derive(Debug, Clone)]
pub struct LastSync {
    pub when: String,        // human-friendly local timestamp
    pub source: String,      // which source ran
    pub success: bool,
    pub message: String,     // short error excerpt or "ok"
}

#[derive(Debug, Clone)]
pub struct AppState {
    pub status: Status,
    pub last: Option<LastSync>,
    /// If true, watcher-triggered syncs are skipped (manual triggers still work).
    pub paused: bool,
    /// If true, the scheduler ticker doesn't fire scheduled jobs.
    pub paused_schedule: bool,
    pub autostart: bool,
    /// Cached human-friendly summary of the configured schedule, refreshed
    /// at startup. Shown in the menu so the user can see what's scheduled.
    pub schedule_summary: String,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            status: Status::Idle,
            last: None,
            paused: false,
            paused_schedule: false,
            autostart: false,
            schedule_summary: String::new(),
        }
    }
}

pub type Shared = Arc<Mutex<AppState>>;

pub fn shared() -> Shared {
    Arc::new(Mutex::new(AppState::new()))
}
