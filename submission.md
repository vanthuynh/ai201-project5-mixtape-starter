# Mixtape Codebase Map

This document traces how the `routes/` and `services/` layers work together, following the
call-chain style suggested in the README ("start at the route, trace it to the service").

## 1. Layers, top to bottom

| File / Directory | Description |
| :--- | :--- |
| `app.py` | Flask app factory: creates the `db` SQLAlchemy instance, registers the four blueprints, and calls `db.create_all()` on startup. |
| `models.py` | SQLAlchemy models + three association tables (`friendships`, `song_tags`, `playlist_entries`). Every service reads/writes these directly via `db.session` — there is no separate repository/DAO layer. |
| `routes/*.py` | Flask blueprints. Each route: parses the request, calls exactly one service function, translates `ValueError` -> a 400/404 JSON response. Routes never touch `db.session` directly except `routes/users.py`'s `GET /<user_id>`, which is a plain lookup with no business logic. |
| `services/*.py` | All business logic and all `db.session` reads/writes live here. This is also where the tracked bugs live (per the README's issue table). |

---

Blueprint registration (`app.py`) and their URL prefixes:

| Blueprint | Prefix | File |
|---|---|---|
| `songs_bp` | `/songs` | `routes/songs.py` |
| `playlists_bp` | `/playlists` | `routes/playlists.py` |
| `users_bp` | `/users` | `routes/users.py` |
| `feed_bp` | `/feed` | `routes/feed.py` |

**Understanding Blueprint**: a Blueprint is a way to define a group of routes in one file without wiring them into the actual Flask app yet. It's like a template or a sub-app — you build it, then "print" it onto the real app later with register_blueprint(). Until it's registered, it doesn't do anything.

**Why that's useful**: without blueprints, every route in your whole app (/songs/search, /playlists/<id>, /users/<id>/streak, etc.) would have to live in one giant file with one @app.route(...) per line, all sharing one flat namespace. Blueprints let you split that by feature area instead.

===>> in this app, a Blueprint = "one Python object per feature (songs/playlists/users/feed) that bundles its own routes," and app.py's job is just to import the four of them and decide what URL prefix each one lives under — cleanly separating "what routes exist" (routes/*.py)

---



## 2. Data model at a glance (`models.py`)

- **User** — has `listening_streak` / `last_listened_at` (owned by `streak_service`), and a
  self-referential many-to-many `friends` via the `friendships` table.
- **Song** — has `shared_by` (the user who shared it) and tags via `song_tags`. Note: there is
  no `POST /songs` route to create a song — songs currently only enter the DB through
  `seed_data.py`. The "songs" module handles searching, viewing, rating, and logging listens
  to *existing* songs, not the act of sharing one.
- **ListeningEvent** — one row per "user listened to song" action. This is the row that both
  the streak service and the feed service key off of.
- **Rating** — one row per (user, song) pair, enforced by a unique constraint; re-rating
  updates the existing row instead of inserting a new one.
- **Playlist** — many-to-many with `Song` via `playlist_entries`, which also stores
  `position`, `added_by`, and `added_at`.
- **Notification** — a flat inbox row per user: `notification_type`, `body`, `read`.

## 3. Route → service call chains

### `routes/songs.py` (prefix `/songs`)

| Route | Service call |
|---|---|
| `GET /search?q=` | `search_service.search_songs(query)` |
| `GET /<song_id>` | `search_service.get_song(song_id)` |
| `POST /<song_id>/rate` | `notification_service.rate_song(user_id, song_id, score)` |
| `POST /<song_id>/listen` | `streak_service.record_listening_event(user_id, song_id)` |

### `routes/playlists.py` (prefix `/playlists`)

| Route | Service call |
|---|---|
| `POST /` | `playlist_service.create_playlist(name, created_by, is_collaborative)` |
| `GET /<playlist_id>` | `playlist_service.get_playlist(playlist_id)` |
| `GET /<playlist_id>/songs` | `playlist_service.get_playlist_songs(playlist_id)` |
| `POST /<playlist_id>/songs` | `notification_service.add_to_playlist(playlist_id, song_id, added_by)` |

Note that adding a song to a playlist is implemented in `notification_service`, not
`playlist_service` — it's the one write path that needs to both mutate the playlist *and*
fire a notification, so it was grouped with the notification logic.

### `routes/users.py` (prefix `/users`)

| Route | Service call |
|---|---|
| `GET /<user_id>` | none — direct `db.session.get(User, user_id)` |
| `GET /<user_id>/streak` | `streak_service.get_streak(user_id)` |
| `GET /<user_id>/notifications` | `notification_service.get_notifications(user_id, unread_only)` |
| `POST /notifications/<id>/read` | `notification_service.mark_as_read(notification_id)` |

### `routes/feed.py` (prefix `/feed`)

| Route | Service call |
|---|---|
| `GET /<user_id>/listening-now` | `feed_service.get_friends_listening_now(user_id)` |
| `GET /<user_id>/activity` | `feed_service.get_activity_feed(user_id)` |

## 4. Worked example: adding a song to a playlist → notification

This is the fullest cross-cutting flow in the app, spanning two services:

```
POST /playlists/<playlist_id>/songs  {song_id, added_by}
  routes/playlists.py: add_song()
    → notification_service.add_to_playlist(playlist_id, song_id, added_by_user_id)
        1. Loads Song, User (adder), Playlist — 404s (as ValueError) if any is missing.
        2. If the song isn't already in playlist.songs, appends it and commits.
           (playlist.songs is the `playlist_entries` secondary relationship on Playlist.)
        3. If song.shared_by != added_by_user_id (i.e. someone other than the original
           sharer added it), calls create_notification(...) to notify song.shared_by,
           with notification_type="song_added_to_playlist".
    → create_notification(user_id, notification_type, body)
        Just constructs a Notification row and commits it. This is the single write path
        for all notifications in the app — every other "notify someone" case should
        ultimately funnel through this function.
```

So "song added to playlist" notifications work end-to-end: route → `add_to_playlist` →
`create_notification` → row in `notification` table → visible via
`GET /users/<id>/notifications`.


There is currently no "share a song" write path at all (no `POST /songs`), so "sharing a
song" itself never triggers a notification — the two notification-worthy actions in the
current code are *rating* and *adding to a playlist*, both keyed off `song.shared_by`.

## 5. Worked example: how a song reaches a user's feed

Unlike notifications, the feed is **not** a table that gets written to when something
happens — it's computed on read from `ListeningEvent` rows. The only write path is:

```
POST /songs/<song_id>/listen  {user_id}
  routes/songs.py: listen()
    → streak_service.record_listening_event(user_id, song_id)
        1. Creates a ListeningEvent(user_id, song_id, listened_at=now) row.
        2. Calls update_listening_streak(user, now) (see §6) to update the *listener's own*
           streak — this is incidental to the feed, just co-located because both features
           key off "a listen happened."
        3. Commits.
```

That single `ListeningEvent` row is then picked up independently by whichever friend
queries their feed:

```
GET /feed/<user_id>/listening-now
  routes/feed.py: listening_now()
    → feed_service.get_friends_listening_now(user_id)
        1. Loads `user.friends` (from the friendships table) to get friend_ids.
        2. Queries ListeningEvent where user_id IN friend_ids AND listened_at >= now-24h,
           ordered newest first.
        3. Deduplicates so each friend appears once, keeping only their most recent event.
        4. Returns {friend, song, listened_at} dicts.

GET /feed/<user_id>/activity
  routes/feed.py: activity()
    → feed_service.get_activity_feed(user_id)
        Same friend_ids lookup, but no 24h cutoff and no per-friend dedup — just the
        most recent `limit` ListeningEvents across all friends.
```

So "a song gets added to a user's feed" really means: someone the user follows calls
`POST /songs/<id>/listen`, which inserts one `ListeningEvent` row; the feed endpoints don't
know or care about that write, they just re-scan `ListeningEvent` filtered by `user.friends`
every time they're called. This is also why the friendships table matters for the feed but
not for notifications — notifications target a specific `song.shared_by` user id directly,
while feed visibility is entirely gated by the `friendships` association table.

## 6. Streak logic (`streak_service.py`)

Triggered as a side effect of `record_listening_event` (see §5), not from its own route.
`update_listening_streak(user, now)`:

- No prior `last_listened_at` → streak = 1.
- Same calendar day as last listen → no-op.
- Exactly one calendar day since last listen → streak += 1 (with a `today.weekday() != 6`
  Sunday-boundary special case).
- More than one day gap → streak resets to 1.

`GET /users/<id>/streak` just reads `user.listening_streak` back out via
`streak_service.get_streak()`; it doesn't recompute anything, so the value shown is only as
fresh as the last time that user (or a route triggering a listen on their behalf) called
`record_listening_event`.

## 7. Search (`search_service.py`)

`GET /songs/search?q=` → `search_songs(query)` does a single query: `Song.title ILIKE %q%
OR Song.artist ILIKE %q%`, `outerjoin`ed to `song_tags` so tag names can be attached via
`song.to_dict()`. The join is why a song with multiple tags can appear once per matching
tag row unless deduplicated by the caller.

## 8. Playlist retrieval (`playlist_service.py`)

`get_playlist_songs(playlist_id)` joins `Song` to `playlist_entries` filtered by
`playlist_id`, ordered by `position` ascending — this is the ordering that
`add_to_playlist` (§4) relies on implicitly when it appends new songs. `get_playlist()` and
`get_user_playlists()` are metadata-only reads and don't touch `playlist_entries`.

## 9. Summary: which service owns which cross-cutting concern

| Concern | Owning service | Triggered by |
|---|---|---|
| Notifications (inbox) | `notification_service.create_notification` | `add_to_playlist`; *not currently called from* `rate_song` |
| Listening streak | `streak_service.update_listening_streak` | `record_listening_event` (from `POST /songs/<id>/listen`) |
| Friends' recent activity | `feed_service` | reads `ListeningEvent` rows written by `record_listening_event` |
| Playlist song order | `playlist_entries.position` | writes in `add_to_playlist`; reads in `get_playlist_songs` |
| Friendship graph | `friendships` table | read by `feed_service` only — no route currently manages it |
