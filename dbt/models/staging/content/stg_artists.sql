
    select
        artist_id, artist_name, genre, country, active_since, verified
    from {{ source('melodify__content', 'artists') }}

