#!/usr/bin/env bash
# Publish a Vetromar desktop release: bump versions, build, sign the updater
# artifact, and upload everything to a GitHub release in the public releases
# repo. Run locally on the build machine — there is no CI for this.
#
#   ./release.sh 0.1.2 ["release notes"]
#   ./release.sh --bump patch ["release notes"]   # 0.1.1 → 0.1.2
#
# The script never commits: after a successful publish, commit the version
# bump as `release: vX.Y.Z` on a branch and PR it to main.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"

# The ONE authoritative home of the releases-repo slug. It unavoidably also
# appears in tauri.conf.json (updater endpoint) and the live site's download
# button (index.html in the Vetromar/site repo) — preflight checks below fail
# the release on any drift.
RELEASES_REPO="Vetromar/releases"
KEY_PATH="${VETROMAR_UPDATER_KEY:-$HOME/.tauri/vetromar-updater.key}"

CONF="src-tauri/tauri.conf.json"
CURRENT="$(python3 -c "import json; print(json.load(open('$CONF'))['version'])")"

# ---- arguments --------------------------------------------------------------
if [[ "${1:-}" == "--bump" ]]; then
  PART="${2:?usage: release.sh --bump patch|minor|major [notes]}"
  VERSION="$(python3 - "$CURRENT" "$PART" <<'EOF'
import sys
major, minor, patch = map(int, sys.argv[1].split("."))
part = sys.argv[2]
if part == "major": major, minor, patch = major + 1, 0, 0
elif part == "minor": minor, patch = minor + 1, 0
elif part == "patch": patch += 1
else: sys.exit(f"unknown bump part {part!r}")
print(f"{major}.{minor}.{patch}")
EOF
)"
  NOTES="${3:-Vetromar $VERSION}"
else
  VERSION="${1:?usage: release.sh <version> [notes]  |  release.sh --bump patch|minor|major [notes]}"
  NOTES="${2:-Vetromar $VERSION}"
fi

# ---- preflight --------------------------------------------------------------
echo "==> Preflight (v$CURRENT → v$VERSION)…"

python3 - "$CURRENT" "$VERSION" <<'EOF'
import sys
cur, new = (tuple(map(int, v.split("."))) for v in sys.argv[1:3])
if new <= cur:
    sys.exit(f"new version {sys.argv[2]} must be greater than current {sys.argv[1]}")
EOF

if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo "ERROR: git tree is not clean — commit or stash first (the post-release" >&2
  echo "       diff must be exactly the version bump)." >&2
  exit 1
fi

if [[ ! -f "$KEY_PATH" ]]; then
  echo "ERROR: updater signing key not found at $KEY_PATH" >&2
  echo "       Generate one: npx tauri signer generate -w $KEY_PATH" >&2
  exit 1
fi

# Apple signing + notarization (M23 follow-up: releases are signed builds).
IDENTITY_LINE="$(security find-identity -v -p codesigning | grep -m1 "Developer ID Application" || true)"
if [[ -z "$IDENTITY_LINE" ]]; then
  echo "ERROR: no 'Developer ID Application' certificate in the keychain." >&2
  echo "       Create one in Xcode: Settings → Accounts → Manage Certificates" >&2
  echo "       → + → Developer ID Application." >&2
  exit 1
fi
APPLE_SIGNING_IDENTITY="$(sed -E 's/.*"(.+)".*/\1/' <<<"$IDENTITY_LINE")"
export APPLE_SIGNING_IDENTITY
echo "    signing as: $APPLE_SIGNING_IDENTITY"

NOTARY_PROFILE="${VETROMAR_NOTARY_PROFILE:-vetromar-notary}"
if ! xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
  echo "ERROR: notarization keychain profile '$NOTARY_PROFILE' is missing or" >&2
  echo "       its credentials no longer work. Store it with:" >&2
  echo "       xcrun notarytool store-credentials $NOTARY_PROFILE \\" >&2
  echo "         --apple-id <apple-id> --team-id <TEAMID> --password <app-specific-pw>" >&2
  exit 1
fi

if [[ "$RELEASES_REPO" == *"VETROMAR-ORG"* ]]; then
  echo "ERROR: RELEASES_REPO is still the placeholder — create the GitHub org +" >&2
  echo "       public releases repo, then update the constant at the top of this" >&2
  echo "       script (and the endpoint in $CONF + the download link in the" >&2
  echo "       Vetromar/site repo)." >&2
  exit 1
fi

if ! grep -q "$RELEASES_REPO" "$CONF"; then
  echo "ERROR: $CONF does not reference $RELEASES_REPO — the updater endpoint" >&2
  echo "       has drifted from RELEASES_REPO. Fix before releasing." >&2
  exit 1
fi

# The download button lives in the Vetromar/site repo now — check the live
# site instead of a local file.
if ! curl -fsS https://vetromar.com/ | grep -q "$RELEASES_REPO"; then
  echo "ERROR: the live vetromar.com download link does not reference" >&2
  echo "       $RELEASES_REPO — fix index.html in Vetromar/site before releasing." >&2
  exit 1
fi

gh auth status >/dev/null 2>&1 || {
  echo "ERROR: gh is not authenticated. Either 'gh auth login', or export GH_TOKEN" >&2
  echo "       with a Vetromar-account PAT that can push to $RELEASES_REPO." >&2
  exit 1
}
if [[ "$(gh api "repos/$RELEASES_REPO" --jq .permissions.push 2>/dev/null)" != "true" ]]; then
  echo "ERROR: no push access to $RELEASES_REPO. Either the signed-in gh account" >&2
  echo "       must be an org member with write access, or export GH_TOKEN with a" >&2
  echo "       fine-grained PAT for the Vetromar account (gh prefers GH_TOKEN)." >&2
  exit 1
fi

# ---- version bump (6 locations; tauri.conf.json is the source of truth) -----
echo "==> Bumping versions to ${VERSION}…"
python3 - "$VERSION" "$ROOT" <<'EOF'
import json, re, sys
from pathlib import Path

version, root = sys.argv[1], Path(sys.argv[2])

conf = root / "desktop/src-tauri/tauri.conf.json"
data = json.loads(conf.read_text())
data["version"] = version
conf.write_text(json.dumps(data, indent=2) + "\n")

def sub_line(path, pattern, repl):
    text = path.read_text()
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"version pattern not found in {path}")
    path.write_text(new)

sub_line(root / "desktop/src-tauri/Cargo.toml", r'^version = "[^"]+"', f'version = "{version}"')
sub_line(root / "pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"')
sub_line(root / "vetromar/__init__.py", r'^__version__ = "[^"]+"', f'__version__ = "{version}"')
EOF
# npm keeps package.json + package-lock.json consistent itself.
npm version --no-git-tag-version "$VERSION" >/dev/null
npm --prefix frontend version --no-git-tag-version "$VERSION" >/dev/null

# ---- build (signed) ---------------------------------------------------------
echo "==> Building (signed)…"
export TAURI_SIGNING_PRIVATE_KEY="$KEY_PATH"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD="${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}"
./build.sh

BUNDLE="src-tauri/target/release/bundle"
TARBALL="$BUNDLE/macos/Vetromar.app.tar.gz"
SIG="$TARBALL.sig"
DMG="$BUNDLE/dmg/Vetromar_${VERSION}_aarch64.dmg"
for f in "$TARBALL" "$SIG" "$DMG"; do
  [[ -s "$f" ]] || { echo "ERROR: expected artifact missing or empty: $f" >&2; exit 1; }
done

# ---- stage + latest.json ----------------------------------------------------
echo "==> Staging release assets…"
STAGING="release-staging"
rm -rf "$STAGING" && mkdir -p "$STAGING"
cp "$TARBALL" "$SIG" "$STAGING/"
cp "$DMG" "$STAGING/Vetromar_${VERSION}_aarch64.dmg"
# Stable name: the website's "Download for Mac" permalink always serves this.
cp "$DMG" "$STAGING/Vetromar.dmg"

python3 - "$VERSION" "$RELEASES_REPO" "$SIG" "$NOTES" > "$STAGING/latest.json" <<'EOF'
import datetime, json, sys
from pathlib import Path
version, repo, sig_path, notes = sys.argv[1:5]
sig = Path(sig_path).read_text().strip()
if not sig:
    sys.exit("signature file is empty")
print(json.dumps({
    "version": version,
    "notes": notes,
    "pub_date": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    "platforms": {
        # Versioned (immutable) asset URL — a half-propagated "latest" can
        # never mix versions; only latest.json itself rides the permalink.
        "darwin-aarch64": {
            "url": f"https://github.com/{repo}/releases/download/v{version}/Vetromar.app.tar.gz",
            "signature": sig,
        }
    },
}, indent=2))
EOF
python3 -c "
import json, sys
data = json.load(open('$STAGING/latest.json'))
assert data['version'] == '$VERSION', 'latest.json version mismatch'
assert data['platforms']['darwin-aarch64']['signature'], 'empty signature'
"

# ---- publish ----------------------------------------------------------------
echo "==> Publishing v$VERSION to ${RELEASES_REPO}…"
if gh release view "v$VERSION" -R "$RELEASES_REPO" >/dev/null 2>&1; then
  echo "    release exists — re-uploading assets (clobber)…"
  gh release upload "v$VERSION" -R "$RELEASES_REPO" --clobber "$STAGING"/*
else
  gh release create "v$VERSION" -R "$RELEASES_REPO" \
    --title "Vetromar $VERSION" --notes "$NOTES" "$STAGING"/*
fi

echo "==> Published: https://github.com/$RELEASES_REPO/releases/tag/v$VERSION"
echo
echo "Next: commit the version bump —"
echo "  git checkout -b release-v$VERSION && git commit -am 'release: v$VERSION' && gh pr create"
