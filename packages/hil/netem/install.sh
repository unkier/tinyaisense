#!/usr/bin/env bash
# Install the HIL tier's netem wrapper as root's, and a sudo rule that lets you run it, and only
# it, as root with no password: the HIL fault scenarios and the soak need it. Run it as yourself;
# it asks for your sudo password. Run it again after the wrapper changes.
#
#   packages/hil/netem/install.sh            # install, or update
#   packages/hil/netem/install.sh --remove   # remove both
set -euo pipefail
BIN=/usr/local/sbin/tinyaisense-netem
RULE=/etc/sudoers.d/tinyaisense-netem

if [[ ${1-} == --remove ]]; then
  sudo rm -f "$RULE" "$BIN"
  echo "removed $BIN and $RULE"
  exit 0
fi
command -v tc > /dev/null || { echo "needs tc, from iproute2" >&2; exit 1; }
here=$(dirname "$(readlink -f "$0")")
rule=$(mktemp)
trap 'rm -f "$rule"' EXIT
echo "$USER ALL=(root) NOPASSWD: $BIN" > "$rule"
sudo visudo -cqf "$rule"
# Root's and not writable by you, or the rule would give you root.
sudo install -o root -g root -m 0755 "$here/tinyaisense-netem" "$BIN"
sudo install -o root -g root -m 0440 "$rule" "$RULE"
echo "installed $BIN: $USER runs it with sudo -n $BIN"
