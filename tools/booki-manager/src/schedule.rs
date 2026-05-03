//! Periodic job scheduler for `sync` and `ingest`.
//!
//! Reads `[manager.schedule.<job>]` from `config.toml`. Each entry is:
//!
//! ```toml
//! [manager.schedule.sync]
//! cadence = "daily"           # "daily" | "weekly" | "off"
//! window  = "02:00-05:00"     # local time; optional → defaults to anytime
//! ```
//!
//! "Due" semantics: a job is due when (a) at least one cadence-period has
//! elapsed since the last successful run AND (b) we're inside the window
//! OR the window already ended today (catch-up after sleep). `last_run`
//! per job is persisted to `<state-dir>/booki-manager/state.json` so a
//! restart doesn't lose the schedule.

use anyhow::{anyhow, Context, Result};
use jiff::civil::Time;
use jiff::{Timestamp, Zoned};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cadence { Off, Daily, Weekly }

impl Cadence {
    fn parse(s: &str) -> Result<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "off" | "" | "disabled" => Ok(Cadence::Off),
            "daily"  => Ok(Cadence::Daily),
            "weekly" => Ok(Cadence::Weekly),
            other => Err(anyhow!("invalid cadence: {} (expected off/daily/weekly)", other)),
        }
    }
    fn period_secs(self) -> Option<i64> {
        match self {
            Cadence::Off    => None,
            Cadence::Daily  => Some(24 * 3600),
            Cadence::Weekly => Some(7 * 24 * 3600),
        }
    }
    fn label(self) -> &'static str {
        match self {
            Cadence::Off    => "off",
            Cadence::Daily  => "daily",
            Cadence::Weekly => "weekly",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Window {
    pub start: Time,  // inclusive
    pub end:   Time,  // exclusive; if end <= start the window wraps midnight
}

impl Window {
    fn parse(s: &str) -> Result<Self> {
        let (a, b) = s.split_once('-')
            .ok_or_else(|| anyhow!("window must be HH:MM-HH:MM, got {}", s))?;
        Ok(Self { start: parse_hhmm(a)?, end: parse_hhmm(b)? })
    }

    fn contains(&self, t: Time) -> bool {
        if self.start <= self.end {
            t >= self.start && t < self.end
        } else {
            // Wraps over midnight (e.g. 22:00-04:00).
            t >= self.start || t < self.end
        }
    }
}

fn parse_hhmm(s: &str) -> Result<Time> {
    let s = s.trim();
    let (h, m) = s.split_once(':').ok_or_else(|| anyhow!("expected HH:MM, got {}", s))?;
    let h: i8 = h.parse().with_context(|| format!("hour {}", h))?;
    let m: i8 = m.parse().with_context(|| format!("minute {}", m))?;
    if !(0..=23).contains(&h) || !(0..=59).contains(&m) {
        return Err(anyhow!("HH:MM out of range: {}", s));
    }
    Time::new(h, m, 0, 0).map_err(|e| anyhow!("time {}: {}", s, e))
}

#[derive(Debug, Clone)]
pub struct JobSchedule {
    pub cadence: Cadence,
    pub window:  Option<Window>,
}

/// Top-level schedule for both jobs. Either may be missing or `Off`.
#[derive(Debug, Clone, Default)]
pub struct ScheduleConfig {
    pub sync:   Option<JobSchedule>,
    pub ingest: Option<JobSchedule>,
}

impl ScheduleConfig {
    pub fn from_config_toml(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("read {}", path.display()))?;
        let val: toml::Value = toml::from_str(&text).context("parse config.toml")?;
        let mgr = val.get("manager")
            .and_then(|m| m.get("schedule"))
            .cloned()
            .unwrap_or_else(|| toml::Value::Table(Default::default()));
        let mut out = Self::default();
        if let Some(t) = mgr.as_table() {
            if let Some(s) = t.get("sync") { out.sync = Some(parse_job(s)?); }
            if let Some(s) = t.get("ingest") { out.ingest = Some(parse_job(s)?); }
        }
        Ok(out)
    }
}

fn parse_job(v: &toml::Value) -> Result<JobSchedule> {
    let t = v.as_table().ok_or_else(|| anyhow!("[manager.schedule.<job>] must be a table"))?;
    let cadence = match t.get("cadence").and_then(|v| v.as_str()) {
        Some(s) => Cadence::parse(s)?,
        None    => Cadence::Off,
    };
    let window = match t.get("window").and_then(|v| v.as_str()) {
        Some(s) => Some(Window::parse(s)?),
        None    => None,
    };
    Ok(JobSchedule { cadence, window })
}

// ─── persisted state ────────────────────────────────────────────────────────

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct PersistState {
    /// Map of job kind ("sync" | "ingest") → ISO-8601 timestamp.
    #[serde(default)]
    last_run: BTreeMap<String, String>,
}

#[derive(Debug, Clone)]
pub struct ScheduleState {
    last_run: BTreeMap<String, Timestamp>,
}

impl ScheduleState {
    pub fn empty() -> Self {
        Self { last_run: BTreeMap::new() }
    }

    pub fn last(&self, kind: &str) -> Option<Timestamp> {
        self.last_run.get(kind).copied()
    }

    pub fn record(&mut self, kind: &str, when: Timestamp) {
        self.last_run.insert(kind.to_string(), when);
    }

    fn from_persist(p: &PersistState) -> Self {
        let mut out = BTreeMap::new();
        for (k, v) in &p.last_run {
            if let Ok(ts) = v.parse::<Timestamp>() {
                out.insert(k.clone(), ts);
            }
        }
        Self { last_run: out }
    }

    fn to_persist(&self) -> PersistState {
        let mut out = BTreeMap::new();
        for (k, v) in &self.last_run {
            out.insert(k.clone(), v.to_string());
        }
        PersistState { last_run: out }
    }
}

// ─── scheduler ──────────────────────────────────────────────────────────────

pub struct Scheduler {
    pub cfg:   ScheduleConfig,
    pub state: ScheduleState,
    pub state_path: PathBuf,
}

impl Scheduler {
    pub fn load(config_toml: &Path, state_dir: &Path) -> Result<Self> {
        let cfg = ScheduleConfig::from_config_toml(config_toml)?;
        std::fs::create_dir_all(state_dir).ok();
        let state_path = state_dir.join("state.json");
        let state = if state_path.exists() {
            let bytes = std::fs::read(&state_path)
                .with_context(|| format!("read {}", state_path.display()))?;
            let p: PersistState = serde_json::from_slice(&bytes).unwrap_or_default();
            ScheduleState::from_persist(&p)
        } else {
            ScheduleState::empty()
        };
        Ok(Self { cfg, state, state_path })
    }

    /// Persist `state` to disk. Best-effort; logs but does not propagate I/O
    /// errors (the menu shouldn't die because we couldn't write a JSON file).
    fn persist(&self) {
        let p = self.state.to_persist();
        let json = match serde_json::to_vec_pretty(&p) {
            Ok(b) => b,
            Err(e) => { log::warn!("encode state: {}", e); return; }
        };
        if let Err(e) = std::fs::write(&self.state_path, json) {
            log::warn!("write {}: {}", self.state_path.display(), e);
        }
    }

    /// Record a successful run and persist.
    pub fn record_run(&mut self, kind: &str, when: Timestamp) {
        self.state.record(kind, when);
        self.persist();
    }

    /// Which jobs ("sync" / "ingest") are due to fire right now?
    pub fn due_now(&self, now: &Zoned) -> Vec<&'static str> {
        let mut out = vec![];
        if let Some(s) = &self.cfg.sync {
            if is_due(s, self.state.last("sync"), now) { out.push("sync"); }
        }
        if let Some(s) = &self.cfg.ingest {
            if is_due(s, self.state.last("ingest"), now) { out.push("ingest"); }
        }
        out
    }

    /// Human-friendly summary for the menu's info line. Returns e.g.
    /// "sync daily 02:00-05:00 · ingest weekly 03:00-05:00" or
    /// "scheduling off" when nothing is configured.
    pub fn summary(&self) -> String {
        let mut parts = vec![];
        for (name, sched) in [("sync", &self.cfg.sync), ("ingest", &self.cfg.ingest)] {
            if let Some(s) = sched {
                if matches!(s.cadence, Cadence::Off) { continue; }
                let win = match &s.window {
                    Some(w) => format!(" {}-{}", fmt_time(w.start), fmt_time(w.end)),
                    None => String::new(),
                };
                parts.push(format!("{} {}{}", name, s.cadence.label(), win));
            }
        }
        if parts.is_empty() { "scheduling off".into() } else { parts.join(" · ") }
    }
}

fn fmt_time(t: Time) -> String {
    format!("{:02}:{:02}", t.hour(), t.minute())
}

fn is_due(s: &JobSchedule, last: Option<Timestamp>, now: &Zoned) -> bool {
    let period = match s.cadence.period_secs() {
        Some(p) => p,
        None    => return false,
    };
    if let Some(last_ts) = last {
        let elapsed = (now.timestamp() - last_ts).get_seconds();
        if elapsed < period { return false; }
    }
    let win = match &s.window {
        None    => return true,
        Some(w) => *w,
    };
    if win.contains(now.time()) { return true; }
    // Catch-up: window already passed today AND we haven't run since the
    // window's start today (i.e. we slept through the window).
    past_window_today(win, last, now)
}

fn past_window_today(win: Window, last: Option<Timestamp>, now: &Zoned) -> bool {
    let tz = now.time_zone();
    let today = now.date();
    let mk = |t: Time| today.at(t.hour(), t.minute(), 0, 0).to_zoned(tz.clone());
    let win_end = match mk(win.end) { Ok(z) => z, Err(_) => return false };
    let win_start = match mk(win.start) { Ok(z) => z, Err(_) => return false };
    if now.timestamp() < win_end.timestamp() { return false; }
    match last {
        None => true,
        Some(l) => l < win_start.timestamp(),
    }
}

// ─── tests ──────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use jiff::tz::TimeZone;

    fn zoned(s: &str) -> Zoned {
        // RFC3339-ish; tests pass an explicit offset so the test is
        // independent of the host's local tz.
        s.parse().expect("valid Zoned")
    }
    fn ts(s: &str) -> Timestamp { s.parse().expect("valid Timestamp") }

    fn sched(cad: Cadence, win: Option<&str>) -> JobSchedule {
        JobSchedule { cadence: cad, window: win.map(|s| Window::parse(s).unwrap()) }
    }

    #[test]
    fn off_is_never_due() {
        let s = sched(Cadence::Off, Some("02:00-05:00"));
        let now = zoned("2026-05-02T03:00:00+00:00[UTC]");
        assert!(!is_due(&s, None, &now));
    }

    #[test]
    fn daily_in_window_first_run() {
        let s = sched(Cadence::Daily, Some("02:00-05:00"));
        let now = zoned("2026-05-02T03:00:00+00:00[UTC]");
        assert!(is_due(&s, None, &now));
    }

    #[test]
    fn daily_in_window_too_recent() {
        let s = sched(Cadence::Daily, Some("02:00-05:00"));
        let now = zoned("2026-05-02T03:00:00+00:00[UTC]");
        let last = ts("2026-05-01T03:30:00+00:00");  // ~23.5h ago
        assert!(!is_due(&s, Some(last), &now));
    }

    #[test]
    fn daily_outside_window_before_waits() {
        let s = sched(Cadence::Daily, Some("02:00-05:00"));
        let now = zoned("2026-05-02T01:30:00+00:00[UTC]");
        assert!(!is_due(&s, None, &now));
    }

    #[test]
    fn daily_outside_window_after_catches_up_when_overdue() {
        // It's noon, the window ended 7 hours ago, and we never ran today
        // (or ever). The catch-up branch should fire.
        let s = sched(Cadence::Daily, Some("02:00-05:00"));
        let now = zoned("2026-05-02T12:00:00+00:00[UTC]");
        assert!(is_due(&s, None, &now));
    }

    #[test]
    fn daily_outside_window_after_does_not_double_run() {
        // We already ran during today's window. Don't re-run after.
        let s = sched(Cadence::Daily, Some("02:00-05:00"));
        let now = zoned("2026-05-02T12:00:00+00:00[UTC]");
        let last = ts("2026-05-02T03:00:00+00:00");
        assert!(!is_due(&s, Some(last), &now));
    }

    #[test]
    fn weekly_respects_seven_days() {
        let s = sched(Cadence::Weekly, None);
        let now = zoned("2026-05-08T10:00:00+00:00[UTC]");
        let last_5d = ts("2026-05-03T10:00:00+00:00");
        let last_8d = ts("2026-04-30T10:00:00+00:00");
        assert!(!is_due(&s, Some(last_5d), &now));
        assert!( is_due(&s, Some(last_8d), &now));
    }

    #[test]
    fn no_window_means_anytime() {
        let s = sched(Cadence::Daily, None);
        let now = zoned("2026-05-02T17:42:00+00:00[UTC]");
        assert!(is_due(&s, None, &now));
    }

    #[test]
    fn window_wraps_midnight() {
        let w = Window::parse("22:00-04:00").unwrap();
        assert!( w.contains(Time::new(23, 0, 0, 0).unwrap()));
        assert!( w.contains(Time::new( 1, 0, 0, 0).unwrap()));
        assert!(!w.contains(Time::new( 5, 0, 0, 0).unwrap()));
        assert!(!w.contains(Time::new(21, 0, 0, 0).unwrap()));
    }

    #[test]
    fn _zone_round_trip() {
        // Smoke-check that jiff types behave the way we use them.
        let z = Zoned::now().with_time_zone(TimeZone::UTC);
        let _ = z.date();
        let _ = z.time();
    }
}
