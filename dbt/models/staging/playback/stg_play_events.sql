
    select
        event_id, listener_id, track_id, played_at, duration_listened_seconds, source
    from {{ source('melodify__playback', 'play_events') }}

