# Модель данных

Схема в коде: `app/db.py` (SQLite). Вложенные объекты хранятся как JSON-текст.

```mermaid
erDiagram
    USERS ||--o{ SESSIONS : has
    USERS ||--o{ APPLICATIONS : owns
    APPLICATIONS ||--o{ EVENTS : logs
    USERS {
        int id PK
        text username UK
        text password_hash "pbkdf2-sha256"
        text role "client | officer"
        text full_name
    }
    SESSIONS {
        text token_hash PK "sha256 токена"
        int user_id FK
        text expires_at
    }
    APPLICATIONS {
        int id PK
        int owner_id FK
        text product "WORKING_CAPITAL | EQUIPMENT (в API: product_type)"
        text status "см. docs/states.md"
        json data "inn, business_age_months, monthly_revenue, monthly_expenses, existing_loans_monthly, requested_amount, requested_term_months, company_name, industry, purpose"
        json checks "ответы БКИ и реестра"
        json score "score 300-850, model_points, risk_category, reasons, version"
        json decision "outcome, zone, stop_factors, offer с графиком платежей, next_step"
        json clarification "запрос сотрудника: message, field, reply"
        text created_at
        text updated_at
    }
    EVENTS {
        int id PK
        int application_id FK
        text at
        text actor "логин или system"
        text kind
        text from_status
        text to_status
        json details
    }
```

Плановые сущности «компания» и «проверка» в MVP вынесены в JSON заявки (`data`, `checks`), чтобы не усложнять схему.
