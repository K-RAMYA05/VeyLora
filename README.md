# Fantasy Room Hunt — Multiplayer Game (5 players)

**Overview**
A fantasy-themed multiplayer game (think: wizarding-school meets hidden artifacts).
- 5 players join with the same session id. When all 5 join, the game starts.
- Round 1: each non-imposter receives a fantasy-related *word* and 90s (configurable) to photograph a matching image (client-side).
- Artifacts: 12 artifacts spread across 8 rooms (0–3 per room). Each artifact has 3 clues generated using a lightweight LLM (flan-t5-small).
- One impostor per session can manipulate up to 10 clues during the game. There are three manipulation types:
  1. Make clue more difficult by re-generating it via the LLM.
  2. Delay the clue by 20s (usable up to 5 times).
  3. Obfuscate the clue (add misleading phrasing).
- Scoring: integrate an external scoring API ("Presaige") to score images. **NOTE:** The Notion link provided was unreachable in our environment; fill your scoring endpoint/credentials in `app.py` or `.env`.
- Hints: players have hint quotas based on PreSaige score (top player gets 11 hints, 2nd 10, 3rd 8, 4th 7).
- Game duration: 12 minutes by default.
- Win conditions:
  - If all artifacts found AND the majority-voted impostor name matches the real impostor: non-impostor with most artifacts wins and players ranked by artifact count.
  - Else the impostor wins.

**What's in this package**
- `app.py` — Flask + Flask-SocketIO server and game state
- `game_logic.py` — core game classes and flow
- `artifacts.py` — artifact generator, clue generation helpers (uses `google/flan-t5-small`)
- `static/` — simple client HTML + JS (connects via socket.io)
- `requirements.txt` — Python dependencies
- `run.sh` — helper to run the server
- `README.md` — this file

**Important notes before running**
1. The environment used to create this project couldn't access the Notion Presaige API docs (404). Please add your Presaige API endpoint and key in the `PRESAIGE_*` variables in `app.py`.
2. The small LLM `google/flan-t5-small` is referenced in `artifacts.py`. You should have `transformers` and `torch` installed and internet access to download the model or provide a local path.
3. This project is a scaffold with working real-time flow and placeholder scoring + model calls. It is intended to be run locally and extended.

**How to run (basic)**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# optionally set PRESAIGE_API_URL and PRESAIGE_API_KEY as environment vars
python app.py
# open http://localhost:5000 in 5 browser windows (or tabs) and join with same session id.
```
