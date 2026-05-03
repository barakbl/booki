use anyhow::{Context, Result};
use auto_launch::AutoLaunchBuilder;

pub struct Autostart {
    inner: auto_launch::AutoLaunch,
}

impl Autostart {
    pub fn new() -> Result<Self> {
        let exe = std::env::current_exe().context("current_exe")?;
        let inner = AutoLaunchBuilder::new()
            .set_app_name("Booki Manager")
            .set_app_path(&exe.to_string_lossy())
            .set_use_launch_agent(true)
            .build()
            .context("build auto-launch")?;
        Ok(Self { inner })
    }

    pub fn enabled(&self) -> bool {
        self.inner.is_enabled().unwrap_or(false)
    }

    pub fn set(&self, on: bool) -> Result<()> {
        if on { self.inner.enable()?; } else { self.inner.disable()?; }
        Ok(())
    }
}
