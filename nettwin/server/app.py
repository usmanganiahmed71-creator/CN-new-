"""Tiny Flask server that gives the clients something real to talk to.

Run on the srv host:  python3 -m nettwin.server.app
"""
import os
import socket
import threading
import time

from flask import Flask, jsonify

from nettwin import netmodel


HEARTBEAT_PATH = "/tmp/nettwin-server-health"


def _write_heartbeat():
    """Publish liveness where the controller can see it outside the namespace."""
    while True:
        temporary_path = HEARTBEAT_PATH + ".tmp"
        try:
            with open(temporary_path, "w") as heartbeat:
                heartbeat.write(str(time.time()))
            os.replace(temporary_path, HEARTBEAT_PATH)
        except OSError:
            pass
        time.sleep(1)


def create_app():
    app = Flask(__name__)

    @app.route("/")
    def index():
        return jsonify(message="NetTwin server is alive", host=socket.gethostname())

    @app.route("/health")
    def health():
        return jsonify(status="ok")

    return app


if __name__ == "__main__":
    # 0.0.0.0: listen on every address of the host, so 10.0.0.100 works.
    threading.Thread(target=_write_heartbeat, daemon=True).start()
    create_app().run(host="0.0.0.0", port=netmodel.SERVER_PORT, threaded=True)
