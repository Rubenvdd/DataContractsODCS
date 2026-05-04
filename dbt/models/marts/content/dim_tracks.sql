with tracks as (
    select * from {{ ref('stg_tracks') }}
),

artists as (
    select * from {{ ref('stg_artists') }}
),

albums as (
    select * from {{ ref('stg_albums') }}
)

select
    t.track_id,
    t.title as track_title,
    t.duration_seconds,
    t.genre,
    t.release_date,
    t.is_explicit,
    t.artist_id,
    a.artist_name,
    a.country as artist_country,
    a.verified as artist_verified,
    t.album_id,
    al.title as album_title,
    al.total_tracks as album_total_tracks
from tracks t
inner join artists a
    on t.artist_id = a.artist_id
left join albums al
    on t.album_id = al.album_id
