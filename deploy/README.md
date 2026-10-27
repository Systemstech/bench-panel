# deploy/ — running BenchPanel on a server

A worked example of BenchPanel as a Linux service, reached from other machines
through Apache. You don't need any of it to run BenchPanel on your own desktop
(`benchpanel`, see the main README).

| File | What it is |
|---|---|
| `bench-panel.service` | systemd unit. Listens on **127.0.0.1:8765 only**. `@USER@` and `@HOME@` are filled in by `deploy.sh --unit` |
| `bench_panel.conf` | Apache `<Location /bench>`: Basic auth + reverse proxy to the panel |
| `deploy.sh` | Runs every check and self-test, then copies the code to the server and restarts the service |

## Why loopback plus a proxy

The panel can switch on a power supply. Bound to `0.0.0.0` it would hand that button
to anyone on the LAN. The service binds to 127.0.0.1, so the only way in is the
authenticated HTTPS proxy.

## Setting it up

1. **Tell `deploy.sh` where the server is.** Create `local/deploy.env` in the repo
   root (git ignores `local/`), with one line:

   ```
   BENCH_HOST=you@your-server
   ```

   That account needs SSH key login and `sudo` on the server.

2. **First deploy, with the unit:**

   ```
   ./deploy/deploy.sh --unit
   ```

   It refuses to ship if any plugin check or self-test fails. It then rsyncs the code
   to `~/bench_panel/` on the server, installs the unit (backing up any existing one
   with a date stamp), and restarts it. On the server, run
   `sudo systemctl enable bench-panel` once.

3. **Apache** (Fedora/RHEL paths): copy `bench_panel.conf` into your TLS virtual host
   or `conf.d/`, then create the login:

   ```
   sudo htpasswd -c /etc/httpd/conf/bench.htpasswd <user>
   sudo chown root:apache /etc/httpd/conf/bench.htpasswd
   sudo chmod 640 /etc/httpd/conf/bench.htpasswd
   sudo restorecon /etc/httpd/conf/bench.htpasswd
   ```

   Run `htpasswd` **on the server**: that's the copy Apache reads. `-c` rewrites the
   file from scratch. With SELinux enforcing, also run
   `sudo setsebool -P httpd_can_network_connect 1` so Apache may proxy.

4. Open `https://your-server/bench/` and add your instruments from the dashboard.
   They are saved in `~/bench_panel/site.json` on the server, which a deploy never
   overwrites.

## Everyday use

```
./deploy/deploy.sh           # code changes
./deploy/deploy.sh --docs    # also publish local/bench_docs.html (your own docs page, optional)
./deploy/deploy.sh --unit    # also reinstall the systemd unit
```

**Every restart runs the safe shutdown.** Each instrument that is ON has its outputs
switched off and its front panel released, and the panel comes back with every card
OFF. Don't deploy in the middle of a measurement.

What a deploy never touches on the server: `site.json`, `plugins/`, `scripts/`,
`results/`, `sim.json`, `sim_results/` and the server's own `.bak-*` files. A deploy
ships code and the plugin catalog. Plugins are installed or upgraded from the
Instruments manager.

## local/

`local/` holds what belongs to one site and stays out of git: `deploy.env`, plus
`bench_docs.html` if you keep your own docs page for your bench (addresses, wiring,
login notes).
