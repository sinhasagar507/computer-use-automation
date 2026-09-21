"""Minimal operator page (mock of a real operator console).

Shows who owns the session, the pending intervention request with its screenshot and reason,
the human's recorded actions, and a single "Hand control back" button. The human performs the
manual steps in the live (headed) browser window itself; this page only carries context and
the control-transfer signal.
"""

from __future__ import annotations

import threading

from flask import Flask, jsonify, redirect, send_from_directory
from werkzeug.serving import make_server

_PAGE = """<!doctype html><html><head><title>CUA Operator</title><meta http-equiv="refresh" content="3">
<style>body{font-family:system-ui;margin:24px;max-width:900px} .own{padding:8px 12px;border-radius:6px;display:inline-block}
.human{background:#ffe9a8}.automation{background:#d7f5d7} pre{background:#f4f4f4;padding:8px;overflow:auto} img{max-width:100%;border:1px solid #ccc}
button{font-size:16px;padding:10px 18px}</style></head><body>
<h2>Operator console (mock)</h2>
<p>Session owner: <span class="own %(owner)s">%(owner)s</span></p>
%(body)s
<h3>Handoff history</h3><pre>%(history)s</pre>
</body></html>"""

_REQ = """<h3>Intervention requested</h3>
<table><tr><td><b>Goal</b></td><td>%(goal)s</td></tr><tr><td><b>Step</b></td><td>%(step_index)s</td></tr>
<tr><td><b>Reason</b></td><td>%(reason)s</td></tr><tr><td><b>URL</b></td><td>%(url)s</td></tr></table>
<p>Use the live browser window to fix the situation, then:</p>
<form method="post" action="/handback"><button>Hand control back to automation</button></form>
<h3>Screen at escalation</h3>%(shot)s
<h3>Your recorded actions</h3><pre>%(actions)s</pre>"""


def start_operator_ui(controller, port: int = 5055) -> None:
    app = Flask("cua-operator")

    @app.route("/")
    def index():
        st = controller.state()
        req = st["request"]
        if req:
            shot = f'<img src="/evidence/{req["screenshot"]}">' if req.get("screenshot") else "(no screenshot)"
            actions = "\n".join(f"{a.get('event')}: {a.get('label') or a.get('name') or a.get('url') or ''} {a.get('value', '')}"
                                for a in st["human_actions"]) or "(none yet)"
            body = _REQ % {**req, "shot": shot, "actions": actions}
        else:
            body = "<p>No intervention pending. Automation owns the session.</p>"
        hist = "\n".join(str(h) for h in st["history"]) or "(none)"
        return _PAGE % {"owner": st["owner"], "body": body, "history": hist}

    @app.route("/api/state")
    def state():
        return jsonify(controller.state())

    @app.route("/handback", methods=["POST"])
    def handback():
        controller.hand_back()
        return redirect("/")

    @app.route("/evidence/<path:name>")
    def evidence(name):
        return send_from_directory(str(controller.log.dir), name)

    srv = make_server("127.0.0.1", port, app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"operator console: http://127.0.0.1:{port}/")
