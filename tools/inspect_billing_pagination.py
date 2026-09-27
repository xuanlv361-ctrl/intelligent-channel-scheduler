"""Inspect only pagination controls in the authenticated domestic-UAT billing page."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import websocket

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.domestic_uat_chrome_manager import DomesticUatChromeManager


def main() -> None:
    manager = DomesticUatChromeManager()
    target = next(item for item in manager._request_json("/json/list") if
                  urlsplit(str(item.get("url") or "")).path ==
                  "/console/billing/logs")
    socket = websocket.create_connection(str(target["webSocketDebuggerUrl"]),
                                         timeout=20, suppress_origin=True)
    try:
        socket.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": """JSON.stringify(Array.from(document.querySelectorAll('button,[role=button],li,.semi-page-item')).map((b,i)=>({i,tag:b.tagName,text:(b.innerText||'').trim(),aria:b.getAttribute('aria-label'),title:b.getAttribute('title'),disabled:Boolean(b.disabled)||b.getAttribute('aria-disabled')==='true',cls:String(b.className||'')})).filter(x=>x.text||x.aria||x.title||/page|pagination/i.test(x.cls)))""",
            "returnByValue": True,
        }}))
        while True:
            result = json.loads(socket.recv())
            if result.get("id") == 1:
                raw = (((result.get("result") or {}).get("result") or {})
                       .get("value") or "[]")
                print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
                return
    finally:
        socket.close()


if __name__ == "__main__":
    main()
