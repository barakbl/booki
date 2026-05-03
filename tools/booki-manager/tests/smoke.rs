//! Smoke test: load the real ../../config.toml and confirm the parser
//! agrees with what's there. Doesn't touch any sockets or processes.

use std::path::PathBuf;

#[test]
fn parses_real_config_and_finds_browser_paths() {
    // Walk up from the cargo manifest dir to the project root.
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let root = manifest
        .ancestors()
        .find(|p| p.join("config.toml").exists())
        .expect("could not find a project root with config.toml")
        .to_path_buf();
    let cfg_path = root.join("config.toml");

    use booki_manager::config::AppConfig;
    use booki_manager::paths;

    let cfg = AppConfig::load(&cfg_path).expect("load config.toml");
    eprintln!("config: web_base = {}", cfg.web_base());
    let names: Vec<&str> = cfg.enabled_sources.iter().map(|s| s.name()).collect();
    eprintln!("enabled sources = {:?}", names);

    // The example config enables chrome/safari/firefox by virtue of having
    // their tables present and not `disabled = true`. So at minimum we
    // expect at least one enabled browser source unless the user disabled
    // them all (unlikely in a default checkout).
    assert!(
        !cfg.enabled_sources.is_empty(),
        "no browser sources enabled — did config.toml disable all of them?"
    );

    // For each enabled source, verify watch_targets returns *something*
    // when the corresponding browser exists on this machine. We only
    // assert the function doesn't panic — empty vec is valid (browser
    // not installed).
    for src in cfg.enabled_sources {
        let dirs = paths::watch_targets(src);
        eprintln!("{}: {} dir(s) found", src.name(), dirs.len());
        for d in &dirs {
            eprintln!("  - {}", d.display());
        }
    }
}
