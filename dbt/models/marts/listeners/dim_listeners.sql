with listeners as (
    select * from {{ ref('stg_listeners') }}
),

subscriptions as (
    select * from {{ ref('stg_subscriptions') }}
),

current_subscription as (
    select
        listener_id,
        subscription_id,
        tier,
        start_date as subscription_start_date,
        end_date as subscription_end_date,
        monthly_price_eur,
        row_number() over (
            partition by listener_id
            order by start_date desc
        ) as rn
    from subscriptions
)

select
    l.listener_id,
    l.listener_name,
    l.email,
    l.country,
    l.signup_date,
    l.gdpr_consent_date,
    l.is_active,
    cs.subscription_id as current_subscription_id,
    cs.tier as current_tier,
    cs.subscription_start_date,
    cs.subscription_end_date,
    cs.monthly_price_eur as current_monthly_price_eur
from listeners l
left join current_subscription cs
    on l.listener_id = cs.listener_id
    and cs.rn = 1
