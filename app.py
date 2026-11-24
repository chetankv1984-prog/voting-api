import os
import re
import secrets
import threading
from uuid import uuid4
from flask import Flask, request, jsonify, g, send_from_directory
from itsdangerous import URLSafeSerializer
from flask_socketio import SocketIO, emit

# Input limits
POLL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_QUESTION_LEN = 300
MAX_OPTION_LEN = 100
MAX_OPTIONS = 20

# App config
app = Flask(__name__, static_folder="public", static_url_path="")
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    # No hardcoded fallback: a known key would let anyone forge voter cookies.
    # A random key means voter cookies reset on restart (polls are in-memory anyway).
    _secret = secrets.token_hex(32)
    print("WARNING: SECRET_KEY not set; using a random key for this run.")
app.config["SECRET_KEY"] = _secret
serializer = URLSafeSerializer(app.config["SECRET_KEY"], salt="uid-salt")

# Threading mode + simple-websocket: works on current Python versions (eventlet is deprecated
# and doesn't support Python 3.13+). For production, use a proper WSGI server setup.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# In-memory polls store (for demo; swap with DB for production)
polls = {
    "demo": {
        "id": "demo",
        "question": "What’s your favorite JS framework?",
        "options": ["React", "Vue", "Angular", "Svelte"],
        "votes": [0, 0, 0, 0],
        "voters": set()  # set of raw uid strings
    },
    "snack": {
        "id": "snack",
        "question": "Best snack while coding?",
        "options": ["Chips", "Chocolate", "Fruit", "Coffee only", "Samosa"],
        "votes": [0, 0, 0, 0, 0],
        "voters": set()
    },
    "tabs-vs-spaces": {
        "id": "tabs-vs-spaces",
        "question": "Tabs or spaces?",
        "options": ["Tabs", "Spaces", "Whatever the formatter says"],
        "votes": [0, 0, 0],
        "voters": set()
    },
}
# Guards polls against concurrent requests (threading async mode)
polls_lock = threading.Lock()
# Number of open Socket.IO connections (shown as "people online")
online_count = 0

# Helpers ------------------------------------------------
def serialize_poll(poll, uid=None):
    return {
        "id": poll["id"],
        "question": poll["question"],
        "options": poll["options"],
        "votes": list(poll["votes"]),
        "totalVoters": len(poll["voters"]),
        "youVoted": (uid in poll["voters"]) if uid else False
    }

# Cookie / UID management -------------------------------
@app.before_request
def ensure_uid():
    signed = request.cookies.get("uid")
    uid = None
    if signed:
        try:
            uid = serializer.loads(signed)
        except Exception:
            uid = None
    if not uid:
        # create a new raw uid; we will set a signed cookie in after_request
        uid = uuid4().hex
        g._set_signed_uid = serializer.dumps(uid)
    else:
        g._set_signed_uid = None
    g.uid = uid

@app.after_request
def set_uid_cookie(resp):
    if getattr(g, "_set_signed_uid", None):
        resp.set_cookie(
            "uid",
            g._set_signed_uid,
            httponly=True,
            samesite="Lax",
            max_age=60 * 60 * 24 * 365
        )
    return resp

# Routes -------------------------------------------------
@app.route("/")
def index():
    # serve frontend
    return send_from_directory("public", "index.html")

@app.route("/api/polls", methods=["GET"])
def get_polls():
    result = [serialize_poll(p, g.uid) for p in polls.values()]
    return jsonify(result)

@app.route("/api/polls/<pid>", methods=["GET"])
def get_poll(pid):
    poll = polls.get(pid)
    if not poll:
        return jsonify({"error": "Poll not found"}), 404
    return jsonify(serialize_poll(poll, g.uid))

@app.route("/api/polls", methods=["POST"])
def create_poll():
    body = request.get_json(silent=True) or {}
    pid = body.get("id")
    question = body.get("question")
    options = body.get("options")

    if not isinstance(pid, str) or not POLL_ID_RE.fullmatch(pid):
        return jsonify({"error": "id must be 1-64 characters: letters, digits, '-' or '_'"}), 400
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_LEN:
        return jsonify({"error": f"question must be a non-empty string (max {MAX_QUESTION_LEN} chars)"}), 400
    if not isinstance(options, list) or not (2 <= len(options) <= MAX_OPTIONS):
        return jsonify({"error": f"options must be a list of 2-{MAX_OPTIONS} strings"}), 400
    if not all(isinstance(o, str) and o.strip() and len(o) <= MAX_OPTION_LEN for o in options):
        return jsonify({"error": f"each option must be a non-empty string (max {MAX_OPTION_LEN} chars)"}), 400
    options = [o.strip() for o in options]
    if len({o.lower() for o in options}) != len(options):
        return jsonify({"error": "options must be unique"}), 400

    with polls_lock:
        if pid in polls:
            return jsonify({"error": "Poll already exists"}), 409
        polls[pid] = {
            "id": pid,
            "question": question.strip(),
            "options": options,
            "votes": [0] * len(options),
            "voters": set()
        }
        result = serialize_poll(polls[pid], g.uid)
    # Let every open page add the new poll to its list in real time
    socketio.emit("poll:created", serialize_poll(polls[pid]))
    return jsonify(result), 201

@app.route("/api/polls/<pid>/vote", methods=["POST"])
def vote_poll(pid):
    poll = polls.get(pid)
    if not poll:
        return jsonify({"error": "Poll not found"}), 404

    body = request.get_json(silent=True) or {}
    idx = body.get("option")
    # Must be a real integer (bool is a subclass of int, so exclude it explicitly)
    if not isinstance(idx, int) or isinstance(idx, bool):
        return jsonify({"error": "Invalid option index"}), 400

    if idx < 0 or idx >= len(poll["options"]):
        return jsonify({"error": "Invalid option index"}), 400

    uid = g.uid
    with polls_lock:
        if uid in poll["voters"]:
            return jsonify({"error": "You have already voted"}), 409
        poll["votes"][idx] += 1
        poll["voters"].add(uid)
        # lastVote lets clients show a live "someone voted for X" activity feed
        payload = {"id": poll["id"], "poll": serialize_poll(poll), "lastVote": idx}
        result = serialize_poll(poll, uid)

    socketio.emit("poll:update", payload)  # emits to all clients outside a socket handler
    return jsonify(result)

# Socket events ------------------------------------------
@socketio.on("connect")
def on_connect():
    # send current polls (no 'youVoted' flag here — client will fetch its own poll for that)
    global online_count
    with polls_lock:
        current = [serialize_poll(p) for p in polls.values()]
        online_count += 1
        count = online_count
    emit("poll:bootstrap", current)
    socketio.emit("presence", {"online": count})

@socketio.on("disconnect")
def on_disconnect(*args):
    global online_count
    with polls_lock:
        online_count = max(0, online_count - 1)
        count = online_count
    socketio.emit("presence", {"online": count})

# Run ---------------------------------------------------
if __name__ == "__main__":
    # Defaults are safe for local dev: localhost only, debug off.
    # Override with HOST=0.0.0.0 (to allow other devices), PORT, FLASK_DEBUG=1.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
