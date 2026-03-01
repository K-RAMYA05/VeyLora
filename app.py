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
    hint = manager.request_hint(session_id, username, room, socketio)
    if hint is None:
        emit("hint_response", {"error":"No hints left, no clues for this room, or session not found"})
    else:
        # For now, emit the hint directly back to the requester.
        # Interactive imposter manipulation/delay will be layered on top later.
        emit("hint_response", hint)

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
        # All five players have uploaded and been scored:
        # - hint quotas have been allocated
        # - now start the shared 12-minute timer
        manager.start_timer(session_id, socketio)
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
    """
    New interactive hint flow entry point.
    Non‑imposter players call this with: session_id, username, room.
    The server creates a pending hint entry and notifies the imposter
    via a 'hint_prompt' event. The final hint will be sent only after
    the imposter replies with 'imposter_hint_action'.
    """
    session_id = data.get("session_id")
    username = data.get("username")
    room = data.get("room")
    if not session_id or not username or not room:
        emit("hint_response", {"error": "session_id, username and room are required"})
        return

    s = manager.get_session(session_id)
    if not s:
        emit("hint_response", {"error": "session not found"})
        return
    player = s.players.get(username)
    if not player:
        emit("hint_response", {"error": "player not in session"})
        return
    if player.is_imposter:
        emit("hint_response", {"error": "imposter cannot request hints"})
        return

    pending_id = manager.start_pending_hint(session_id, username, room)
    if not pending_id:
        emit("hint_response", {"error": "could not start hint request"})
        return

    # For now, we do not attach artifact/clue here – those will be populated
    # in a later step when we wire room-aware hint selection. We already
    # notify the imposter that someone requested a hint.
    imposter_name = s.imposter
    imposter_player = s.players.get(imposter_name) if imposter_name else None
    prompt_payload = {
        "pending_id": pending_id,
        "session_id": session_id,
        "requester": username,
        "room": room,
        "artifact": None,
        "base_clue": None,
    }
    if imposter_player:
        socketio.emit("hint_prompt", prompt_payload, room=imposter_player.sid)
    else:
        # No imposter (should not happen), fallback: tell requester no hint
        emit("hint_response", {"error": "no imposter available for hint approval"})

@socketio.on("imposter_hint_action")
def on_imposter_hint_action(data):
    """
    Imposter's response to a hint prompt.
    Carries: session_id, pending_id, action ('pass' | 'manipulate' | 'delay'),
    and optional 'clue' override. This handler only validates and clears the
    pending entry for now; the full emission logic will be wired in a later step.
    """
    session_id = data.get("session_id")
    pending_id = data.get("pending_id")
    action = data.get("action")
    override_clue = data.get("clue")

    if not session_id or not pending_id or not action:
        emit("error", {"msg": "session_id, pending_id and action are required"})
        return

    s = manager.get_session(session_id)
    if not s:
        emit("error", {"msg": "session not found"})
        return

    # Make sure the sender is actually the imposter in this session
    imposter_name = s.imposter
    sender = None
    for p in s.players.values():
        if p.sid == request.sid:
            sender = p
            break
    if not sender or sender.name != imposter_name:
        emit("error", {"msg": "only the imposter can act on hints"})
        return

    pending = manager.resolve_pending_hint(session_id, pending_id)
    if not pending:
        emit("error", {"msg": "pending hint not found"})
        return

    # For now we simply acknowledge the decision; in the next step we will
    # actually apply the action to the underlying hint and notify the requester.
    emit("hint_action_ack", {
        "pending_id": pending_id,
        "action": action,
        "session_id": session_id
    }, room=sender.sid)

@socketio.on("join_world_view")
def on_join_world_view(data):
    session_id = data.get("session_id")
    if not session_id:
        return
    # Only join the Socket.IO room for broadcast events, without adding a new player.
    join_room(session_id)

if __name__ == "__main__":
    # Bind to localhost and use a non-default port to avoid conflicts
    port = int(os.getenv("PORT", "5050"))
    # Explicit startup message so you can confirm it's this app
    env_name = os.getenv("CONDA_DEFAULT_ENV", "unknown-env")
    print(f"\n[Arcane Artifact Hunt] Starting on http://127.0.0.1:{port} (env: {env_name})\n")
    socketio.run(app, host="127.0.0.1", port=port)
