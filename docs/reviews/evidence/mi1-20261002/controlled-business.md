# M-I1 controlled business checks

Real PostgreSQL; controlled fixtures; no provider quality claim.

| Case | Verdict | Failed checks |
| --- | --- | --- |
| created | True |  |
| claimed_but_absent | False | ticket_count |
| wrong_customer | False | customer_identity |
| duplicate_business_effect | False | ticket_count |
| wrong_ticket_state | False | ticket_status |
| denied_no_effect | True |  |
| denied_but_effect | False | ticket_count |
| unknown_preserved | True |  |
| wrong_run_state | False | run_status |
| run_missing | None |  |
