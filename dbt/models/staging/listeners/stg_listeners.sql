
    select
        listener_id, listener_name, email, country, signup_date, gdpr_consent_date, is_active
    from {{ source('melodify__listeners', 'listeners') }}

