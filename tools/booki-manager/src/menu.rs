use crate::state::{AppState, Status};
use anyhow::Result;
use tray_icon::menu::{Menu, MenuId, MenuItem, PredefinedMenuItem, Submenu};
use tray_icon::{TrayIcon, TrayIconBuilder};

/// Three-state LED color used as the menubar icon.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LedColor {
    Green,   // server up + watching
    Orange,  // server up but paused
    Red,     // server down or last sync errored
}

impl LedColor {
    pub fn from_state(st: &AppState) -> Self {
        match st.status {
            Status::ServerDown | Status::Error => LedColor::Red,
            Status::Idle | Status::Syncing => {
                if st.paused { LedColor::Orange } else { LedColor::Green }
            }
        }
    }

    fn rgb(self) -> (u8, u8, u8) {
        match self {
            // Slightly muted so the LED reads as a glyph, not a fluorescent dot.
            LedColor::Green  => (0x2e, 0xc4, 0x6b),
            LedColor::Orange => (0xf5, 0xa6, 0x23),
            LedColor::Red    => (0xe5, 0x3a, 0x3a),
        }
    }
}

pub struct Items {
    pub status: MenuItem,
    pub last: MenuItem,
    pub schedule_info: MenuItem,
    pub booki_info: MenuItem,
    pub sync_now: MenuItem,
    pub ingest_now: MenuItem,
    pub pause: MenuItem,
    pub pause_schedule: MenuItem,
    /// "Open Booki web UI" — lives inside the Web interface submenu now.
    pub open_web: MenuItem,
    /// Web-interface submenu entries — each separately addressable so
    /// MenuEvent fires its dedicated id.
    pub web_start: MenuItem,
    pub web_stop: MenuItem,
    pub web_restart: MenuItem,
    pub pick_booki: MenuItem,
    pub autostart: MenuItem,
    pub quit: MenuItem,
}

pub struct Tray {
    pub icon: TrayIcon,
    pub items: Items,
}

pub fn build() -> Result<Tray> {
    let menu = Menu::new();

    let status = MenuItem::with_id(MenuId::new("status"), "Status: idle", false, None);
    let last = MenuItem::with_id(MenuId::new("last"), "No syncs yet", false, None);
    let schedule_info = MenuItem::with_id(MenuId::new("schedule_info"), "Schedule: off", false, None);
    let booki_info = MenuItem::with_id(MenuId::new("booki_info"), "Booki: ?", false, None);
    let sync_now = MenuItem::with_id(MenuId::new("sync_now"), "Sync now", true, None);
    let ingest_now = MenuItem::with_id(MenuId::new("ingest_now"), "Ingest now", true, None);
    let pause = MenuItem::with_id(MenuId::new("pause"), "Pause watching", true, None);
    let pause_schedule = MenuItem::with_id(MenuId::new("pause_schedule"), "Pause scheduled jobs", true, None);

    // Web-interface submenu — Open + Start / Stop / Restart for the Python
    // server. Each item carries its own id; refresh() flips Start/Stop
    // disabled state based on whether the server is currently running.
    let open_web = MenuItem::with_id(MenuId::new("open_web"), "Open Booki web UI", true, None);
    let web_start = MenuItem::with_id(MenuId::new("web_start"), "Start", true, None);
    let web_stop = MenuItem::with_id(MenuId::new("web_stop"), "Stop", true, None);
    let web_restart = MenuItem::with_id(MenuId::new("web_restart"), "Restart", true, None);
    let web_submenu = Submenu::new("Web interface", true);
    web_submenu.append(&open_web)?;
    web_submenu.append(&PredefinedMenuItem::separator())?;
    web_submenu.append(&web_start)?;
    web_submenu.append(&web_stop)?;
    web_submenu.append(&web_restart)?;

    let pick_booki = MenuItem::with_id(MenuId::new("pick_booki"), "Pick Booki folder…", true, None);
    let autostart = MenuItem::with_id(MenuId::new("autostart"), "Launch at login", true, None);
    let quit = MenuItem::with_id(MenuId::new("quit"), "Quit Booki Manager", true, None);

    menu.append(&status)?;
    menu.append(&last)?;
    menu.append(&schedule_info)?;
    menu.append(&booki_info)?;
    menu.append(&PredefinedMenuItem::separator())?;
    menu.append(&sync_now)?;
    menu.append(&ingest_now)?;
    menu.append(&pause)?;
    menu.append(&pause_schedule)?;
    menu.append(&PredefinedMenuItem::separator())?;
    menu.append(&web_submenu)?;
    menu.append(&PredefinedMenuItem::separator())?;
    menu.append(&pick_booki)?;
    menu.append(&autostart)?;
    menu.append(&PredefinedMenuItem::separator())?;
    menu.append(&quit)?;

    let icon = TrayIconBuilder::new()
        .with_menu(Box::new(menu))
        .with_tooltip("Booki — bookmark sync")
        .with_icon(led_icon(LedColor::Green))
        .build()?;

    Ok(Tray {
        icon,
        items: Items {
            status, last, schedule_info, booki_info,
            sync_now, ingest_now,
            pause, pause_schedule,
            open_web, web_start, web_stop, web_restart,
            pick_booki, autostart, quit,
        },
    })
}

/// Refresh the labels on the info-only items + the Pause / Launch toggles.
pub fn refresh(items: &Items, st: &AppState) {
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

    items.pause.set_text(if st.paused { "Resume watching" } else { "Pause watching" });
    items.pause_schedule.set_text(if st.paused_schedule {
        "Resume scheduled jobs"
    } else {
        "Pause scheduled jobs"
    });
    items.autostart.set_text(if st.autostart { "Launch at login ✓" } else { "Launch at login" });

    let sched_label = if st.schedule_summary.is_empty() {
        "Schedule: off".to_string()
    } else {
        format!("Schedule: {}", st.schedule_summary)
    };
    items.schedule_info.set_text(&sched_label);

    // Show the basename of the active Booki path (full path is too wide
    // for a tray menu item). Tooltip / hover would be nicer but tray-icon
    // doesn't expose that for menu items.
    let booki_label = if st.booki_home.as_os_str().is_empty() {
        "Booki: not set".to_string()
    } else {
        let basename = st.booki_home.file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| st.booki_home.to_string_lossy().into_owned());
        format!("Booki: {}", basename)
    };
    items.booki_info.set_text(&booki_label);

    // Web-interface submenu: only enable the action that makes sense for
    // the current server state.
    let server_up = matches!(st.status, Status::Idle | Status::Syncing);
    items.web_start.set_enabled(!server_up);
    items.web_stop.set_enabled(server_up);
    items.web_restart.set_enabled(server_up);
}

/// Render a small filled-circle "LED" in the requested color. Anti-aliased
/// at the edge by ramping alpha across a 1px band, plus a faint highlight
/// on the upper-left so it reads as a 3D dot at menubar size.
pub fn led_icon(color: LedColor) -> tray_icon::Icon {
    let size: u32 = 22;
    let mut rgba = vec![0u8; (size * size * 4) as usize];

    let cx = (size as f32 - 1.0) / 2.0;
    let cy = (size as f32 - 1.0) / 2.0;
    // Slight inset so the LED has breathing room from the menubar text.
    let r  = (size as f32) * 0.40;
    let edge = 1.0; // antialiased band width in pixels
    let (r8, g8, b8) = color.rgb();

    for y in 0..size {
        for x in 0..size {
            let dx = x as f32 - cx;
            let dy = y as f32 - cy;
            let d  = (dx * dx + dy * dy).sqrt();
            // Coverage: 1 inside, 0 outside, linear ramp across `edge`.
            let cov = ((r - d) / edge).clamp(0.0, 1.0);
            if cov <= 0.0 { continue; }

            // Subtle highlight: brighten the upper-left arc by ~25%.
            let hi = (-dx - dy) / (r * 1.4);  // ranges roughly -1..1
            let bias = (hi.clamp(-0.5, 1.0) * 0.25).max(0.0);
            let lift = |c: u8| {
                let v = c as f32 + (255.0 - c as f32) * bias;
                v.clamp(0.0, 255.0) as u8
            };

            let i = ((y * size + x) * 4) as usize;
            rgba[i]     = lift(r8);
            rgba[i + 1] = lift(g8);
            rgba[i + 2] = lift(b8);
            rgba[i + 3] = (cov * 255.0) as u8;
        }
    }

    tray_icon::Icon::from_rgba(rgba, size, size)
        .expect("static icon data is well-formed")
}
