use booki_manager::api::Client;
use booki_manager::config::AppConfig;
use booki_manager::paths::{booki_root, Source};
use booki_manager::schedule::Scheduler;
use booki_manager::server::ServerProc;
use booki_manager::state::{AppState, LastSync, Shared, Status};
use booki_manager::{autostart, menu, server, state, watcher};
use anyhow::{Context, Result};
use crossbeam_channel::{unbounded, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tray_icon::menu::MenuEvent;

#[derive(Debug)]
enum AppEvent {
    Watcher(watcher::ChangeEvent),
    /// Generic per-job lifecycle. `kind` is "sync" or "ingest"; `label` is
    /// what the user sees in the "Last:" line (e.g. source name, or "ingest").
    JobStarted { kind: String, label: String },
    JobFinished { kind: String, label: String, success: bool, message: String },
    HealthChanged(bool),
    Menu(String),
    /// Scheduler ticker fired and there are due jobs to run.
    ScheduleDue(Vec<String>),
}

fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format_timestamp_secs()
        .init();

    let root = booki_root().context("locate Booki root")?;
    let cfg_path = root.join("config.toml");
    let cfg = AppConfig::load(&cfg_path).with_context(|| {
        format!("load {}", cfg_path.display())
    })?;
    log::info!("config: web {} sources {:?}",
        cfg.web_base(),
        cfg.enabled_sources.iter().map(|s| s.name()).collect::<Vec<_>>());

    let client = Client::new(cfg.web_base());
    let server = Arc::new(Mutex::new(ServerProc::new(root.clone())));
    let state = state::shared();
    {
        let st = state.clone();
        if let Ok(au) = autostart::Autostart::new() {
            st.lock().unwrap().autostart = au.enabled();
        }
    }

    // Scheduler: parses [manager.schedule.*] from the same config.toml,
    // persists last_run under the platform state dir.
    let state_dir = dirs::state_dir()
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join(".local/state"))
        .join("booki-manager");
    let scheduler = Arc::new(Mutex::new(
        Scheduler::load(&cfg_path, &state_dir).context("init scheduler")?,
    ));
    {
        let summary = scheduler.lock().unwrap().summary();
        log::info!("schedule: {}", summary);
        state.lock().unwrap().schedule_summary = summary;
    }

    // Channels.
    let (app_tx, app_rx) = unbounded::<AppEvent>();
    let (watch_tx, watch_rx) = unbounded::<watcher::ChangeEvent>();

    // Wire the watcher → app channel.
    let _watcher = watcher::spawn(cfg.enabled_sources.clone(), watch_tx)?;
    {
        let app_tx = app_tx.clone();
        std::thread::Builder::new().name("watcher-bridge".into()).spawn(move || {
            for ev in watch_rx {
                let _ = app_tx.send(AppEvent::Watcher(ev));
            }
        })?;
    }

    // Periodic health check (every 5s) so the menu reflects server state
    // even when the user isn't clicking around.
    {
        let client = client.clone();
        let app_tx = app_tx.clone();
        std::thread::Builder::new().name("health-poller".into()).spawn(move || {
            let mut last: Option<bool> = None;
            loop {
                std::thread::sleep(Duration::from_secs(5));
                let now = client.health();
                if Some(now) != last {
                    last = Some(now);
                    let _ = app_tx.send(AppEvent::HealthChanged(now));
                }
            }
        })?;
    }

    // Bridge tray-icon's MenuEvent global channel → AppEvent::Menu.
    {
        let app_tx = app_tx.clone();
        std::thread::Builder::new().name("menu-bridge".into()).spawn(move || {
            let rx = MenuEvent::receiver();
            for ev in rx.iter() {
                let _ = app_tx.send(AppEvent::Menu(ev.id().0.clone()));
            }
        })?;
    }

    // Schedule ticker: once a minute, check whether any scheduled jobs are
    // due. Skip when the user has paused scheduled jobs from the menu.
    // 60s granularity is plenty — the cheapest window is "daily 02:00-05:00",
    // a 60s tick can't miss the 3-hour window even after a long sleep.
    {
        let scheduler = scheduler.clone();
        let state = state.clone();
        let app_tx = app_tx.clone();
        std::thread::Builder::new().name("schedule-ticker".into()).spawn(move || {
            loop {
                std::thread::sleep(Duration::from_secs(60));
                if state.lock().unwrap().paused_schedule { continue; }
                let now = jiff::Zoned::now();
                let due: Vec<String> = scheduler.lock().unwrap()
                    .due_now(&now).into_iter().map(String::from).collect();
                if !due.is_empty() {
                    log::info!("schedule due: {:?}", due);
                    let _ = app_tx.send(AppEvent::ScheduleDue(due));
                }
            }
        })?;
    }

    // Start the Python server up front. Failure here doesn't kill the app —
    // the menu will show "Server: not running" and sync attempts will retry.
    {
        let mut s = server.lock().unwrap();
        if let Err(e) = s.ensure_running(&client) {
            log::warn!("initial server spawn failed: {}", e);
            state.lock().unwrap().status = Status::ServerDown;
        }
    }

    // Build the tray icon. tray-icon must be built on the main thread on
    // macOS, so this stays in main.
    let event_loop = EventLoopBuilder::new().build();
    let tray = menu::build()?;
    menu::refresh(&tray.items, &state.lock().unwrap());

    let menu_ids = MenuIds {
        status:         tray.items.status.id().0.clone(),
        last:           tray.items.last.id().0.clone(),
        schedule_info:  tray.items.schedule_info.id().0.clone(),
        sync_now:       tray.items.sync_now.id().0.clone(),
        ingest_now:     tray.items.ingest_now.id().0.clone(),
        pause:          tray.items.pause.id().0.clone(),
        pause_schedule: tray.items.pause_schedule.id().0.clone(),
        open_web:       tray.items.open_web.id().0.clone(),
        autostart:      tray.items.autostart.id().0.clone(),
        quit:           tray.items.quit.id().0.clone(),
    };

    let shared_for_loop = AppLoopShared {
        state: state.clone(),
        client: client.clone(),
        server: server.clone(),
        cfg: cfg.clone(),
        scheduler: scheduler.clone(),
        app_tx: app_tx.clone(),
        items: ItemsView {
            status: tray.items.status,
            last: tray.items.last,
            schedule_info: tray.items.schedule_info,
            sync_now: tray.items.sync_now,
            ingest_now: tray.items.ingest_now,
            pause: tray.items.pause,
            pause_schedule: tray.items.pause_schedule,
            open_web: tray.items.open_web,
            autostart: tray.items.autostart,
            icon: tray.icon,
        },
    };

    event_loop.run(move |_event, _, control_flow| {
        // Drain pending app events. tao runs this closure frequently; we
        // poll the channel non-blockingly so the UI stays responsive.
        *control_flow = ControlFlow::WaitUntil(
            std::time::Instant::now() + std::time::Duration::from_millis(100));

        while let Ok(ev) = shared_for_loop.app_tx_recv(&app_rx).try_recv() {
            handle_event(&shared_for_loop, &menu_ids, ev, control_flow);
        }
    });
}

struct MenuIds {
    #[allow(dead_code)] status: String,
    #[allow(dead_code)] last: String,
    #[allow(dead_code)] schedule_info: String,
    sync_now: String,
    ingest_now: String,
    pause: String,
    pause_schedule: String,
    open_web: String,
    autostart: String,
    quit: String,
}

struct ItemsView {
    status: tray_icon::menu::MenuItem,
    last: tray_icon::menu::MenuItem,
    schedule_info: tray_icon::menu::MenuItem,
    #[allow(dead_code)] sync_now: tray_icon::menu::MenuItem,
    #[allow(dead_code)] ingest_now: tray_icon::menu::MenuItem,
    pause: tray_icon::menu::MenuItem,
    pause_schedule: tray_icon::menu::MenuItem,
    #[allow(dead_code)] open_web: tray_icon::menu::MenuItem,
    autostart: tray_icon::menu::MenuItem,
    icon: tray_icon::TrayIcon,
}

struct AppLoopShared {
    state: Shared,
    client: Client,
    server: Arc<Mutex<ServerProc>>,
    cfg: AppConfig,
    scheduler: Arc<Mutex<Scheduler>>,
    app_tx: Sender<AppEvent>,
    items: ItemsView,
}

impl AppLoopShared {
    fn app_tx_recv<'a>(&self, rx: &'a Receiver<AppEvent>) -> &'a Receiver<AppEvent> { rx }
}

fn handle_event(
    s: &AppLoopShared,
    ids: &MenuIds,
    ev: AppEvent,
    control_flow: &mut ControlFlow,
) {
    match ev {
        AppEvent::Watcher(c) => {
            let paused = s.state.lock().unwrap().paused;
            if paused {
                log::info!("paused — ignoring change for {}", c.source.name());
                return;
            }
            kick_sync_source(s, c.source);
        }
        AppEvent::JobStarted { kind, label } => {
            log::info!("job started: {} ({})", kind, label);
            s.state.lock().unwrap().status = Status::Syncing;
        }
        AppEvent::JobFinished { kind, label, success, message } => {
            log::info!("job finished: {} ({}) ok={} ({})", kind, label, success, message);
            let mut st = s.state.lock().unwrap();
            st.status = if success { Status::Idle } else { Status::Error };
            st.last = Some(LastSync {
                when: now_local(),
                source: format!("{} {}", kind, label),
                success,
                message,
            });
            drop(st);
            // Persist the run so catch-up logic doesn't re-fire it.
            if success {
                s.scheduler.lock().unwrap().record_run(&kind, jiff::Timestamp::now());
            }
        }
        AppEvent::HealthChanged(up) => {
            let mut st = s.state.lock().unwrap();
            if !up {
                st.status = Status::ServerDown;
            } else if matches!(st.status, Status::ServerDown) {
                st.status = Status::Idle;
            }
        }
        AppEvent::ScheduleDue(kinds) => {
            for kind in kinds {
                match kind.as_str() {
                    "sync" => {
                        // Sync every configured source — same as "Sync now".
                        for src in s.cfg.enabled_sources.clone() {
                            kick_sync_source(s, src);
                        }
                    }
                    "ingest" => kick_ingest(s),
                    other => log::warn!("schedule-due unknown kind: {}", other),
                }
            }
        }
        AppEvent::Menu(id) => {
            handle_menu_click(s, ids, &id, control_flow);
            return; // refresh is done inside the click handler if needed
        }
    }
    refresh_items(&s.items, &s.state.lock().unwrap());
}

fn handle_menu_click(
    s: &AppLoopShared,
    ids: &MenuIds,
    id: &str,
    control_flow: &mut ControlFlow,
) {
    if id == ids.quit {
        log::info!("quit requested");
        s.server.lock().unwrap().shutdown();
        *control_flow = ControlFlow::Exit;
        return;
    }
    if id == ids.sync_now {
        // Fire-and-forget: trigger every enabled source.
        for src in s.cfg.enabled_sources.clone() {
            kick_sync_source(s, src);
        }
    } else if id == ids.ingest_now {
        kick_ingest(s);
    } else if id == ids.pause {
        let mut st = s.state.lock().unwrap();
        st.paused = !st.paused;
    } else if id == ids.pause_schedule {
        let mut st = s.state.lock().unwrap();
        st.paused_schedule = !st.paused_schedule;
    } else if id == ids.open_web {
        // Try to bring the server up first if it's down.
        let mut srv = s.server.lock().unwrap();
        let _ = srv.ensure_running(&s.client);
        drop(srv);
        let _ = server::url_in_browser(&s.cfg.web_base());
    } else if id == ids.autostart {
        if let Ok(au) = autostart::Autostart::new() {
            let now = !au.enabled();
            if let Err(e) = au.set(now) {
                log::warn!("autostart toggle failed: {}", e);
            } else {
                s.state.lock().unwrap().autostart = now;
            }
        }
    } else if id == ids.status || id == ids.last {
        // info-only items — disabled, but tray-icon may still emit events.
    }
    refresh_items(&s.items, &s.state.lock().unwrap());
}

fn kick_sync_source(s: &AppLoopShared, src: Source) {
    // Layer in `--enrich` / `--enrich-meta` per `[manager.sync]` in
    // config.toml so the manager's "Sync now" + scheduled syncs include
    // whichever enrichment passes the user wants.
    let mut args = vec!["--source".into(), src.name().into()];
    args.extend(s.cfg.sync.cli_args());
    kick_job(s, "sync".into(), src.name().into(), args);
}

fn kick_ingest(s: &AppLoopShared) {
    // Plain incremental ingest. `--reset` stays a manual CLI thing; the
    // scheduled ingest just keeps the index up to date.
    kick_job(s, "ingest".into(), "ingest".into(), vec![]);
}

/// Generic per-job runner: ensures the server is up, submits via
/// /api/jobs/run, polls until terminal, emits started/finished events.
fn kick_job(s: &AppLoopShared, kind: String, label: String, args: Vec<String>) {
    let app_tx = s.app_tx.clone();
    let client = s.client.clone();
    let server = s.server.clone();
    let kind_for_thread = kind.clone();
    let label_for_thread = label.clone();

    std::thread::Builder::new()
        .name(format!("{}-{}", kind, label))
        .spawn(move || {
            if let Err(e) = server.lock().unwrap().ensure_running(&client) {
                let _ = app_tx.send(AppEvent::JobFinished {
                    kind: kind_for_thread.clone(),
                    label: label_for_thread.clone(),
                    success: false,
                    message: format!("server unavailable: {}", e),
                });
                return;
            }

            let _ = app_tx.send(AppEvent::JobStarted {
                kind: kind_for_thread.clone(),
                label: label_for_thread.clone(),
            });

            let job_id = match client.submit_job(&kind_for_thread, &args) {
                Ok(id) => id,
                Err(e) => {
                    let _ = app_tx.send(AppEvent::JobFinished {
                        kind: kind_for_thread,
                        label: label_for_thread,
                        success: false,
                        message: format!("submit failed: {}", e),
                    });
                    return;
                }
            };

            // 5-minute budget per job — covers slow LLM enrichers and
            // ingest re-embeddings.
            let result = client.await_job(&job_id, Duration::from_secs(300));
            let (ok, msg) = match result {
                Ok(st) => {
                    let ok = st.status == "success";
                    let msg = if ok { "ok".to_string() }
                              else { first_line(&st.error).unwrap_or_else(|| "failed".into()) };
                    (ok, msg)
                }
                Err(e) => (false, e.to_string()),
            };
            let _ = app_tx.send(AppEvent::JobFinished {
                kind: kind_for_thread,
                label: label_for_thread,
                success: ok,
                message: msg,
            });
        })
        .expect("spawn job thread");
}

fn refresh_items(items: &ItemsView, st: &AppState) {
    let status_label = match st.status {
        Status::Idle       => "Status: idle",
        Status::Syncing    => "Status: syncing…",
        Status::ServerDown => "Status: server not running",
        Status::Error      => "Status: error",
    };
    items.status.set_text(status_label);

    let last_label = match &st.last {
        None => "No syncs yet".to_string(),
        Some(l) => {
            let tag = if l.success { "ok" } else { "failed" };
            format!("Last: {} · {} · {}", l.source, tag, l.when)
        }
    };
    items.last.set_text(&last_label);

    let sched_label = if st.schedule_summary.is_empty() {
        "Schedule: off".to_string()
    } else {
        format!("Schedule: {}", st.schedule_summary)
    };
    items.schedule_info.set_text(&sched_label);

    items.pause.set_text(if st.paused { "Resume watching" } else { "Pause watching" });
    items.pause_schedule.set_text(if st.paused_schedule {
        "Resume scheduled jobs"
    } else {
        "Pause scheduled jobs"
    });
    items.autostart.set_text(if st.autostart { "Launch at login ✓" } else { "Launch at login" });

    let color = booki_manager::menu::LedColor::from_state(st);
    let _ = items.icon.set_icon(Some(booki_manager::menu::led_icon(color)));
}

fn first_line(s: &str) -> Option<String> {
    s.lines().next().map(|l| l.trim().to_string()).filter(|l| !l.is_empty())
}

fn now_local() -> String {
    // Avoid pulling in chrono just for a timestamp string.
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // HH:MM in the system's local-ish view: we render UTC for portability;
    // refining to local-tz is a follow-up.
    let h = (secs / 3600) % 24;
    let m = (secs / 60) % 60;
    format!("{:02}:{:02}Z", h, m)
}
