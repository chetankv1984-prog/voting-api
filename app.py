import os
from uuid import uuid4
from flask import Flask, request, jsonify, g, send_from_directory
from itsdangerous import URLSafeSerializer
from flask_socketio import SocketIO, emit

# App config
app = Flask(__name__, static_folder="public", static_url_path="")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret")
serializer = URLSafeSerializer(app.config["SECRET_KEY"], salt="uid-salt")

# Use eventlet (recommended) or gevent in production
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# In-memory polls store (for demo; swap with DB for production)
polls = {
    "demo": {
        "id": "demo",
        "question": "What’s your favorite JS framework?",
        "options": ["React", "Vue", "Angular", "Svelte"],
        "votes": [0, 0, 0, 0],
        "voters": set()  # set of raw uid strings
    }
}

# Helpers ------------------------------------------------
def serialize_poll(poll, uid=None):
    return {
        "id": poll["id"],
        "question": poll["question"],
        "options": poll["options"],
        "votes": poll["votes"],
        "totalVoters": len(poll["voters"]),
        "youVoted": (uid in poll["vote rs"]) if uid else False
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
    if not pid or not question or not isinstance(options, list) or len(options) < 2:
        return jsonify({"error": "id, question and at least two options required"}), 400
    if pid in polls:
        return jsonify({"error": "Poll already exists"}), 409
    polls[pid] = {
        "id": pid,
        "question": question,
        "options": options,
        "votes": [0] * len(options),
        "voters": set()
    }
    return jsonify(serialize_poll(polls[pid], g.uid)), 201

@app.route("/api/polls/<pid>/vote", methods=["POST"])
def vote_poll(pid):
    poll = polls.get(pid)
    if not poll:
        return jsonify({"error": "Poll not found"}), 404

    body = request.get_json(silent=True) or {}
    option_index = body.get("option")
    try:
        idx = int(option_index)
    except Exception:
        return jsonify({"error": "Invalid option index"}), 400

    if idx < 0 or idx >= len(poll["options"]):
        return jsonify({"error": "Invalid option index"}), 400

    uid = g.uid
    if uid in poll["voters"]:
        return jsonify({"error": "You have already voted"}), 409

    poll["votes"][idx] += 1
    poll["voters"].add(uid)

    payload = {"id": poll["id"], "poll": serialize_poll(poll)}
    socketio.emit("poll:update", payload, broadcast=True)

    return jsonify(serialize_poll(poll, uid))

# Socket events ------------------------------------------
@socketio.on("connect")
def on_connect():
    # send current polls (no 'youVoted' flag here — client will fetch its own poll for that)
    current = [serialize_poll(p) for p in polls.values()]
    emit("poll:bootstrap", current)

# Run ---------------------------------------------------
if __name__ == "__main__":
    # default port 5000; change via PORT env var if you want
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=True)
