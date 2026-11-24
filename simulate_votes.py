"""Simulate several users voting on the demo poll.

Start the server first (python app.py), then run:  python simulate_votes.py
Set BASE_URL to target a different host/port, e.g. BASE_URL=http://127.0.0.1:5077
"""
import os
import random

import requests

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:5000")
POLL_ID = os.environ.get("POLL_ID", "demo")
NUM_USERS = int(os.environ.get("NUM_USERS", 10))


def main():
    poll = requests.get(f"{BASE_URL}/api/polls/{POLL_ID}", timeout=5)
    poll.raise_for_status()
    options = range(len(poll.json()["options"]))

    for i in range(NUM_USERS):
        # A fresh Session has no cookies, so the server treats it as a new user
        with requests.Session() as session:
            choice = random.choice(options)
            response = session.post(
                f"{BASE_URL}/api/polls/{POLL_ID}/vote", json={"option": choice}, timeout=5
            )
            print(f"[User {i + 1}] voted for option {choice} -> {response.status_code} {response.json()}")


if __name__ == "__main__":
    main()
