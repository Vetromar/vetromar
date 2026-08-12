#!/usr/bin/env bash
# Build the full macOS desktop app: engine sidecar (PyInstaller) + Tauri bundle.
#
#   ./build.sh            # full build → src-tauri/target/release/bundle/{macos,dmg}
#   ./build.sh --sidecar  # only (re)build the Python sidecar
#
# Prereqs: the project venv with `pip install -e ".[capture,ui]"`, node/npm,
# the Rust toolchain, and PyInstaller (`pip install pyinstaller`).
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(cd .. && pwd)"
PY="${VETROMAR_PY:-$ROOT/.venv/bin/python}"

echo "==> Building engine sidecar with PyInstaller (this is the big one)…"
"$PY" -m PyInstaller --noconfirm --distpath dist --workpath build vetromar-sidecar.spec

echo "==> Staging sidecar into src-tauri/sidecar/…"
rm -rf src-tauri/sidecar
mkdir -p src-tauri/sidecar
cp -R dist/vetromar-sidecar src-tauri/sidecar/vetromar-sidecar

# Native meeting-capture helper (Core Audio process taps, macOS 14.2+ at
# runtime). It lands inside the sidecar dir so the Mach-O codesign walk below
# signs it with the sidecar entitlements automatically.
echo "==> Compiling the audio capture helper (swiftc)…"
swiftc -O -target arm64-apple-macos14.2 \
  -o src-tauri/sidecar/vetromar-sidecar/vetromar-helper helper/main.swift
# Informational only — build hosts older than 14.2 still produce a valid app.
src-tauri/sidecar/vetromar-sidecar/vetromar-helper selftest \
  || echo "    (helper selftest failed on this host — feature self-gates at runtime)"

# Smoke-test the bundled binary before we wrap it.
echo "==> Smoke-testing the bundled sidecar…"
BIN="src-tauri/sidecar/vetromar-sidecar/vetromar-sidecar"
"$BIN" --help >/dev/null && echo "    sidecar --help OK"

if [[ "${1:-}" == "--sidecar" ]]; then
  echo "Sidecar built at $BIN"
  exit 0
fi

# Apple code signing is driven by APPLE_SIGNING_IDENTITY (release.sh sets it);
# plain dev builds skip every signing/notarization step and behave as before.
NOTARY_PROFILE="${VETROMAR_NOTARY_PROFILE:-vetromar-notary}"

if [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  # Notarization requires EVERY Mach-O in the bundle signed with the hardened
  # runtime — Tauri signs the app itself but not resource binaries, and the
  # PyInstaller sidecar carries hundreds of dylibs/sos. Sign them in place
  # before Tauri copies them into the bundle (dylibs first, main binary last).
  echo "==> Codesigning sidecar Mach-O binaries (takes a few minutes)…"
  SIDECAR_DIR="src-tauri/sidecar/vetromar-sidecar"
  MACHO_LIST="$(mktemp)"
  python3 - "$SIDECAR_DIR" <<'EOF' > "$MACHO_LIST"
import os, shutil, sys
root = sys.argv[1]

# Tauri's resource copy DEREFERENCES symlinks into real files. A framework
# binary's signature references the framework Info.plist relative to its own
# location, so a symlink like _internal/Python -> Python.framework/.../Python
# becomes a real copy whose signature is invalid where it lands (the v0.1.7
# first-notarization rejection). Dereference symlinks here first so staging
# matches the final bundle exactly and every file is signed in place.
links = []
for dirpath, dirnames, filenames in os.walk(root):
    for name in dirnames + filenames:
        path = os.path.join(dirpath, name)
        if os.path.islink(path):
            links.append(path)
for link in links:
    if not os.path.islink(link):
        continue  # already replaced via a parent directory copy
    if any(part.endswith(".framework") for part in link.split(os.sep)):
        # Framework-internal convenience symlinks (Python.framework/Python,
        # Versions/Current, root Resources). A dereferenced REAL file at the
        # framework root makes codesign's bundle detection ambiguous ("bundle
        # format is ambiguous") and the copies are never used at runtime —
        # delete them instead. The post-signing smoke test below proves the
        # sidecar still launches without them.
        os.unlink(link)
        continue
    target = os.path.realpath(link)
    if not os.path.exists(target):
        continue
    os.unlink(link)
    if os.path.isdir(target):
        shutil.copytree(target, link, symlinks=False)
    else:
        shutil.copy2(target, link)

MAGICS = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
          b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
for dirpath, _, names in os.walk(root):
    for name in names:
        path = os.path.join(dirpath, name)
        if os.path.islink(path):
            continue
        try:
            with open(path, "rb") as f:
                magic = f.read(4)
        except OSError:
            continue
        if magic in MAGICS:
            print(path)
EOF
  MAIN_BIN="$SIDECAR_DIR/vetromar-sidecar"
  grep -vxF "$MAIN_BIN" "$MACHO_LIST" | tr '\n' '\0' | xargs -0 -n 50 \
    codesign --force --timestamp --options runtime \
      --entitlements src-tauri/entitlements-sidecar.plist \
      -s "$APPLE_SIGNING_IDENTITY" || {
    echo "ERROR: codesign failed on at least one sidecar binary — its error is" >&2
    echo "       printed above (xargs only reports failure at the end)." >&2
    exit 1
  }
  codesign --force --timestamp --options runtime \
    --entitlements src-tauri/entitlements-sidecar.plist \
    -s "$APPLE_SIGNING_IDENTITY" "$MAIN_BIN"
  echo "    signed $(wc -l < "$MACHO_LIST" | tr -d ' ') Mach-O files"
  rm -f "$MACHO_LIST"
  # Re-smoke-test: the pruned symlinks + hardened-runtime signatures must not
  # break launch — fail HERE, not after a 20-minute notarization round-trip.
  "$BIN" --help >/dev/null && echo "    signed sidecar --help OK"
fi

echo "==> Building frontend + Tauri app…"
# Tauri bundles only the .app (targets exclude "dmg" — its dmg step drives
# Finder/AppleScript and fails headless). The .dmg is always built with
# hdiutil below, so a tauri failure here is a real failure.
# When APPLE_SIGNING_IDENTITY is set Tauri signs the .app with it (hardened
# runtime + entitlements.plist). We never set APPLE_ID/APPLE_PASSWORD, so
# Tauri won't attempt notarization — that happens on the .dmg below.
npm run tauri build

VERSION="$(python3 -c "import json; print(json.load(open('src-tauri/tauri.conf.json'))['version'])")"
BUNDLE="src-tauri/target/release/bundle"
APP="$BUNDLE/macos/Vetromar.app"
DMG="$BUNDLE/dmg/Vetromar_${VERSION}_aarch64.dmg"
if [[ ! -d "$APP" ]]; then
  echo "ERROR: Vetromar.app was not produced — see the Tauri output above." >&2
  exit 1
fi

# Updater artifacts (.app.tar.gz + .sig) are emitted only when the signing key
# is in the env — release.sh sets it; plain dev builds skip them.
TARBALL="$BUNDLE/macos/Vetromar.app.tar.gz"
if [[ -n "${TAURI_SIGNING_PRIVATE_KEY:-}${TAURI_SIGNING_PRIVATE_KEY_PATH:-}" ]]; then
  if [[ ! -f "$TARBALL" || ! -s "$TARBALL.sig" ]]; then
    echo "ERROR: signing key was set but updater artifacts are missing:" >&2
    echo "       $TARBALL (+ .sig)" >&2
    exit 1
  fi
fi

echo "==> Building the .dmg with hdiutil…"
mkdir -p "$(dirname "$DMG")"
hdiutil create -volname "Vetromar" -srcfolder "$APP" -ov -format UDZO "$DMG"

if [[ -n "${APPLE_SIGNING_IDENTITY:-}" ]]; then
  echo "==> Codesigning the .dmg…"
  codesign --force --timestamp -s "$APPLE_SIGNING_IDENTITY" "$DMG"

  # One submission covers the dmg AND the nested .app — Apple issues tickets
  # for every signed item inside. ~500 MB upload; usually done in minutes.
  echo "==> Notarizing the .dmg (upload + Apple scan — this takes a while)…"
  SUBMIT_OUT="$(xcrun notarytool submit "$DMG" \
    --keychain-profile "$NOTARY_PROFILE" --wait 2>&1 | tee /dev/stderr)" || true
  if ! grep -q "status: Accepted" <<<"$SUBMIT_OUT"; then
    SUBMISSION_ID="$(grep -m1 -Eo 'id: [0-9a-f-]+' <<<"$SUBMIT_OUT" | awk '{print $2}')"
    echo "ERROR: notarization was not accepted." >&2
    if [[ -n "$SUBMISSION_ID" ]]; then
      echo "       Per-file details:" >&2
      xcrun notarytool log "$SUBMISSION_ID" --keychain-profile "$NOTARY_PROFILE" >&2 || true
    fi
    exit 1
  fi

  echo "==> Stapling…"
  xcrun stapler staple "$DMG"
  # Same ticket applies to the standalone .app (fetched by signature hash);
  # best-effort — the dmg staple is the one that matters for downloads.
  xcrun stapler staple "$APP" || true

  echo "==> Verifying Gatekeeper acceptance…"
  spctl -a -t open --context context:primary-signature -vv "$DMG"
  spctl --assess --type execute -vv "$APP"
  echo "    signed + notarized OK"
fi

echo "==> Done."
echo "    App: $APP"
echo "    DMG: $DMG"
if [[ -f "$TARBALL" ]]; then
  echo "    Updater: $TARBALL (+ .sig)"
fi
