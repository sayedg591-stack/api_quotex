#!/usr/bin/env bash
# Run login.py with correct environment for Playwright on Replit NixOS.
# On NixOS, Playwright's downloaded Chromium (built for Ubuntu) can't find
# shared libraries via the default linker search path. We build LD_LIBRARY_PATH
# from every Nix store package currently in HOST_PATH, plus two packages whose
# libraries aren't reflected there (mesa-libgbm for libgbm.so.1, systemd for
# libudev.so.1). The Nix store paths are content-addressed and stable.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Playwright env ────────────────────────────────────────────────────────────
export PLAYWRIGHT_BROWSERS_PATH="$SCRIPT_DIR/.playwright-browsers"
export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true

# ── Build LD_LIBRARY_PATH from installed Nix packages ────────────────────────
# Start with the two packages not covered by HOST_PATH:
NIX_LIBS="/nix/store/24w3s75aa2lrvvxsybficn8y3zxd27kp-mesa-libgbm-25.1.0/lib"     # libgbm.so.1
NIX_LIBS="${NIX_LIBS}:/nix/store/0bbg0gbgw2m3clxrq57q5v2hnzz660bj-systemd-254.10/lib" # libudev.so.1

# Add lib dirs derived from every bin dir in HOST_PATH
if [ -n "${HOST_PATH:-}" ]; then
    while IFS= read -r bindir; do
        libdir="${bindir%/bin}/lib"
        [ -d "$libdir" ] && NIX_LIBS="${NIX_LIBS}:${libdir}"
    done < <(echo "$HOST_PATH" | tr ':' '\n')
fi

export LD_LIBRARY_PATH="${NIX_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# ── Run ───────────────────────────────────────────────────────────────────────
cd "$SCRIPT_DIR"
exec python3 login.py
