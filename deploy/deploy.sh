#!/bin/bash
# Push the bench panel from this working copy to a server and restart it.
#
#   ./deploy/deploy.sh           code + README -> server:~/bench_panel/, restart, status
#   ./deploy/deploy.sh --docs    also publish local/bench_docs.html -> /var/www/html/bench-docs/
#   ./deploy/deploy.sh --unit    also install deploy/bench-panel.service and daemon-reload
#
# The restart runs the panel's safe shutdown first: every card that is ON has its
# outputs switched off and its front panel handed back. It comes back with every
# card OFF. Do not deploy in the middle of a measurement.
#
# Never --delete: the server's copy keeps its own dated .bak-* files.
#
# The server comes from local/deploy.env (git-ignored), one line:  BENCH_HOST=user@server
# See deploy/README.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$HERE/local/deploy.env" ] && . "$HERE/local/deploy.env"
HOST=${BENCH_HOST:?set BENCH_HOST=user@server in local/deploy.env (see deploy/README.md)}
RUSER=${HOST%@*}
TS=$(date +%Y%m%d-%H%M%S)

docs=0; unit=0
for a in "$@"; do
  case "$a" in
    --docs) docs=1 ;;
    --unit) unit=1 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

python3 -m py_compile "$HERE"/bench_panel.py "$HERE"/benchcore/*.py "$HERE"/catalog/*/driver.py "$HERE"/catalog/*/sim.py
# Every catalog plugin must meet the standard, and the checker must still catch
# what it claims to -- or nothing ships.
python3 "$HERE"/tools/check_plugin.py -q "$HERE"/catalog/*
python3 "$HERE"/tests/test_conformance.py >/dev/null || { echo "conformance self-test FAILED"; exit 1; }
python3 "$HERE"/tools/gen_api_actions.py --check
python3 "$HERE"/tools/check_script.py -q "$HERE"/examples/*.py
python3 "$HERE"/tests/test_scripts.py >/dev/null || { echo "scripts self-test FAILED"; exit 1; }
# The simulator: every catalog script on the simulated bench, with the real transport
# booby-trapped -- plus run-key isolation, the editor's save and the lint.
python3 "$HERE"/tests/test_sim.py >/dev/null || { echo "simulator self-test FAILED"; exit 1; }
# The plugin manager's recovery path (refused plugin -> Update) and address parsing.
python3 "$HERE"/tests/test_manager.py >/dev/null || { echo "manager self-test FAILED"; exit 1; }
# E3632A: the readback check catches a stuck ADC; OCP and limits behave (simulator only).
python3 "$HERE"/tests/test_e3632a.py >/dev/null || { echo "E3632A self-test FAILED"; exit 1; }
# 6063B: shutdown seen through INP?=1, input-off-first rules, sense-switch flag (simulator only).
python3 "$HERE"/tests/test_6063b.py >/dev/null || { echo "6063B self-test FAILED"; exit 1; }
# 438A: ?ID + serial-poll keepalive through the real Link, 9.0000E+40 never a power,
# serial polls only while ZERO / CAL ADJ runs, CAL ADJ removes a gain error (simulator only).
python3 "$HERE"/tests/test_438a.py >/dev/null || { echo "438A self-test FAILED"; exit 1; }
# E3641A: setpoints judged on the live range (the psu_dmm_check restore case).
python3 "$HERE"/tests/test_e3641a.py >/dev/null || { echo "E3641A self-test FAILED"; exit 1; }

# NEVER site.json, plugins/, scripts/, results/, sim.json or sim_results/: those are the live bench -- which instruments are on
# it and the plugins installed from the dashboard. A deploy ships code and the
# catalog; installing or upgrading a plugin is done from the Instruments manager.
rsync -av --exclude '*.bak-*' --exclude '__pycache__' --exclude '.git' \
      --exclude 'deploy/' --exclude '/local/' --exclude 'bench_favicon.svg' \
      --exclude 'site.json*' --exclude 'plugins/' --exclude '/scripts/' --exclude '/results/' \
      --exclude '/sim_results/' --exclude '/sim.json' \
      --exclude 'dist/' --exclude 'build/' --exclude '*.egg-info' \
      "$HERE"/ "$HOST":bench_panel/

if [ $unit = 1 ]; then
  # Fill the unit's @USER@ / @HOME@ for the account it runs under.
  RHOME=$(ssh "$HOST" 'echo $HOME')
  sed "s#@USER@#$RUSER#g; s#@HOME@#$RHOME#g" "$HERE"/deploy/bench-panel.service | ssh "$HOST" 'cat > bench-panel.service.new'
  ssh "$HOST" "sudo cp -p /etc/systemd/system/bench-panel.service /etc/systemd/system/bench-panel.service.bak-$TS \
    && sudo cp ~/bench-panel.service.new /etc/systemd/system/bench-panel.service && rm ~/bench-panel.service.new \
    && sudo restorecon /etc/systemd/system/bench-panel.service && sudo systemctl daemon-reload"
fi

if [ $docs = 1 ]; then
  [ -f "$HERE/local/bench_docs.html" ] || { echo "--docs: no local/bench_docs.html"; exit 2; }
  scp -q "$HERE"/local/bench_docs.html "$HOST":bench_docs.new.html
  # SELinux silently 403s a hand-placed file without restorecon; chown to apache like the rest of the portal.
  ssh "$HOST" "D=/var/www/html/bench-docs && sudo cp -p \$D/index.html \$D/index.html.bak-$TS \
    && sudo cp ~/bench_docs.new.html \$D/index.html && rm ~/bench_docs.new.html \
    && sudo chown apache:apache \$D/index.html && sudo restorecon \$D/index.html \
    && curl -sk -o /dev/null -w 'bench-docs: %{http_code}\n' https://127.0.0.1/bench-docs/"
fi

ssh "$HOST" 'sudo systemctl restart bench-panel && sleep 2 && systemctl is-active bench-panel \
  && curl -s -o /dev/null -w "api/state: %{http_code}\n" http://127.0.0.1:8765/api/state'
