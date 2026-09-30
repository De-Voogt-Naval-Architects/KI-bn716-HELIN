"""Print what the fake Helin Data Collector (helindatacollector service) has received."""

import json
import ssl
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
with urllib.request.urlopen("https://127.0.0.1:6684/stats", context=ctx, timeout=10) as r:
    stats = json.load(r)

per_asset = stats.pop("per_asset")
print(f"posts {stats['posts']}, readings {stats['readings']}, assets {stats['assets']}")
for name, n in stats.get("north", {}).items():
    err = f"  last error: {n['last_error']}" if n["last_error"] else ""
    print(f"north {name:<10} written {n['written']:>9}  backlog {n['backlog']:>7}  purged {n['purged']}{err}")
print(f"last reading: {json.dumps(stats['last'])}")
for asset, n in list(per_asset.items())[:12]:
    print(f"{n:>9}  {asset}")
if len(per_asset) > 12:
    print(f"      ... {len(per_asset) - 12} more assets")
