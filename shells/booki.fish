# Booki shell integration — fish
#
# Source it from ~/.config/fish/config.fish, e.g.:
#
#     set -gx BOOKI_HOME ~/dev/booki
#     source $BOOKI_HOME/shells/booki.fish
#
# It puts the `booki` script on PATH and registers subcommand + flag completions.

set -l booki_home (status dirname)/..

# Resolve to an absolute path so PATH stays valid wherever fish is launched from.
set -l booki_home (realpath $booki_home 2>/dev/null; or echo $booki_home)

if test -x $booki_home/booki
    if not contains $booki_home $PATH
        set -gx PATH $booki_home $PATH
    end
end

# ── Completions ──────────────────────────────────────────────────────────────

set -l __booki_subs sync ingest chat web browse download help

function __booki_needs_subcommand
    set -l tokens (commandline -opc)
    test (count $tokens) -le 1
end

function __booki_using_sub
    set -l tokens (commandline -opc)
    test (count $tokens) -ge 2; and test "$tokens[2]" = "$argv[1]"
end

# Top-level subcommands (only suggested when nothing follows `booki`).
complete -c booki -n __booki_needs_subcommand -f -a sync     -d 'Sync sources → markdown files'
complete -c booki -n __booki_needs_subcommand -f -a ingest   -d 'Index markdown into the vector DB'
complete -c booki -n __booki_needs_subcommand -f -a chat     -d 'Semantic search + LLM answer'
complete -c booki -n __booki_needs_subcommand -f -a web      -d 'Run the FastAPI web UI'
complete -c booki -n __booki_needs_subcommand -f -a browse   -d 'Open the fzf TUI browser'
complete -c booki -n __booki_needs_subcommand -f -a download -d 'Download a video / audio with yt-dlp'
complete -c booki -n __booki_needs_subcommand -f -a doctor    -d 'Visual health check — what works, what to run next'
complete -c booki -n __booki_needs_subcommand -f -a bootstrap -d 'Interactive config.toml wizard'
complete -c booki -n __booki_needs_subcommand -f -a help      -d 'Show top-level usage'

# ── booki sync ────────────────────────────────────────────────────────────────
complete -c booki -n '__booki_using_sub sync' -l link          -d 'Add a single link by URL' -r
complete -c booki -n '__booki_using_sub sync' -l link-title    -d 'Override the title for --link' -r
complete -c booki -n '__booki_using_sub sync' -l source        -d 'Limit to one or more sources' -r
complete -c booki -n '__booki_using_sub sync' -l list-sources  -d 'List registered sources + availability'
complete -c booki -n '__booki_using_sub sync' -l check-dead-links -d 'Check unchecked URLs (HTTP)'
complete -c booki -n '__booki_using_sub sync' -l enrich        -d 'Fetch + LLM-summarize new items'
complete -c booki -n '__booki_using_sub sync' -l enrich-meta   -d 'Run enricher plugins (github, youtube, …)'
complete -c booki -n '__booki_using_sub sync' -l enricher      -d 'Limit --enrich-meta to specific enrichers' -r -a 'github youtube'
complete -c booki -n '__booki_using_sub sync' -l list-enrichers -d 'List all registered enricher plugins'
complete -c booki -n '__booki_using_sub sync' -l all           -d 'Re-process every item, not just new ones'
complete -c booki -n '__booki_using_sub sync' -l no-sync       -d 'Skip sync; only enrich / check links'
complete -c booki -n '__booki_using_sub sync' -l dry-run       -d 'Preview without writing'

# ── booki ingest ──────────────────────────────────────────────────────────────
complete -c booki -n '__booki_using_sub ingest' -l reset  -d 'Wipe and re-index from scratch'
complete -c booki -n '__booki_using_sub ingest' -l config -d 'Alternate config.toml' -r

# ── booki chat ────────────────────────────────────────────────────────────────
complete -c booki -n '__booki_using_sub chat' -l no-llm         -d 'Show results only, skip LLM'
complete -c booki -n '__booki_using_sub chat' -l n              -d 'Number of results to retrieve' -r
complete -c booki -n '__booki_using_sub chat' -l min-importance -d 'Filter by importance ≥ N' -r
complete -c booki -n '__booki_using_sub chat' -l config         -d 'Alternate config.toml' -r

# ── booki web ─────────────────────────────────────────────────────────────────
complete -c booki -n '__booki_using_sub web' -l host   -d 'Bind host'   -r
complete -c booki -n '__booki_using_sub web' -l port   -d 'Bind port'   -r
complete -c booki -n '__booki_using_sub web' -l reload -d 'uvicorn auto-reload (dev)'
complete -c booki -n '__booki_using_sub web' -l config -d 'Alternate config.toml' -r

# ── booki download ────────────────────────────────────────────────────────────
complete -c booki -n '__booki_using_sub download' -l audio  -d 'Audio-only (mp3)'
complete -c booki -n '__booki_using_sub download' -l config -d 'Alternate config.toml' -r

# ── booki doctor ──────────────────────────────────────────────────────────────
complete -c booki -n '__booki_using_sub doctor' -l no-color -d 'Plain output (no ANSI)'
complete -c booki -n '__booki_using_sub doctor' -l config   -d 'Alternate config.toml' -r

# ── booki bootstrap ───────────────────────────────────────────────────────────
complete -c booki -n '__booki_using_sub bootstrap' -l no-color    -d 'Plain output (no ANSI)'
complete -c booki -n '__booki_using_sub bootstrap' -l output -s o -d 'Output path (must not exist)' -r
