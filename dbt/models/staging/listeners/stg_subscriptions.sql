
    select
        subscription_id, listener_id, tier, start_date, end_date, monthly_price_eur
    from {{ source('melodify__listeners', 'subscriptions') }}

