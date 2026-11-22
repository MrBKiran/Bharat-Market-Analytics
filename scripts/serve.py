"""Run the whole app at one address: the API and the dashboard (Phase 12).

Run from the project root:

    .venv\\Scripts\\python scripts\\serve.py                 # serve the existing dashboard build + the API on http://127.0.0.1:8000
    .venv\\Scripts\\python scripts\\serve.py --build         # build the dashboard first (needs Node.js), then serve
    .venv\\Scripts\\python scripts\\serve.py --port 9000
    .venv\\Scripts\\python scripts\\serve.py --reload        # development: restart the API when Python files change
    .venv\\Scripts\\python scripts\\serve.py --host 0.0.0.0  # reachable from other machines on your network (read the warning!)

For dashboard development use `npm run dev` in frontend/ instead (hot reload), with the API running separately.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.mongo import check_connection  # noqa: E402


def build_dashboard() -> bool:
    npm = shutil.which("npm")
    frontend = ROOT / "frontend"
    if not npm:
        print("Node.js (npm) was not found, so the dashboard cannot be built. Install Node.js 24 from https://nodejs.org")
        return False
    if not (frontend / "node_modules").is_dir():
        print("Installing the dashboard's packages first (npm install) ...")
        if subprocess.run([npm, "install"], cwd=frontend).returncode != 0:
            return False
    print("Building the dashboard (npm run build) ...")
    return subprocess.run([npm, "run", "build"], cwd=frontend).returncode == 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--build", action="store_true", help="build the dashboard before serving")
    p.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    args = p.parse_args()

    if args.build and not build_dashboard():
        print("The dashboard build failed; fix the errors above, or run without --build to serve the API only.")
        return 1

    settings = get_settings()
    dist = settings.frontend_dist
    db = check_connection()
    if not db["connected"]:
        print(f"WARNING: MongoDB is not reachable ({db.get('error')}). The page will open but every panel will show an error. Start the MongoDB service.")
    if not settings.serve_frontend:
        print("SERVE_FRONTEND is false in .env: serving the API only.")
    elif not (dist / "index.html").is_file():
        print(f"No dashboard build found at {dist}. Serving the API only; run with --build (or `npm run build` in frontend/) to add the dashboard.")

    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"\n  Dashboard:  http://{shown}:{args.port}/\n  API docs:   http://{shown}:{args.port}/docs\n  Stop with Ctrl+C\n")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        key = "set" if settings.api_key else "NOT set"
        print(f"WARNING: listening on {args.host}: anyone on your network can read the data. There is no login for reading; the API key ({key}) "
              "protects only actions that change data. Put authentication or a firewall in front of this.\n")

    import uvicorn

    uvicorn.run("app.main:app", app_dir=str(ROOT / "backend"), host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
