#!/usr/bin/env bash
# Register this PC as the self-hosted runner of the model tier (.github/workflows/model.yml).
# It runs as you, under `systemd --user` with lingering: while you're logged out too, and with
# your access to PipeWire and the serial ports. Needs `gh`, logged in as an admin of the repo.
# Run it again to register it afresh; the runner updates itself.
#
# To remove it:
#   systemctl --user disable --now tinyaisense-runner
#   ~/actions-runner/config.sh remove --token \
#     "$(gh api -X POST repos/unkier/tinyaisense/actions/runners/remove-token --jq .token)"
set -euo pipefail

REPO=unkier/tinyaisense
LABEL=tinyaisense-hub
DIR="${RUNNER_DIR:-$HOME/actions-runner}"
UNIT=tinyaisense-runner.service

command -v gh > /dev/null || { echo "needs gh: https://cli.github.com" >&2; exit 1; }
gh auth status > /dev/null

if [[ ! -x "$DIR/run.sh" ]]; then
  case "$(uname -m)" in
    x86_64) arch=x64 ;;
    aarch64) arch=arm64 ;;
    *) echo "no runner for $(uname -m)" >&2; exit 1 ;;
  esac
  tag=$(gh api repos/actions/runner/releases/latest --jq .tag_name)
  tarball="actions-runner-linux-$arch-${tag#v}.tar.gz"
  digest=$(gh api "repos/actions/runner/releases/tags/$tag" \
    --jq ".assets[] | select(.name == \"$tarball\") | .digest")
  [[ "$digest" == sha256:* ]] || { echo "no sha256 for $tarball" >&2; exit 1; }
  mkdir -p "$DIR"
  echo "downloading $tarball into $DIR"
  gh release download "$tag" --repo actions/runner --pattern "$tarball" --dir "$DIR" --clobber
  echo "${digest#sha256:}  $DIR/$tarball" | sha256sum --check --quiet
  tar -xzf "$DIR/$tarball" -C "$DIR"
  rm "$DIR/$tarball"
fi

cd "$DIR"
if [[ -f .runner ]]; then
  # config.sh configures a runner once: drop the registration there is first.
  systemctl --user stop "$UNIT" 2> /dev/null || true
  ./config.sh remove \
    --token "$(gh api -X POST "repos/$REPO/actions/runners/remove-token" --jq .token)"
fi
./config.sh --unattended --replace --url "https://github.com/$REPO" \
  --token "$(gh api -X POST "repos/$REPO/actions/runners/registration-token" --jq .token)" \
  --name "$(hostname)" --labels "$LABEL"

mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$UNIT" << UNIT
[Unit]
Description=GitHub Actions runner for $REPO (model tier)
Wants=network-online.target
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/run.sh
Restart=always
RestartSec=10
# run.sh passes the stop on to the runner, which lets a job finish its cleanup; whatever is left
# after 5 min is killed.
Environment=RUNNER_MANUALLY_TRAP_SIG=1
KillMode=mixed
TimeoutStopSec=5min

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable "$UNIT"
systemctl --user restart "$UNIT"
loginctl enable-linger "$USER"
echo "the runner is up: systemctl --user status $UNIT"
