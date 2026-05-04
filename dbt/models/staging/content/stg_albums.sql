
    select
        album_id, title, artist_id, release_date, total_tracks
    from {{ source('melodify__content', 'albums') }}

