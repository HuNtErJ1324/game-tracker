"""Game Tracker command line.

  python -m gametracker                 run everything on this PC (server + agent), open the dashboard
  python -m gametracker server          dashboard/database only (e.g. on a home server)
  python -m gametracker agent --server URL --token TOKEN
                                        tracker only, reporting to a remote server (saved for next time)
  python -m gametracker scan            print the games detected on this PC
  python -m gametracker token           print the agent token for this server
  python -m gametracker demo            the dashboard with made-up data in a throwaway folder (nothing on this PC is read)
"""
import argparse
import os
import sys
import threading
import time
import webbrowser


def _wait_forever(stoppers, alive=lambda: True):
    try:
        while alive():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        for s in stoppers:
            s()
        time.sleep(1)


def cmd_all(args):
    from . import config, server
    from .agent import Agent
    port = args.port or int(config.get("web_port") or 8787)
    httpd = server.serve(args.host, port)
    url = f"http://localhost:{port}"
    print(f"Game Tracker running at {url}  (Ctrl+C to stop)")
    agent = Agent(f"http://127.0.0.1:{port}", config.agent_token())
    threading.Thread(target=agent.run, daemon=True, name="agent").start()
    if not args.no_browser:
        webbrowser.open(url)
    _wait_forever([agent.stop, httpd.shutdown])


def cmd_server(args):
    from . import config, server
    port = args.port or int(config.get("web_port") or 8787)
    httpd = server.serve(args.host, port)
    print(f"Game Tracker server on http://{args.host}:{port}  (Ctrl+C to stop)")
    print("Agent token: see Settings in the dashboard, or run `python -m gametracker token`.")
    in_container = os.path.exists("/.dockerenv")  # Docker maps the port; the bind address is the compose file's call
    if args.host in ("0.0.0.0", "::") and not in_container:
        print("Note: listening on all interfaces. Prefer --host 127.0.0.1 behind `tailscale serve`, "
              "or bind to this machine's Tailscale IP.")
    _wait_forever([httpd.shutdown])


def cmd_agent(args):
    from . import config
    from .agent import Agent
    saved = config.load_agent()
    url = args.server or saved.get("server_url")
    token = args.token or saved.get("token")
    if not url or not token:
        sys.exit("First run needs --server URL and --token TOKEN (shown on the server's Settings page).")
    if args.server or args.token:
        config.save_agent(url, token)
    print(f"Game Tracker agent reporting to {url}  (Ctrl+C to stop)")
    agent = Agent(url, token)
    t = threading.Thread(target=agent.run, daemon=True, name="agent")
    t.start()
    _wait_forever([agent.stop], alive=t.is_alive)
    if not agent._stop.is_set():
        sys.exit(1)  # agent gave up (e.g. rejected token); the reason was printed above


def cmd_scan(args):
    from . import detect
    games = detect.scan_all()
    for g in games:
        pt = g["extra"].get("imported_playtime_min")
        print(f"{g['source']:<10} {g['name'][:45]:<45} {f'{pt // 60}h' if pt else '':>6}  {g['install_dir']}")
    print(f"\n{len(games)} games found.")


def cmd_demo(args):
    from . import demo
    httpd, folder, keep = demo.start(args.host, args.port)
    url = f"http://localhost:{httpd.server_address[1]}"
    print(f"Game Tracker demo at {url}  (Ctrl+C to stop)")
    print(f"Made-up data in {folder}" + ("" if keep else ", deleted when the demo stops"))
    if not args.no_browser:
        webbrowser.open(url)
    _wait_forever([httpd.shutdown])
    demo.cleanup(folder, keep)


def cmd_coach_runner(args):
    from . import coach_runner
    coach_runner.serve(port=args.port or 8790)


def cmd_token(args):
    from . import config
    print(config.agent_token())


def main():
    p = argparse.ArgumentParser(prog="gametracker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    for name in ("run", "server", "demo"):
        sp = sub.add_parser(name)
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int)
        sp.add_argument("--no-browser", action="store_true")
    ap = sub.add_parser("agent")
    ap.add_argument("--server", help="e.g. https://homeserver.your-tailnet.ts.net")
    ap.add_argument("--token")
    sub.add_parser("scan")
    sub.add_parser("token")
    cr = sub.add_parser("coach-runner", help="prime-agent coach service (Docker)")
    cr.add_argument("--port", type=int)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--log", help="append output to this file instead of the console")
    args = p.parse_args()
    if args.log:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
    {"run": cmd_all, None: cmd_all, "server": cmd_server, "agent": cmd_agent,
     "scan": cmd_scan, "token": cmd_token, "demo": cmd_demo, "coach-runner": cmd_coach_runner}[args.cmd](args)


if __name__ == "__main__":
    main()
