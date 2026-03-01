import random, time, threading, uuid
from artifacts import ArtifactManager

class Player:
    def __init__(self, name, sid):
        self.name = name
        self.sid = sid
        self.found = set()
        self.hints_allowed = 0
        self.hints_used = 0
        self.is_imposter = False
        # score shown to players, based only on artifacts found
        self.score = 0.0
        # internal photo-based score used only for allocating hints
        self.photo_score = 0.0
        self.photo_uploaded = False
        self.ready = False
        self.prompt_word = None

    def to_public(self):
        return {
            "name": self.name,
            "found": len(self.found),
            "hints_left": self.hints_allowed - self.hints_used,
            "hints_used": self.hints_used,
            "hints_allowed": self.hints_allowed,
            "ready": self.ready,
            "prompt_word": self.prompt_word,
            "score": self.score,
            "imposter": self.is_imposter,
        }

class Session:
    def __init__(self, session_id, N_PLAYERS=5, duration_minutes=10):
        self.session_id = session_id
        self.players = {}
        self.started = False
        self.duration = duration_minutes * 60
        self.started_at = None
        self.artifact_manager = ArtifactManager()
        self.imposter = None
        self.votes = {}
        self.N_PLAYERS = N_PLAYERS
        # pending hint requests waiting for an imposter decision:
        # pending_id -> {"requester": str, "room": str, "artifact": Optional[str], "clue": Optional[str]}
        self.pending_hints = {}
        self.world_view_sids = {}
        # imposter powers
        self.manipulations_left = 10
        self.delay_uses = 0

    def to_dict(self):
        total_artifacts = len(self.artifact_manager.ARTIFACTS)
        claimed = len(self.artifact_manager.claimed)
        manips = self.manipulations_left
        return {
            "session_id": self.session_id,
            "players": [p.to_public() for p in self.players.values()],
            "started": self.started,
            "artifacts_total": total_artifacts,
            "artifacts_claimed": claimed,
            "manipulations_left": manips,
        }

class GameManager:
    N_PLAYERS = 5
    FANTASY_WORDS = [
        "Patronus",
        "Horcrux",
        "Quidditch",
        "Azkaban",
        "Hogwarts",
        "Pensieve",
        "Portkey",
        "Phoenix",
        "Elder Wand",
        "Invisibility Cloak",
        "Time-Turner",
        "Dragonfire",
        "Forbidden Forest",
        "Grimmoire",
        "Nimbus 2000",
    ]
    def __init__(self):
        self.sessions = {}

    def create_session(self):
        sid = str(uuid.uuid4())[:8]
        self.sessions[sid] = Session(sid, self.N_PLAYERS)
        return sid

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    @staticmethod
    def _norm(name):
        return (name or "").strip().lower()

    def add_player(self, session_id, username, socket_sid):
        s = self.sessions.get(session_id)
        if not s:
            raise Exception("session not found")
        p = Player(username, socket_sid)
        s.players[username] = p
        return p

    def register_world_view(self, session_id, username, socket_sid):
        s = self.sessions.get(session_id)
        if not s:
            return
        key = self._norm(username)
        if not key:
            return
        user_map = s.world_view_sids.setdefault(key, set())
        user_map.add(socket_sid)

    def unregister_world_view(self, session_id, username, socket_sid):
        s = self.sessions.get(session_id)
        if not s:
            return
        key = self._norm(username)
        if not key:
            return
        user_map = s.world_view_sids.get(key)
        if not user_map:
            return
        user_map.discard(socket_sid)
        if not user_map:
            s.world_view_sids.pop(key, None)

    def world_view_sockets(self, session_id, username):
        key = self._norm(username)
        if not key:
            return set()
        s = self.sessions.get(session_id)
        if not s:
            return set()
        return set(s.world_view_sids.get(key, set()))

    def get_players(self, session_id):
        s = self.sessions.get(session_id)
        return list(s.players.values()) if s else []

    def count_players(self, session_id):
        s = self.sessions.get(session_id)
        return len(s.players) if s else 0

    def mark_player_ready(self, session_id, username):
        s = self.sessions.get(session_id)
        if not s:
            return False
        player = s.players.get(username)
        if not player:
            return False
        player.ready = True
        # all players present and marked ready
        if len(s.players) == self.N_PLAYERS and all(p.ready for p in s.players.values()):
            return True
        return False

    def _assign_prompt_words(self, session):
        # Assign a random fantasy-themed word to each player
        for p in session.players.values():
            p.prompt_word = random.choice(self.FANTASY_WORDS)

    def start_game(self, session_id, socketio):
        s = self.sessions.get(session_id)
        if not s or s.started:
            return
        s.started = True
        # choose imposter
        s.imposter = random.choice(list(s.players.keys()))
        s.players[s.imposter].is_imposter = True
        # generate artifacts placement & clues
        s.artifact_manager.setup_new_session()
        # assign a fantasy-themed prompt word per player
        self._assign_prompt_words(s)
        # send initial payload per player including their prompt word (capture phase)
        for p in s.players.values():
            payload = {
                "imposter": None,
                "session": s.to_dict(),
                "prompt_word": p.prompt_word,
            }
            socketio.emit("game_started", payload, room=p.sid)
        # create initial clues for artifacts (these are stored in artifact_manager)
        s.artifact_manager.generate_all_clues()
        # initialize imposter manipulation/delay counters
        s.manipulations_left = 10
        s.delay_uses = 0

    # --- Interactive hint flow: pending hints & imposter decisions ---

    def start_pending_hint(self, session_id, requester, room, hint=None):
        """
        Create and register a pending hint request for a given session.
        This does NOT emit anything by itself; the caller is expected to
        send a 'hint_prompt' event to the imposter.
        """
        s = self.sessions.get(session_id)
        if not s:
            return None
        if requester not in s.players:
            return None
        pending_id = str(uuid.uuid4())
        artifact = None
        clue = None
        if isinstance(hint, dict):
            artifact = hint.get("artifact")
            clue = hint.get("clue")
        s.pending_hints[pending_id] = {
            "requester": requester,
            "room": room,
            "artifact": artifact,
            "clue": clue,
        }
        return pending_id

    def get_pending_hint(self, session_id, pending_id):
        s = self.sessions.get(session_id)
        if not s:
            return None
        return s.pending_hints.get(pending_id)

    def resolve_pending_hint(self, session_id, pending_id):
        """
        Remove and return a pending hint entry. Returns None if not found.
        """
        s = self.sessions.get(session_id)
        if not s:
            return None
        return s.pending_hints.pop(pending_id, None)

    def start_timer(self, session_id, socketio):
        """Begin the shared 10-minute countdown once all players are in the world view."""
        s = self.sessions.get(session_id)
        if not s:
            return
        if s.started_at is not None:
            # Timer already running
            return
        s.started_at = time.time()
        threading.Thread(target=self._game_timer, args=(session_id, socketio), daemon=True).start()

    def _game_timer(self, session_id, socketio):
        s = self.sessions.get(session_id)
        if not s:
            return
        time_left = s.duration
        while time_left > 0:
            payload = {"time_left": time_left}
            socketio.emit("time_tick", payload, room=session_id)
            # also broadcast to namespace so any listener sees the tick
            socketio.emit("time_tick", payload)
            time.sleep(1)
            time_left = s.duration - int(time.time() - s.started_at)
        # time over
        results = self.end_game(session_id)
        socketio.emit("game_over", results, room=session_id)

    def request_hint(self, session_id, username, room, socketio):
        s = self.sessions.get(session_id)
        if not s:
            return None
        player = s.players.get(username)
        if not player:
            return None
        # Imposter never receives normal hints
        if player.is_imposter:
            return None
        if player.hints_used >= player.hints_allowed:
            return None
        # get a room-aware artifact hint from artifact manager
        hint = None
        if room:
            hint = s.artifact_manager.provide_room_hint(session_id, room, s.players)
        if hint is None:
            # fallback to legacy behaviour if no room-specific hint is available
            hint = s.artifact_manager.provide_hint(session_id, username, socketio, s.imposter, s.players)
        # Only consume a hint from the player's quota if we actually produced
        # a clue for them.
        if hint is not None:
            player.hints_used += 1
        return hint

    def submit_artifact(self, session_id, username, artifact_id):
        s = self.sessions.get(session_id)
        if not s:
            return {"error":"no session"}
        # simple artifact capture: if artifact still available, award to player
        ok = s.artifact_manager.claim_artifact(artifact_id, username)
        if ok:
            player = s.players[username]
            player.found.add(artifact_id)
            player.score += 10.0
            total = len(s.artifact_manager.ARTIFACTS)
            claimed = len(s.artifact_manager.claimed)
            remaining = max(0, total - claimed)
            return {
                "ok": True,
                "message": "claimed",
                "artifact_id": artifact_id,
                "artifacts_remaining": remaining,
                "score": player.score,
            }
        return {"ok":False, "message":"already found or not exist"}

    def vote_imposter(self, session_id, voter, vote_name):
        s = self.sessions.get(session_id)
        if not s:
            return
        s.votes.setdefault(vote_name, 0)
        s.votes[vote_name] += 1

    def get_votes(self, session_id):
        s = self.sessions.get(session_id)
        return s.votes if s else {}

    def all_votes_in(self, session_id):
        s = self.sessions.get(session_id)
        return sum(s.votes.values()) >= len(s.players)

    def tally_votes(self, session_id):
        s = self.sessions.get(session_id)
        if not s or not s.votes:
            return {"voted_imposter": None, "actual_imposter": getattr(s, "imposter", None)}
        top = max(s.votes.items(), key=lambda kv: kv[1])[0]
        return {"voted_imposter": top, "actual_imposter": s.imposter}

    def check_game_end(self, session_id):
        s = self.sessions.get(session_id)
        if not s:
            return True
        # end if all artifacts claimed; timer expiry handled in _game_timer
        return s.artifact_manager.all_claimed()

    def end_game(self, session_id):
        s = self.sessions.get(session_id)
        if not s:
            return {}

        all_claimed = s.artifact_manager.all_claimed()
        vote_info = self.tally_votes(session_id)
        voted = vote_info.get("voted_imposter")
        correct_vote = (voted == s.imposter)

        # Non‑imposters win only if all artifacts are found AND
        # the majority correctly guessed the imposter's name.
        if all_claimed and correct_vote:
            non_imposters = [p for p in s.players.values() if not p.is_imposter]
            # Rank by score (10 points per artifact found)
            ranks = sorted(non_imposters, key=lambda p: p.score, reverse=True)
            winner = ranks[0].name if ranks else None
            return {
                "winner": winner,
                "ranks": [{"name": p.name, "score": p.score} for p in ranks],
                "imposter": s.imposter,
                "voted_imposter": voted,
                "reason": "non-imposters found all artifacts and the imposter",
            }

        # Otherwise, the imposter wins: either time expired, not all artifacts
        # were found, or the majority guessed the wrong name.
        return {
            "winner": s.imposter,
            "imposter": s.imposter,
            "voted_imposter": voted,
            "reason": "imposter wins (not all artifacts found or wrong guess)",
        }

    def record_photo_score(self, session_id, username, score):
        s = self.sessions.get(session_id)
        if not s:
            return None
        p = s.players.get(username)
        if not p:
            return None
        # keep game score at 0 until artifacts are found; use a separate
        # photo_score for hint allocation.
        p.photo_score = float(score or 0.0)
        p.photo_uploaded = True
        # when all players have uploaded, allocate hint quotas
        if len(s.players) == self.N_PLAYERS and all(pl.photo_uploaded for pl in s.players.values()):
            self._allocate_hints(s)
            return True
        return False

    def _allocate_hints(self, session):
        # Distribute a total of 36 hints to non-imposters: 11,10,8,7.
        # The imposter always receives 0 hints.
        quotas = [11, 10, 8, 7]
        players = list(session.players.values())
        imposter = session.players.get(session.imposter) if session.imposter else None
        non_imposters = [p for p in players if not p.is_imposter]
        # Sort non-imposters by photo score (high to low)
        non_imposters_sorted = sorted(non_imposters, key=lambda p: p.photo_score, reverse=True)
        for p, q in zip(non_imposters_sorted, quotas):
            p.hints_allowed = q
        # Imposter gets zero hints
        if imposter:
            imposter.hints_allowed = 0
