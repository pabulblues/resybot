# One-off: run the sniper immediately for an already-released date.
# Usage: python book_now.py [--live]
import logging, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from resybot.config import Config, Secrets
from resybot.notify import Notifier
from resybot.release import Drop
from resybot.resy import ResyClient
from resybot.sniper import Sniper

live = "--live" in sys.argv
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
cfg = Config.load()
cfg.poll_duration_s = 5
t = cfg.targets[0]
sec = Secrets.from_env()
client = ResyClient(sec.resy_auth_token, sec.resy_api_key, dry_run=not live)
drop = Drop(at=datetime.now(ZoneInfo(cfg.timezone)), dates=tuple(t.dates))
b = Sniper(cfg, t, drop, client, Notifier(None)).run()
print("RESULT:", b)
client.close()
