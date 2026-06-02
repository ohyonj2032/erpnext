import json
import os

FIXTURES_DIR = os.path.dirname(os.path.abspath(__file__))


def load_initial_data():
    with open(os.path.join(FIXTURES_DIR, "initial_data.json")) as f:
        return json.load(f)