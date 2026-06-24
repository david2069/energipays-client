"""
Get fresh Energipays tokens from a logged-in Chrome tab via Chrome DevTools Protocol.
Chrome must be running with --remote-debugging-port=9222 (already configured on this machine).
No user interaction required — reads directly from localStorage.
"""
from __future__ import annotations
import json, pathlib, urllib.request

CDP_URL = "http://localhost:9222"
TOKEN_CACHE = pathlib.Path(__file__).parent / ".token_cache.json"


def _find_energipays_tab() -> str | None:
    """Return the webSocketDebuggerUrl for the first energipays.com tab."""
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json", timeout=3) as resp:
            tabs = json.loads(resp.read())
        for tab in tabs:
            if "energipays.com" in tab.get("url", ""):
                return tab["id"], tab["webSocketDebuggerUrl"]
    except Exception:
        pass
    return None, None


def get_tokens(save: bool = True) -> dict:
    """
    Pull localStorage.tokens from the active Chrome energipays tab.
    Returns {"access_token": ..., "refresh_token": ..., ...}.
    Raises RuntimeError if Chrome or the tab is not found.
    """
    try:
        import pychrome
    except ImportError:
        raise RuntimeError("pychrome not installed — run: pip install pychrome")

    tab_id, ws_url = _find_energipays_tab()
    if not tab_id:
        raise RuntimeError(
            "No energipays.com tab found in Chrome. "
            "Open https://energipays.com in Chrome and log in."
        )

    tab = pychrome.Tab(id=tab_id, type="page", url="", webSocketDebuggerUrl=ws_url)
    tab.start()
    try:
        result = tab.Runtime.evaluate(
            expression="localStorage.tokens || '{}'",
            returnByValue=True,
        )
        tokens = json.loads(result["result"]["value"])
    finally:
        tab.stop()

    if save and tokens.get("access_token"):
        TOKEN_CACHE.write_text(json.dumps({
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
        }))

    return tokens


if __name__ == "__main__":
    t = get_tokens()
    print(f"access_token:  {t.get('access_token','')[:60]}...")
    print(f"refresh_token: {t.get('refresh_token','')[:40]}...")
    print(f"expires_in:    {t.get('expires_in')}s")
    print(f"Saved to {TOKEN_CACHE}")
