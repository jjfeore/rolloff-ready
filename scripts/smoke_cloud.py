"""Read-only smoke test. Never submits a quote or sends email."""
import json
import os
import re
import time
from urllib.error import URLError
from urllib.request import urlopen

hostname = os.environ.get("SITE_HOSTNAME", "")
if not re.fullmatch(r"[a-z0-9-]+\.(?:[a-z0-9-]+\.)?azurestaticapps\.net", hostname):
    raise SystemExit("Unexpected Static Web App hostname.")

for attempt in range(6):
    try:
        with urlopen(f"https://{hostname}/api/config", timeout=15) as response:
            config = json.load(response)
        if not isinstance(config.get("mapsConfigured"), bool) or not isinstance(config.get("emailConfigured"), bool) or not isinstance(config.get("formToken"), str):
            raise ValueError("Unexpected API config shape")
        print(f"API reachable. Maps configured: {config['mapsConfigured']}; email configured: {config['emailConfigured']}.")
        if not config["mapsConfigured"]:
            raise SystemExit("HERE maps is not configured. Run Provision Azure with a valid secret.")
        break
    except (URLError, ValueError, TimeoutError):
        if attempt == 5:
            raise SystemExit("API smoke test failed after deployment. Inspect the deployment and runtime configuration.") from None
        time.sleep(10)
