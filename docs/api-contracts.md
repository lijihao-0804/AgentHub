# API contracts

All versioned routes use `/api/v1`. Every request receives an `X-Request-ID` response
header. Errors use this shape:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed.",
    "request_id": "..."
  }
}
```

M0 system endpoints:

- `GET /api/v1/health`: process liveness only.
- `GET /api/v1/ready`: readiness depends on PostgreSQL only.
- `GET /api/v1/dependencies`: PostgreSQL, Redis, Qdrant and Langfuse status.
