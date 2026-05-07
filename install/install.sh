#!/bin/sh
# install.sh — Booki one-liner installer.
#
# Usage:
#   curl -sSfL https://raw.githubusercontent.com/barakbl/booki/main/install/install.sh | sh
#
# Goals:
#   - Idempotent: re-running pulls the latest code, refreshes the venv,
#     and re-applies shell snippets without duplicating lines.
#   - XDG-compliant: code under $XDG_DATA_HOME/booki, config under
#     $XDG_CONFIG_HOME/booki, wrapper under $XDG_BIN_HOME (or PATH-y
#     equivalent for whichever shell you run).
#   - Self-contained: creates its own virtualenv, never touches system Python.
#   - Friendly: detects fish / zsh / bash and adds the right snippet to
#     the right rc file. Suggests brew / apt / dnf / pacman commands for
#     optional binaries (ffmpeg, fzf, ollama, …) at the end.
#
# Heads-up: this is the *less recommended* install path. Booki is a small,
# editable, hackable project — you're meant to read and tweak it. The
# `git clone + cd booki` flow gives you the working tree this project is
# designed around. The installer is here for users who just want to try
# Booki without the venv ceremony.

set -e

# ─── parameters ─────────────────────────────────────────────────────────────

REPO=${BOOKI_REPO:-https://github.com/barakbl/booki.git}
BRANCH=${BOOKI_BRANCH:-main}

XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
XDG_BIN_HOME=${XDG_BIN_HOME:-$HOME/.local/bin}

BOOKI_HOME=$XDG_DATA_HOME/booki
VENV=$BOOKI_HOME/.venv

# ─── pretty output ──────────────────────────────────────────────────────────

if [ -t 1 ]; then
    NC=$(printf '\033[0m'); BOLD=$(printf '\033[1m')
    G=$(printf '\033[32m'); Y=$(printf '\033[33m')
    R=$(printf '\033[31m'); B=$(printf '\033[36m')
else
    NC=""; BOLD=""; G=""; Y=""; R=""; B=""
fi

say()  { printf '\n%s▸ %s%s\n'  "$B"   "$1" "$NC"; }
ok()   { printf '%s  ✓%s %s\n'  "$G"   "$NC" "$1"; }
warn() { printf '%s  ⚠%s %s\n'  "$Y"   "$NC" "$1"; }
err()  { printf '%s  ✗%s %s\n'  "$R"   "$NC" "$1" >&2; }
die()  { err "$1"; exit 1; }

# ─── prereqs ────────────────────────────────────────────────────────────────

need() {
    if ! command -v "$1" >/dev/null 2>&1; then
        die "missing required tool: $1 (install it and re-run)"
    fi
}

say "Checking prerequisites"
need git
need python3
need curl

PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PY_VERSION" in
    3.10|3.11|3.12|3.13|3.14|3.15) ok "python $PY_VERSION" ;;
    *) die "python ≥ 3.10 required (found $PY_VERSION)" ;;
esac

mkdir -p "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" "$XDG_BIN_HOME"

# ─── clone or update ────────────────────────────────────────────────────────

say "Source tree at $BOOKI_HOME"
if [ -d "$BOOKI_HOME/.git" ]; then
    git -C "$BOOKI_HOME" fetch --quiet origin "$BRANCH"
    # Refuse to reset --hard when the working tree has local edits or
    # commits ahead of origin. The original installer silently
    # discarded both, which surprised users who follow the README's
    # "read and tweak it" advice. (P5-05)
    DIRTY=""
    if ! git -C "$BOOKI_HOME" diff --quiet 2>/dev/null \
       || ! git -C "$BOOKI_HOME" diff --cached --quiet 2>/dev/null; then
        DIRTY="uncommitted changes"
    fi
    AHEAD=$(git -C "$BOOKI_HOME" rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)
    if [ "$AHEAD" != "0" ] && [ -n "$AHEAD" ]; then
        DIRTY="${DIRTY:+$DIRTY + }$AHEAD local commit(s) ahead of origin/$BRANCH"
    fi
    if [ -n "$DIRTY" ]; then
        warn "Local checkout has $DIRTY — refusing to reset --hard."
        warn "  stash or push your work, then re-run this installer."
        exit 1
    fi
    # If $BOOKI_TAG is set, prefer the signed tag over the branch tip.
    # Falls back to the branch tip when the tag isn't reachable. (P5-04)
    if [ -n "${BOOKI_TAG:-}" ]; then
        if git -C "$BOOKI_HOME" tag -v "$BOOKI_TAG" 2>/dev/null \
            | grep -q "Good signature"; then
            ok "verified signed tag $BOOKI_TAG"
        else
            warn "BOOKI_TAG=$BOOKI_TAG not signature-verifiable on this host."
        fi
        git -C "$BOOKI_HOME" reset --hard --quiet "$BOOKI_TAG"
    else
        git -C "$BOOKI_HOME" reset --hard --quiet "origin/$BRANCH"
    fi
    ok "updated to origin/$BRANCH ($(git -C "$BOOKI_HOME" rev-parse --short HEAD))"
elif [ -e "$BOOKI_HOME" ]; then
    die "$BOOKI_HOME exists but is not a git checkout. Move it aside or delete it."
else
    git clone --quiet --branch "$BRANCH" "$REPO" "$BOOKI_HOME"
    ok "cloned $REPO → $BOOKI_HOME"
fi

# ─── venv + dependencies ────────────────────────────────────────────────────

say "Python virtualenv at $VENV"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
    ok "created"
else
    ok "reused existing"
fi

"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$BOOKI_HOME/requirements.txt"
ok "dependencies up to date"

# ─── config ─────────────────────────────────────────────────────────────────
#
# The installer no longer drops a `config.toml` in place. `booki bootstrap`
# is the supported way to author one — it asks about sources, embeddings,
# LLM provider, and the menubar manager, and writes a config tailored to
# the answers. The `config.toml.example` in the checkout stays as the
# documented reference.

say "Configuration"
if [ -e "$BOOKI_HOME/config.toml" ]; then
    ok "kept existing $BOOKI_HOME/config.toml"
else
    ok "no config written — run 'booki bootstrap' to generate one"
fi

# ─── wrapper ────────────────────────────────────────────────────────────────

say "Dispatcher wrapper"
WRAPPER=$XDG_BIN_HOME/booki
cat > "$WRAPPER" <<EOF
#!/bin/sh
# Booki dispatcher — generated by install.sh; safe to overwrite by re-running.
# Runs the venv-bound python on the in-tree dispatcher script so the
# system Python is never touched.
exec "$VENV/bin/python" "$BOOKI_HOME/booki" "\$@"
EOF
chmod +x "$WRAPPER"
ok "wrote $WRAPPER"

# ─── shell integration (idempotent) ─────────────────────────────────────────

# Append `line` to `file` only if it isn't already a literal line in the file.
idem_append() {
    file=$1; line=$2; tag=$3
    mkdir -p "$(dirname "$file")"
    [ -f "$file" ] || : > "$file"
    if grep -F -x -- "$line" "$file" >/dev/null 2>&1; then
        ok "$file already has $tag"
    else
        printf '\n# Added by booki installer\n%s\n' "$line" >> "$file"
        ok "added $tag to $file"
    fi
}

detect_shell() {
    case "${SHELL:-}" in
        */fish) echo fish ;;
        */zsh)  echo zsh ;;
        */bash) echo bash ;;
        *)      echo "" ;;
    esac
}

say "Shell integration"
SH=$(detect_shell)
case "$SH" in
    fish)
        FISH_RC=$XDG_CONFIG_HOME/fish/config.fish
        idem_append "$FISH_RC" "fish_add_path -g $XDG_BIN_HOME" "PATH"
        if [ -f "$BOOKI_HOME/shells/booki.fish" ]; then
            idem_append "$FISH_RC" "source $BOOKI_HOME/shells/booki.fish" "completions"
        fi
        ;;
    zsh)
        ZRC=$HOME/.zshrc
        idem_append "$ZRC" "export PATH=\"$XDG_BIN_HOME:\$PATH\"" "PATH"
        if [ -f "$BOOKI_HOME/shells/booki.zsh" ]; then
            idem_append "$ZRC" "source $BOOKI_HOME/shells/booki.zsh" "completions"
        fi
        ;;
    bash)
        BRC=$HOME/.bashrc
        case "$(uname -s)" in Darwin) BRC=$HOME/.bash_profile ;; esac
        idem_append "$BRC" "export PATH=\"$XDG_BIN_HOME:\$PATH\"" "PATH"
        ;;
    *)
        warn "Could not detect your shell. Add $XDG_BIN_HOME to PATH manually."
        ;;
esac

# ─── optional binaries ──────────────────────────────────────────────────────

say "Optional dependencies"
suggest() {
    pkg=$1; mac=$2; deb=$3; arch=$4; rh=$5
    if command -v "$pkg" >/dev/null 2>&1; then
        ok "$pkg already installed"
        return
    fi
    case "$(uname -s)" in
        Darwin)
            if command -v brew >/dev/null 2>&1; then
                warn "$pkg missing — recommended:  brew install $mac"
            else
                warn "$pkg missing — install Homebrew, then: brew install $mac"
            fi
            ;;
        Linux)
            if command -v apt >/dev/null 2>&1; then
                [ -n "$deb" ] && warn "$pkg missing — recommended:  sudo apt install $deb"
            elif command -v dnf >/dev/null 2>&1; then
                [ -n "$rh" ] && warn "$pkg missing — recommended:  sudo dnf install $rh"
            elif command -v pacman >/dev/null 2>&1; then
                [ -n "$arch" ] && warn "$pkg missing — recommended:  sudo pacman -S $arch"
            else
                warn "$pkg missing (your package manager wasn't detected)"
            fi
            ;;
        *)
            warn "$pkg missing"
            ;;
    esac
}

suggest ffmpeg ffmpeg ffmpeg ffmpeg ffmpeg
suggest fzf    fzf    fzf    fzf    fzf
suggest ollama ollama ""     ollama-bin ollama   # local LLM (optional)

# ─── manager sidecar (optional, opt-in) ─────────────────────────────────────
#
# tools/booki-manager is a small Rust menubar app. Building it costs ~30s and
# ~350 MB of cargo target cache, so we ask before doing it. Re-running the
# installer asks again — answer `n` to keep things lightweight, `y` once you
# decide you want the menubar UI.
#
# Reads from /dev/tty so the prompt works under `curl … | sh` (where stdin is
# the piped script, not the terminal). When /dev/tty isn't reachable (CI,
# automation) we default to `no` and print a manual-build hint.

prompt_yes() {
    msg=$1
    if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
        return 1
    fi
    printf '%s [y/N] ' "$msg" > /dev/tty
    read -r ans < /dev/tty || return 1
    case "$ans" in
        y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

say "Manager sidecar"
MGR_SRC=$BOOKI_HOME/tools/booki-manager
MGR_BIN=$MGR_SRC/target/release/booki-manager
MGR_WRAPPER=$XDG_BIN_HOME/booki-manager

if [ ! -d "$MGR_SRC" ]; then
    warn "tools/booki-manager not present in checkout — skipping"
elif ! command -v cargo >/dev/null 2>&1; then
    warn "cargo missing — skipping manager build"
    warn "  install Rust toolchain to enable: https://rustup.rs/"
elif prompt_yes "Build the optional booki-manager menubar app now (cargo build --release)?"; then
    # Best-effort dep-advisory scan before we compile something
    # we'll then exec from a wrapper. cargo-audit might not be
    # installed; print a hint instead of failing. (P5-06)
    if command -v cargo-audit >/dev/null 2>&1; then
        ( cd "$MGR_SRC" && cargo audit --quiet ) || \
            warn "cargo audit reported advisories — review before continuing"
    else
        warn "cargo-audit not installed — skipping advisory scan"
        warn "  (cargo install cargo-audit  to enable)"
    fi
    ( cd "$MGR_SRC" && cargo build --release --quiet )
    if [ ! -x "$MGR_BIN" ]; then
        die "cargo build did not produce $MGR_BIN"
    fi
    ok "built $MGR_BIN"

    # Generated wrapper pins BOOKI_HOME so the manager finds config.toml + the
    # ./booki entrypoint regardless of cwd at launch time.
    cat > "$MGR_WRAPPER" <<EOF
#!/bin/sh
# booki-manager wrapper — generated by install.sh; safe to overwrite.
exec env BOOKI_HOME="$BOOKI_HOME" "$MGR_BIN" "\$@"
EOF
    chmod +x "$MGR_WRAPPER"
    ok "wrote $MGR_WRAPPER"
else
    warn "skipped — build later with: (cd $MGR_SRC && cargo build --release)"
fi

# ─── done ───────────────────────────────────────────────────────────────────

cat <<EOF

${BOLD}Booki installed.${NC}

  Code      : $BOOKI_HOME
  Venv      : $VENV
  Wrapper   : $WRAPPER
EOF
if [ -x "$MGR_WRAPPER" ]; then
cat <<EOF
  Manager   : $MGR_WRAPPER
EOF
fi
cat <<EOF

Open a fresh shell (or 'source' your rc file) and run the wizard:

  ${BOLD}booki bootstrap${NC}     # interactive: sources, embeddings, LLM, manager

Then the usual loop:

  booki sync            # pull from sources
  booki ingest          # build the vector index
  booki web             # browse in your browser
  booki doctor          # check the install any time
EOF
if [ ! -x "$MGR_WRAPPER" ]; then
cat <<EOF

If you opt into the menubar manager during ${BOLD}booki bootstrap${NC}, build it
with the cargo command the wizard prints at the end (requires Rust:
https://rustup.rs/).
EOF
else
cat <<EOF
  booki-manager         # menubar app: watches bookmarks + scheduled sync/ingest
EOF
fi
cat <<EOF

Re-run this installer any time to update — it's idempotent and only
fast-forwards origin/$BRANCH.
EOF
