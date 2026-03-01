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
        # room -> [artifact names in order]
        self.room_artifacts_order = {}

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
                    # preserve artifact order per room
                    self.room_artifacts_order[room_name] = list(artifacts.keys())
                    for art, clues in artifacts.items():
                        if isinstance(clues, list) and clues:
                            self.artifact_clues[art] = list(clues)
                        # track static room placement so later we can mention
                        # which chamber a hint refers to.
                        self.artifact_locations[art] = room_name
                loaded = True
            except Exception as e:
                print("Failed to load artifact hints file:", e)
        if loaded:
            return
        # Fallback: generate 3 template-based clues per artifact using room context
        for a, room in self.artifact_locations.items():
            self.artifact_clues[a] = [self.generate_clue_for(a, room, i) for i in range(1,4)]

    def get_next_clue(self, artifact):
        """Peek the next clue without consuming it."""
        return (self.artifact_clues.get(artifact) or [None])[0]

    def consume_clue(self, artifact):
        """Consume and return the next clue for an artifact."""
        clues = self.artifact_clues.get(artifact, [])
        if not clues:
            return None
        return clues.pop(0)


    def provide_hint(self, session_id, username, socketio, imposter_name, players):
        # legacy path: random artifact / clue (kept for compatibility if needed)
        available = [a for a in self.ARTIFACTS if a not in self.claimed]
        if not available:
            return None
        artifact = random.choice(available)
        clues = self.artifact_clues.get(artifact, [])
        if not clues:
            return None
        clue = clues.pop(0)
        # try to report which room this artifact belongs to, falling back to unknown
        room = self.artifact_locations.get(artifact)
        if not room:
            # try to infer from room order data
            for r, arts in self.room_artifacts_order.items():
                if artifact in arts:
                    room = r
                    break
        return {"artifact": artifact, "clue": clue, "room": room}

    def provide_room_hint(self, session_id, room, players):
        """
        Room-aware hint selection:
        - Use artifact_hints.json order for artifacts in that room.
        - Skip artifacts that are already claimed.
        - For each artifact in order:
            * Serve its 3 hints in sequence (1, then 2, then 3).
            * Once an artifact has no clues left, move on to the next artifact in that room,
              even if the first one was never found.
        - When all clues for all artifacts in this room are exhausted, return None.
        """
        order = self.room_artifacts_order.get(room)
        if not order:
            return None
        # iterate in configured order and find the first artifact that:
        #   - is not yet claimed
        #   - still has at least one remaining clue
        for artifact in order:
            if artifact in self.claimed:
                continue
            clues = self.artifact_clues.get(artifact, [])
            if clues:
                clue = clues.pop(0)
                return {"artifact": artifact, "clue": clue, "room": room}
        # no artifact in this room has clues left
        return None

    def claim_artifact(self, artifact_id, username):
        if artifact_id not in self.ARTIFACTS:
            return False
        if artifact_id in self.claimed:
            return False
        self.claimed.add(artifact_id)
        return True

    def all_claimed(self):
        return len(self.claimed) >= len(self.ARTIFACTS)
