"""Read JSON from the HOT Tasking Manager API."""

import json
import os
import urllib.error
import urllib.request

TM_API = os.environ.get("TM_API") or "https://tasking-manager-production-api.hotosm.org/api/v2"


class TaskingManagerError(Exception):
    """The Tasking Manager API returned something unusable."""


def fetch_json(url: str, timeout: int) -> dict:
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise TaskingManagerError(f"{url}: {error}") from error
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise TaskingManagerError(f"{url}: {error}") from error
