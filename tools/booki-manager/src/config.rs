use crate::paths::Source;
use anyhow::{Context, Result};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Debug, Deserialize, Default)]
struct Root {
    #[serde(default)]
    web: Web,
    #[serde(default)]
    sources: BTreeMap<String, SourceCfg>,
    #[serde(default)]
    manager: ManagerCfg,
}

#[derive(Debug, Deserialize, Default)]
struct ManagerCfg {
    #[serde(default)]
    sync: SyncOptsCfg,
}

#[derive(Debug, Deserialize)]
struct SyncOptsCfg {
    /// `enrich` / `enrich-meta` are both true by default. The dash form
    /// matches Booki's CLI flags; the underscore form is TOML-idiomatic.
    /// Either spelling works in config.toml.
    #[serde(default = "default_true")]
    enrich: bool,
    #[serde(default = "default_true", alias = "enrich-meta")]
    enrich_meta: bool,
}

impl Default for SyncOptsCfg {
    fn default() -> Self {
        Self { enrich: true, enrich_meta: true }
    }
}

fn default_true() -> bool { true }

#[derive(Debug, Deserialize)]
struct Web {
    #[serde(default = "default_host")]
    host: String,
    #[serde(default = "default_port")]
    port: u16,
}

impl Default for Web {
    fn default() -> Self {
        Self { host: default_host(), port: default_port() }
    }
}

fn default_host() -> String { "127.0.0.1".to_string() }
fn default_port() -> u16 { 8765 }

#[derive(Debug, Deserialize, Default)]
struct SourceCfg {
    #[serde(default)]
    disabled: bool,
}

/// What `booki-manager` cares about in `config.toml` — everything else passes
/// through to the Python side untouched.
#[derive(Debug, Clone)]
pub struct AppConfig {
    pub host: String,
    pub port: u16,
    pub enabled_sources: Vec<Source>,
    pub sync: SyncOpts,
}

/// Flags the manager appends to every `booki sync` invocation it triggers
/// (manual "Sync now" + the periodic schedule). `[manager.sync]` in
/// config.toml — both default to true so out-of-the-box the manager keeps
/// summaries and plugin-enrichers fresh.
#[derive(Debug, Clone, Copy)]
pub struct SyncOpts {
    pub enrich: bool,
    pub enrich_meta: bool,
}

impl Default for SyncOpts {
    fn default() -> Self {
        Self { enrich: true, enrich_meta: true }
    }
}

impl SyncOpts {
    /// CLI flags for `booki sync`. Empty when both options are off.
    pub fn cli_args(&self) -> Vec<String> {
        let mut v = Vec::with_capacity(2);
        if self.enrich      { v.push("--enrich".into()); }
        if self.enrich_meta { v.push("--enrich-meta".into()); }
        v
    }
}

impl AppConfig {
    pub fn web_base(&self) -> String {
        // Bind host of "0.0.0.0" still means "talk to it on localhost".
        let h = if self.host == "0.0.0.0" { "127.0.0.1" } else { &self.host };
        format!("http://{}:{}", h, self.port)
    }

    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("read {}", path.display()))?;
        let parsed: Root = toml::from_str(&text).context("parse config.toml")?;

        let mut sources = vec![];
        for (name, cfg) in &parsed.sources {
            if cfg.disabled { continue; }
            let src = match name.as_str() {
                "chrome"  => Some(Source::Chrome),
                "safari"  => Some(Source::Safari),
                "firefox" => Some(Source::Firefox),
                // rss, youtube, directory aren't browser-bookmark sources —
                // they pull from network/disk and don't need file watching.
                _ => None,
            };
            if let Some(s) = src { sources.push(s); }
        }

        Ok(Self {
            host: parsed.web.host,
            port: parsed.web.port,
            enabled_sources: sources,
            sync: SyncOpts {
                enrich:      parsed.manager.sync.enrich,
                enrich_meta: parsed.manager.sync.enrich_meta,
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    static SEQ: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

    fn load_inline(toml_text: &str) -> AppConfig {
        // Avoid pulling in `tempfile` as a dev-dep — a process-unique seq +
        // pid keeps parallel tests from clobbering each other's files.
        let id = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let p = std::env::temp_dir().join(format!(
            "booki-manager-cfg-{}-{}.toml",
            std::process::id(),
            id,
        ));
        std::fs::write(&p, toml_text).unwrap();
        let cfg = AppConfig::load(&p).expect("load");
        let _ = std::fs::remove_file(&p);
        cfg
    }

    #[test]
    fn sync_opts_default_to_true_when_section_missing() {
        let cfg = load_inline("[web]\nport = 8765\n");
        assert!(cfg.sync.enrich);
        assert!(cfg.sync.enrich_meta);
        assert_eq!(cfg.sync.cli_args(), vec!["--enrich", "--enrich-meta"]);
    }

    #[test]
    fn sync_opts_accept_dash_and_underscore_alias() {
        // dash form
        let cfg = load_inline(
            "[manager.sync]\nenrich = false\nenrich-meta = false\n",
        );
        assert!(!cfg.sync.enrich);
        assert!(!cfg.sync.enrich_meta);
        assert!(cfg.sync.cli_args().is_empty());

        // underscore form
        let cfg = load_inline(
            "[manager.sync]\nenrich = false\nenrich_meta = true\n",
        );
        assert!(!cfg.sync.enrich);
        assert!(cfg.sync.enrich_meta);
        assert_eq!(cfg.sync.cli_args(), vec!["--enrich-meta"]);
    }

    #[test]
    fn partial_sync_opts_keeps_unset_field_at_default() {
        let cfg = load_inline("[manager.sync]\nenrich = false\n");
        assert!(!cfg.sync.enrich);
        assert!(cfg.sync.enrich_meta, "enrich_meta should default to true");
    }
}
