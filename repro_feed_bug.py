"""
repro_feed_bug.py — Mixtape

Reproduces: "Friends Listening Now shows people from yesterday."

Scenario: it's Monday 6:00 AM. A friend's last listen was Sunday 11:50 PM
(~6 hours ago) — clearly "the night before", not "today". The feature is
supposed to only show today's activity, but get_friends_listening_now uses a
rolling 24-hour window (RECENT_THRESHOLD), so the Sunday-night listen (well
under 24h old) still shows up this morning.

get_friends_listening_now() reads datetime.now(timezone.utc) directly rather
than taking a `now` argument, so "now" is faked here by monkeypatching the
`datetime` name inside services.feed_service — that's what FakeDatetime does.

Run with:
    python repro_feed_bug.py
"""

from datetime import datetime, timezone
from unittest.mock import patch
from app import create_app, db
from models import User, Song
import services.feed_service as feed_service

app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})

with app.app_context():
    db.create_all()

    me = User(username="me", email="me@example.com")
    friend = User(username="friend", email="friend@example.com")
    db.session.add_all([me, friend])
    db.session.flush()

    me.friends.append(friend)  # one-directional is enough for this query

    song = Song(title="Late Night Track", artist="Someone", shared_by=friend.id)
    db.session.add(song)
    db.session.flush()

    sunday_night = datetime(2024, 6, 9, 23, 50, 0, tzinfo=timezone.utc)   # "last night"
    monday_morning = datetime(2024, 6, 10, 6, 0, 0, tzinfo=timezone.utc)  # "now" / "today"

    event = feed_service.ListeningEvent(
        user_id=friend.id, song_id=song.id, listened_at=sunday_night
    )
    db.session.add(event)
    db.session.commit()

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return monday_morning

    with patch("services.feed_service.datetime", FakeDatetime):
        result = feed_service.get_friends_listening_now(me.id)

    print("'now' is Monday 6:00 AM; friend's last listen was Sunday 11:50 PM (~6h10m ago).")
    print(f"Friends shown as 'listening now': {len(result)}")
    for r in result:
        print(f"  - {r['friend']['username']} listened at {r['listened_at']}")

    if result:
        print("\nBUG REPRODUCED: friend from last night still shows up in today's feed.")
    else:
        print("\nNot reproduced (already fixed?).")
