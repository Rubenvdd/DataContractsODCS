
    select
        track_id, title, artist_id, album_id, duration_seconds, genre, release_date, is_explicit
    from {{ source('melodify__content', 'tracks') }}

