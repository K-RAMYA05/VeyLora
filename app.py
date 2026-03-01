import os, time, threading, uuid, random, json
from flask import Flask, render_template, send_from_directory, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from game_logic import GameManager
from dotenv import load_dotenv
import requests

load_dotenv()
PRESAIGE_API_URL = os.getenv("PRESAIGE_API_URL", "")  # set your scoring endpoint here
PRESAIGE_API_KEY = os.getenv("PRESAIGE_API_KEY", "")

app = Flask(__name__, static_folder="static", template_folder="templates")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
manager = GameManager()

def _emit_hint_error(message, target_sid):
    """Utility to send a hint error to a specific client."""
    socketio.emit("hint_response", {"error": message}, room=target_sid)

def _queue_hint_request(session_id, username, room, requester_sid=None):
    """Generate a hint and route it through the imposter for optional manipulation."""
    if requester_sid is None:
        requester_sid = request.sid
    if not room:
        _emit_hint_error("You must be inside a room to request a hint.", requester_sid)
        return
    s = manager.get_session(session_id)
    if not s:
        _emit_hint_error("Session not found.", requester_sid)
        return
    player = s.players.get(username)
    if not player:
        _emit_hint_error("Player not registered in this session.", requester_sid)
        return

    # Generate a hint for this requester/room.
    hint = manager.request_hint(session_id, username, room, socketio)
    if hint is None:
        _emit_hint_error("No hints available for this room or you have no hints left.", requester_sid)
        return
    hint.setdefault("room", room)

    # If there is no imposter for some reason, deliver the hint directly.
    imposter_name = s.imposter
    if not imposter_name:
        final_payload = dict(hint)
        final_payload["requester"] = username
        socketio.emit("hint_response", final_payload, room=session_id)
        return

    # Create a pending entry and broadcast a prompt. Clients will filter so
    # only the imposter sees the prompt UI.
    pending_id = manager.start_pending_hint(session_id, username, room, hint)
    if not pending_id:
        _emit_hint_error("Unable to queue this hint request.", requester_sid)
        return
    manips_left = getattr(s, "manipulations_left", 10)
    prompt_payload = {
        "pending_id": pending_id,
        "session_id": session_id,
        "requester": username,
        "room": room,
        "artifact": hint.get("artifact"),
        "base_clue": hint.get("clue"),
        "manipulations_left": manips_left,
        "imposter": imposter_name,
    }
    # Send to the session room so all world views in this session see it…
    socketio.emit("hint_prompt", prompt_payload, room=session_id)
    # …and also broadcast to the namespace as a safety net so the
    # imposter's tab receives it even if it missed the room join.
    socketio.emit("hint_prompt", prompt_payload)

@app.route("/")
def index():
    return send_from_directory('static', 'index.html')

@app.route("/image/<path:filename>")
def image_file(filename):
    # Serve fantasy world and other game images from the image directory
    return send_from_directory("image", filename)

# REST endpoints for quick testing
@app.route("/create_session", methods=["POST"])
def create_session():
    session_id = manager.create_session()
    print(f"[create_session] New session: {session_id}")
    return jsonify({"session_id": session_id})

@app.route("/status/<session_id>")
def status(session_id):
    s = manager.get_session(session_id)
    if not s:
        return jsonify({"error":"not found"}), 404
    return jsonify(s.to_dict())

@app.route("/pending_hint", methods=["GET"])
def pending_hint():
    """
    HTTP fallback for the imposter: returns the first pending hint
    for this session so the imposter overlay can poll even if the
    real-time Socket.IO event was missed.
    """
    session_id = request.args.get("session_id")
    username = request.args.get("username")
    if not session_id or not username:
        return jsonify({"error": "session_id and username required"}), 400
    s = manager.get_session(session_id)
    if not s:
        return jsonify({"error": "session not found"}), 404
    if username != s.imposter:
        return jsonify({"error": "only the imposter can view pending hints"}), 403
    entry = manager.first_pending_for_imposter(session_id)
    if not entry:
        return jsonify({})
    payload = {
        "pending_id": entry.get("pending_id"),
        "session_id": session_id,
        "requester": entry.get("requester"),
        "room": entry.get("room"),
        "artifact": entry.get("artifact"),
        "base_clue": entry.get("clue"),
        "manipulations_left": getattr(s, "manipulations_left", 10),
        "imposter": s.imposter,
    }
    return jsonify(payload)

# SocketIO events
@socketio.on("join_session")
def on_join(data):
    username = data.get("username")
    session_id = data.get("session_id")
    sid = request.sid
    if not username or not session_id:
        emit("error", {"msg":"username and session_id required"})
        return
    player = manager.add_player(session_id, username, sid)
    join_room(session_id)
    emit("joined", {"player": player.to_public()}, room=sid)
    socketio.emit("player_list", {"players":[p.to_public() for p in manager.get_players(session_id)]}, room=session_id)

@socketio.on("request_hint")
def on_hint(data):
    session_id = data.get("session_id")
    username = data.get("username")
    room = data.get("room")
    if not room:
        emit("hint_response", {"error": "You must be inside a room to request a hint."})
        return
    _queue_hint_request(session_id, username, room, requester_sid=request.sid)

@socketio.on("submit_artifact")
def on_submit(data):
    # data: session_id, username, artifact_id
    session_id = data.get("session_id")
    username = data.get("username")
    artifact_id = data.get("artifact_id")
    res = manager.submit_artifact(session_id, username, artifact_id)
    socketio.emit("artifact_update", {"result": res, "username": username}, room=session_id)
    # Check end condition
    if manager.check_game_end(session_id):
        results = manager.end_game(session_id)
        socketio.emit("game_over", results, room=session_id)

@socketio.on("vote_imposter")
def on_vote(data):
    session_id = data.get("session_id")
    voter = data.get("username")
    vote = data.get("vote")  # name of suspected player
    manager.vote_imposter(session_id, voter, vote)
    socketio.emit("votes", {"votes": manager.get_votes(session_id)}, room=session_id)
    if manager.all_votes_in(session_id):
        results = manager.tally_votes(session_id)
        socketio.emit("vote_results", results, room=session_id)
        if manager.check_game_end(session_id):
            results = manager.end_game(session_id)
            socketio.emit("game_over", results, room=session_id)

def _score_image_with_presaige(file_storage):
    if not PRESAIGE_API_URL or not PRESAIGE_API_KEY:
        # Fallback: simple pseudo-score if API is not configured
        return random.uniform(0, 100)
    try:
        # Mirror the Presaige curl example: JSON body with filename + content_type,
        # API key passed via x-api-key header.
        payload = {
            "filename": file_storage.filename,
            "content_type": file_storage.mimetype or "image/jpeg",
        }
        headers = {
            # As per docs: -H "x-api-key: YOUR_API_KEY" -H "Content-Type: application/json"
            "x-api-key": PRESAIGE_API_KEY,
            "Accept": "application/json",
        }
        resp = requests.post(PRESAIGE_API_URL, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        # Try a few common locations for a numeric score
        score = data.get("score")
        if score is None and isinstance(data, dict):
            inner = data.get("data") or data.get("result") or {}
            score = inner.get("score") or inner.get("quality_score")
        if score is None:
            # fallback if shape is unexpected
            score = random.uniform(0, 100)
        return float(score)
    except Exception as e:
        print("Presaige scoring failed:", e)
        return random.uniform(0, 100)

@app.route("/upload_photo", methods=["POST"])
def upload_photo():
    session_id = request.form.get("session_id")
    username = request.form.get("username")
    if "photo" not in request.files:
        return jsonify({"error": "photo file required"}), 400
    if not session_id or not username:
        return jsonify({"error": "session_id and username required"}), 400
    photo = request.files["photo"]
    score = _score_image_with_presaige(photo)
    all_scored = manager.record_photo_score(session_id, username, score)
    result = {"ok": True, "score": score}
    # When all 5 players have uploaded and hints are allocated, broadcast quotas
    if all_scored:
        players = [p.to_public() for p in manager.get_players(session_id)]
        socketio.emit(
            "hints_ready",
            {"players": players},
            room=session_id,
        )
        # All five players have uploaded and been scored and hint quotas allocated.
        # The actual game countdown will start only after everyone loads the
        # fantasy_world view (handled in the join_world_view handler).
        result["hints_assigned"] = True
    else:
        result["hints_assigned"] = False
    return jsonify(result)

@socketio.on("player_ready")
def on_player_ready(data):
    session_id = data.get("session_id")
    username = data.get("username")
    if not username or not session_id:
        emit("error", {"msg": "username and session_id required"})
        return
    all_ready = manager.mark_player_ready(session_id, username)
    # broadcast updated readiness state for all players
    socketio.emit(
        "player_ready_state",
        {"players": [p.to_public() for p in manager.get_players(session_id)]},
        room=session_id,
    )
    # start game only when all 5 players have clicked start
    if all_ready:
        manager.start_game(session_id, socketio)

@socketio.on("request_hint_interactive")
def on_request_hint_interactive(data):
    """Alias for the standard hint request flow that routes via the imposter."""
    session_id = data.get("session_id")
    username = data.get("username")
    room = data.get("room")
    if not session_id or not username or not room:
        emit("hint_response", {"error": "session_id, username and room are required"})
        return

    _queue_hint_request(session_id, username, room, requester_sid=request.sid)

@socketio.on("imposter_hint_action")
def on_imposter_hint_action(data):
    """
    Imposter's response to a hint prompt.
    Carries: session_id, pending_id, action ('pass' | 'manipulate'),
    and optional 'clue' override. Applies the requested action, delivers the
    final hint to the requesting player, and updates manipulation counters.
    """
    session_id = data.get("session_id")
    pending_id = data.get("pending_id")
    action = data.get("action")
    override_clue = data.get("clue")
    username = data.get("username")

    if not session_id or not pending_id or not action:
        emit("error", {"msg": "session_id, pending_id and action are required"})
        return

    s = manager.get_session(session_id)
    if not s:
        emit("error", {"msg": "session not found"})
        return

    # Make sure the sender is actually the imposter in this session by name
    imposter_name = s.imposter
    if not imposter_name or (username and username != imposter_name):
        socketio.emit(
            "hint_action_ack",
            {"error": "Only the imposter can act on hints.", "session_id": session_id},
            room=request.sid,
        )
        return

    preview = manager.get_pending_hint(session_id, pending_id)
    if not preview:
        socketio.emit(
            "hint_action_ack",
            {"error": "Pending hint not found.", "session_id": session_id, "pending_id": pending_id},
            room=request.sid,
        )
        return

    action = action.lower()
    if action not in ("pass", "manipulate"):
        socketio.emit(
            "hint_action_ack",
            {"error": "Unknown hint action.", "pending_id": pending_id, "session_id": session_id},
            room=request.sid,
        )
        return

    manips_left = getattr(s, "manipulations_left", 10)
    if action == "manipulate" and manips_left <= 0:
        socketio.emit(
            "hint_action_ack",
            {
                "error": "No manipulations remaining.",
                "pending_id": pending_id,
                "session_id": session_id,
                "manipulations_left": manips_left,
            },
            room=request.sid,
        )
        return

    pending = manager.resolve_pending_hint(session_id, pending_id)
    if not pending:
        socketio.emit(
            "hint_action_ack",
            {"error": "Pending hint missing.", "pending_id": pending_id, "session_id": session_id},
            room=request.sid,
        )
        return

    final_clue = pending.get("clue") or ""
    manipulated = False
    if action == "manipulate":
        manipulated = True
        final_clue = override_clue if override_clue is not None else ""
        s.manipulations_left = max(0, manips_left - 1)
        manips_left = s.manipulations_left

    requester_name = pending.get("requester")
    hint_payload = {
        "artifact": pending.get("artifact"),
        "clue": final_clue,
        "room": pending.get("room"),
        "manipulated": manipulated,
        "requester": requester_name,
    }
    # Broadcast to the session; clients filter so only the requester processes it.
    socketio.emit("hint_response", hint_payload, room=session_id)

    socketio.emit(
        "hint_action_ack",
        {
            "pending_id": pending_id,
            "action": action,
            "session_id": session_id,
            "manipulations_left": manips_left,
        },
        room=request.sid,
    )

@socketio.on("join_world_view")
def on_join_world_view(data):
    session_id = data.get("session_id")
    username = data.get("username")
    if not session_id or not username:
        return
    join_room(session_id)
    manager.register_world_view(session_id, username, request.sid)
    # Ensure the shared countdown timer (10 minutes) is running as soon as
    # the fantasy world view is active for this session. The GameManager
    # will ignore duplicate start requests once the timer is running.
    manager.start_timer(session_id, socketio)

@socketio.on("disconnect")
def on_disconnect():
    # remove this socket from any world-view tracking
    sid = request.sid
    for session_id, session in manager.sessions.items():
        for username, sockets in list(session.world_view_sids.items()):
            if sid in sockets:
                sockets.discard(sid)
                if not sockets:
                    session.world_view_sids.pop(username, None)

if __name__ == "__main__":
    # Bind to localhost and use a non-default port to avoid conflicts
    port = int(os.getenv("PORT", "5050"))
    # Explicit startup message so you can confirm it's this app
    env_name = os.getenv("CONDA_DEFAULT_ENV", "unknown-env")
    print(f"\n[Arcane Artifact Hunt] Starting on http://127.0.0.1:{port} (env: {env_name})\n")
    socketio.run(app, host="127.0.0.1", port=port)
