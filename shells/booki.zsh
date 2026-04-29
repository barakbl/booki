# Booki shell integration — zsh
#
# Source it from ~/.zshrc, e.g.:
#
#     export BOOKI_HOME=~/dev/booki
#     source $BOOKI_HOME/shells/booki.zsh
#
# It puts the `booki` script on PATH and registers a `_booki` completion.

# Resolve the directory containing this script even when sourced.
() {
  local self=${(%):-%N}
  local booki_home=${self:A:h:h}     # <booki_home>/shells/booki.zsh -> <booki_home>

  if [[ -x "$booki_home/booki" ]]; then
    case ":$PATH:" in
      *":$booki_home:"*) ;;
      *) export PATH="$booki_home:$PATH" ;;
    esac
  fi
}

# ── Completions ──────────────────────────────────────────────────────────────

# Make sure compinit has been initialised. If the user already runs compinit
# elsewhere (oh-my-zsh, prezto, …), this is a cheap no-op; otherwise it bootstraps.
autoload -Uz compinit && compinit -u 2>/dev/null

_booki() {
  local -a subcommands
  subcommands=(
    'sync:Sync sources → markdown files'
    'ingest:Index markdown into the vector DB'
    'chat:Semantic search + LLM answer'
    'web:Run the FastAPI web UI'
    'browse:Open the fzf TUI browser'
    'download:Download a video / audio with yt-dlp'
    'doctor:Visual health check — what works, what to run next'
    'bootstrap:Interactive config.toml wizard'
    'help:Show top-level usage'
  )

  local context state state_descr line
  typeset -A opt_args

  _arguments -C \
    '1: :->sub' \
    '*::arg:->args'

  case $state in
    sub)
      _describe -t commands 'booki subcommand' subcommands
      ;;
    args)
      case $line[1] in
        sync)
          _arguments \
            '--link[Add a single link by URL]:url:' \
            '--link-title[Override the title for --link]:title:' \
            '--source[Limit to one or more sources]:source:' \
            '--list-sources[List registered sources]' \
            '--check-dead-links[Check unchecked URLs]' \
            '--enrich[LLM-summarize new items]' \
            '--enrich-meta[Run enricher plugins (github, youtube, …)]' \
            '*--enricher[Limit --enrich-meta to specific enrichers]:enricher:(github youtube)' \
            '--list-enrichers[List all registered enricher plugins]' \
            '--all[Re-process everything, not just new]' \
            '--no-sync[Skip sync; only enrich / check links]' \
            '--dry-run[Preview without writing]'
          ;;
        ingest)
          _arguments \
            '--reset[Wipe and re-index from scratch]' \
            '--config[Alternate config.toml]:file:_files'
          ;;
        chat)
          _arguments \
            '--no-llm[Show results only, skip LLM]' \
            '--n[Number of results to retrieve]:n:' \
            '--min-importance[Filter by importance ≥ N]:n:' \
            '--config[Alternate config.toml]:file:_files' \
            '*:query:'
          ;;
        web)
          _arguments \
            '--host[Bind host]:host:' \
            '--port[Bind port]:port:' \
            '--reload[uvicorn auto-reload (dev)]' \
            '--config[Alternate config.toml]:file:_files'
          ;;
        download)
          _arguments \
            '--audio[Audio-only (mp3)]' \
            '--config[Alternate config.toml]:file:_files' \
            '*:url:'
          ;;
        doctor)
          _arguments \
            '--no-color[Plain output (no ANSI)]' \
            '--config[Alternate config.toml]:file:_files'
          ;;
        bootstrap)
          _arguments \
            '--no-color[Plain output (no ANSI)]' \
            '(-o --output)'{-o,--output}'[Output path (must not exist)]:file:_files'
          ;;
      esac
      ;;
  esac
}

compdef _booki booki
