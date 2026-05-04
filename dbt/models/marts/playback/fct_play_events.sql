with play_events as (
    select * from {{ ref('stg_play_events') }}
),

tracks as (
    select * from {{ ref('stg_tracks') }}
),

artists as (
    select * from {{ ref('stg_artists') }}
)

select
    pe.event_id,
    pe.listener_id,
    pe.track_id,
    pe.played_at,
    pe.duration_listened_seconds,
    pe.source as play_source,
    t.title as track_title,
    t.genre as track_genre,
    t.duration_seconds as track_duration_seconds,
    t.is_explicit as track_is_explicit,
    t.artist_id,
    a.artist_name
from play_events pe
left join tracks t
    on pe.track_id = t.track_id
left join artists a
    on t.artist_id = a.artist_id
