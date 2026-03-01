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
        self.score = 0.0
        self.photo_uploaded = False
        self.ready = False
        self.prompt_word = None

    def to_public(self):
        return {
            "name": self.name,
            "found": len(self.found),
            "hints_left": self.hints_allowed - self.hints_used,
            "ready": self.ready,
            "prompt_word": self.prompt_word,
            "score": self.score,
        }

class Session:
    def __init__(self, session_id, N_PLAYERS=5, duration_minutes=12):
        self.session_id = session_id
        self.players = {}
        self.started = False
        self.duration = duration_minutes * 60
        self.started_at = None
        self.artifact_manager = ArtifactManager()
        self.imposter = None
        self.votes = {}
        self.N_PLAYERS = N_PLAYERS

    def to_dict(self):
        total_artifacts = len(self.artifact_manager.ARTIFACTS)
        claimed = len(self.artifact_manager.claimed)
        return {
            "session_id": self.session_id,
            "players": [p.to_public() for p in self.players.values()],
            "started": self.started,
            "artifacts_total": total_artifacts,
            "artifacts_claimed": claimed,
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

    def add_player(self, session_id, username, socket_sid):
        s = self.sessions.get(session_id)
        if not s:
            raise Exception("session not found")
        p = Player(username, socket_sid)
        s.players[username] = p
        return p

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

    def start_timer(self, session_id, socketio):
        """Begin the shared 12-minute countdown once all photos are scored."""
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
        time_left = s.duration
        while time_left > 0:
            socketio.emit("time_tick", {"time_left": time_left}, room=session_id)
            time.sleep(1)
            time_left = s.duration - int(time.time() - s.started_at)
        # time over
        results = self.end_game(session_id)
        socketio.emit("game_over", results, room=session_id)

    def request_hint(self, session_id, username, socketio):
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
        # get an artifact hint from artifact manager (this will handle impostor manipulation)
        hint = s.artifact_manager.provide_hint(session_id, username, socketio, s.imposter, s.players)
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
        if not s:
            return {}
        top = max(s.votes.items(), key=lambda kv: kv[1])[0]
        return {"voted_imposter": top, "actual_imposter": s.imposter}

    def check_game_end(self, session_id):
        s = self.sessions.get(session_id)
        # end if all artifacts claimed or time up handled elsewhere
        return s.artifact_manager.all_claimed()

    def end_game(self, session_id):
        s = self.sessions.get(session_id)
        # determine winner
        all_claimed = s.artifact_manager.all_claimed()
        # decide voted impostor
        voted = self.tally_votes(session_id).get("voted_imposter", None)
        correct_vote = (voted == s.imposter)
        if all_claimed and correct_vote:
            # non-impostor with most found wins
            ranks = sorted([p for p in s.players.values() if not p.is_imposter], key=lambda p: len(p.found), reverse=True)
            winner = ranks[0].name if ranks else None
            return {"winner": winner, "ranks": [{p.name: len(p.found)} for p in ranks], "imposter": s.imposter}
        else:
            return {"winner": s.imposter, "imposter": s.imposter, "reason": "imposter wins (not all found or wrong vote)"}

    def record_photo_score(self, session_id, username, score):
        s = self.sessions.get(session_id)
        if not s:
            return None
        p = s.players.get(username)
        if not p:
            return None
        p.score = float(score or 0.0)
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
        non_imposters_sorted = sorted(non_imposters, key=lambda p: p.score, reverse=True)
        for p, q in zip(non_imposters_sorted, quotas):
            p.hints_allowed = q
        # Imposter gets zero hints
        if imposter:
            imposter.hints_allowed = 0
