import random, json, time, threading, os

class ArtifactManager:
    ROOMS = ["Great Hall","Moon Library","Crystal Conservatory","Dungeon Labs","Skyward Tower","Fae Garden","Astral Observatory","Mirror Wing"]
    ARTIFACTS = [
        "Phoenix Feather","Dragon Scale","Moonstone Amulet","Spellbound Quill","Wand of Whisper","Elder Grimoire",
        "Goblet of Echoes","Starlight Compass","Cloak of Mist","Runed Lantern","Time-Twined Hourglass","Singing Chalice"
    ]

    def __init__(self):
        self.artifact_locations = {}  # artifact -> room
        self.artifact_clues = {}      # artifact -> [clue1, clue2, clue3]
        self.claimed = set()
        # manipulation tracking per session
        self.manipulations_left = {}  # session_id -> remaining manipulations by imposter
        self.delays_used = {}         # session_id -> delay uses count

    def setup_new_session(self):
        # randomly place artifacts in rooms; each room fewer than 4 artifacts by design (<=3)
        self.artifact_locations = {}
        rooms = self.ROOMS.copy()
        placements = {r: [] for r in rooms}
        artifacts = self.ARTIFACTS.copy()
        random.shuffle(artifacts)
        for a in artifacts:
            placed = False
            attempts = 0
            while not placed and attempts < 100:
                room = random.choice(rooms)
                if len(placements[room]) < 4:
                    placements[room].append(a)
                    self.artifact_locations[a] = room
                    placed = True
                attempts += 1
        # init clues dict
        self.artifact_clues = {a: [] for a in self.ARTIFACTS}
        self.claimed = set()

    def generate_clue_for(self, artifact, room, variant=0):
        """Produce a short fantasy-themed clue without using an LLM."""
        templates = [
            "Whispers say the {artifact} hides close to the {room}.",
            "A shimmer near the {room} hints at the {artifact}.",
            "Follow the faint glow in the {room} to find the {artifact}.",
            "Old portraits in the {room} seem to watch over the {artifact}.",
            "Dusty footprints in the {room} circle where the {artifact} once gleamed.",
            "Echoes in the {room} repeat the name of the {artifact}.",
        ]
        pattern = templates[variant % len(templates)]
        return pattern.format(artifact=artifact, room=room)

    def generate_all_clues(self):
        # First try to load static hints from artifact_hints.json
        hints_path = os.path.join(os.path.dirname(__file__), "artifact_hints.json")
        loaded = False
        if os.path.exists(hints_path):
            try:
                with open(hints_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for room_name, artifacts in data.items():
                    for art, clues in artifacts.items():
                        if isinstance(clues, list) and clues:
                            self.artifact_clues[art] = list(clues)
                loaded = True
            except Exception as e:
                print("Failed to load artifact hints file:", e)
        if loaded:
            return
        # Fallback: generate 3 template-based clues per artifact using room context
        for a, room in self.artifact_locations.items():
            self.artifact_clues[a] = [self.generate_clue_for(a, room, i) for i in range(1,4)]

    def provide_hint(self, session_id, username, socketio, imposter_name, players):
        # pick a random unrevealed artifact and return next unused clue
        available = [a for a in self.ARTIFACTS if a not in self.claimed]
        if not available:
            return None
        artifact = random.choice(available)
        clues = self.artifact_clues.get(artifact, [])
        if not clues:
            return None
        # pop one clue to simulate consumption
        clue = clues.pop(0)

        # find requester sid and imposter sid
        requester_sid = None
        imposter_sid = None
        for p in players.values():
            if p.name == username:
                requester_sid = p.sid
            if p.is_imposter:
                imposter_sid = p.sid

        # Initialize per-session manipulation/delay budgets (5 each)
        self.manipulations_left.setdefault(session_id, 5)
        self.delays_used.setdefault(session_id, 0)

        def emit_hint(payload, target_sid):
            if target_sid:
                socketio.emit("hint_response", payload, room=target_sid)
            else:
                socketio.emit("hint_response", payload, room=session_id)

        payload = {"artifact": artifact, "clue": clue}

        # If we still have manipulation budget, make the clue harder once
        if self.manipulations_left[session_id] > 0:
            twisted = "A misleading whisper: " + "".join(
                reversed(clue.split(" ", 1)[-1])
            )
            payload = {"artifact": artifact, "clue": twisted}
            self.manipulations_left[session_id] -= 1
            emit_hint(payload, requester_sid)
        # Otherwise, if we still have delay budget, delay the hint by ~20s
        elif self.delays_used[session_id] < 5:
            self.delays_used[session_id] += 1

            def delayed():
                time.sleep(20)
                emit_hint(payload, requester_sid)

            threading.Thread(target=delayed, daemon=True).start()
        else:
            emit_hint(payload, requester_sid)

        return {"artifact": artifact, "clue": clue}

    def claim_artifact(self, artifact_id, username):
        if artifact_id not in self.ARTIFACTS:
            return False
        if artifact_id in self.claimed:
            return False
        self.claimed.add(artifact_id)
        return True

    def all_claimed(self):
        return len(self.claimed) >= len(self.ARTIFACTS)
