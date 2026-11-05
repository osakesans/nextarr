"""Entry point: start the web server and the scheduler."""
import os
import threading

from . import __version__, config, discordbot, store, web
from .engine import Engine


def main():
    port = int(os.environ.get("NEXTARR_PORT", "8790"))
    cfg = config.load()
    store.log(f"Nextarr {__version__} starting on port {port} (dry run: {cfg['dry_run']})", "info")
    engine = Engine()
    threading.Thread(target=engine.run_forever, daemon=True).start()
    threading.Thread(target=discordbot.Bot(engine).run_forever, daemon=True).start()
    web.serve(engine, port)


if __name__ == "__main__":
    main()
