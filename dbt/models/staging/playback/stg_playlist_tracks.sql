
    select
        playlist_id, playlist_name, listener_id, track_id, position, added_at
    from {{ source('melodify__playback', 'playlist_tracks') }}

