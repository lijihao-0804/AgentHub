from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_AUTH_JWT_SECRET = "local-dev-only-change-me-32-characters"
DEVELOPMENT_ENVIRONMENTS = frozenset({"local", "test", "testing", "development", "dev"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTHUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "agenthub-api"
    environment: str = "local"
    log_level: str = "INFO"
    testing: bool = False
    process_role: Literal["api", "worker", "beat"] = "api"
    credential_master_key: str | None = None
    credential_allow_legacy_plaintext: bool = False
    database_url: str = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"
    knowledge_qdrant_collection: str = "agenthub_knowledge"
    knowledge_dense_vector_size: int = Field(default=1024, ge=1, le=4096)
    knowledge_embedding_model: str = "BAAI/bge-m3"
    knowledge_embedding_device: str = "auto"
    knowledge_embedding_batch_size: int = Field(default=8, ge=1, le=128)
    knowledge_reranker_model: str = "BAAI/bge-reranker-v2-m3"
    knowledge_reranker_device: str = "auto"
    knowledge_reranker_batch_size: int = Field(default=8, ge=1, le=64)
    knowledge_qdrant_timeout_seconds: float = Field(default=10, gt=0, le=120)
    knowledge_rrf_k: int = Field(default=60, ge=1, le=10_000)
    # Evidence whose rerank score falls below this is dropped, so a question the
    # corpus cannot answer yields an empty evidence set instead of the six
    # least-bad chunks. Off by default on purpose: the usable value is a
    # property of a corpus, not a constant, and the usable *window* is narrow.
    # Measured on a 60-document enterprise corpus: the strongest unanswerable
    # question scored +0.1038 and the weakest answerable one +0.1483, so the
    # whole window is 0.04 wide. Calibrate against the weakest score of any
    # document a real question needs -- not against top-1 scores, which are far
    # higher and suggest a margin that is not there. At +0.13 this corpus
    # rejected 10/10 unanswerable questions and lost no answerable one; at
    # +1.0, still well under the weakest top-1, it silently lost three.
    knowledge_min_rerank_score: float | None = Field(default=None, ge=-100, le=100)
    # Subtracted from the rerank score of a chunk whose document has been
    # superseded. A penalty rather than a filter: "what did the old policy
    # say" is a legitimate question, and filtering would make it unanswerable.
    #
    # Off by default because measurement says a constant cannot do this job:
    # on the corpus above, the score gaps of questions wanting the current
    # version (1.07, 0.10) interleave with those wanting the retired one
    # (0.87, 0.15, 1.83), so no value separates them -- 2.0 fixed two cases and
    # broke three. Worse, it interacts with the floor: demoting a retired
    # document pushes correct evidence below the unanswerable questions, which
    # inverts the separation the floor depends on. Enable it only on a corpus
    # where a retired document is never the answer, and not with a floor.
    # Version *selection* belongs to the model, which sees the question: the
    # ``superseded`` and ``effective_date`` fields on every search_knowledge
    # row answered 4/4 of these cases that the ranking layer could not.
    knowledge_superseded_rank_penalty: float = Field(default=0.0, ge=0, le=100)
    blob_root: str = "data/blobs"
    knowledge_max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    knowledge_ingestion_lease_seconds: int = Field(default=300, ge=5, le=86_400)
    knowledge_ingestion_lease_renewal_seconds: int = Field(default=30, ge=1, le=3_600)
    knowledge_ingestion_enqueue_grace_seconds: int = Field(default=30, ge=0, le=86_400)
    knowledge_parser_timeout_seconds: float = Field(default=60, gt=0, le=3_600)
    knowledge_max_pdf_pages: int = Field(default=500, ge=1, le=100_000)
    knowledge_max_parsed_chars: int = Field(default=2_000_000, ge=1, le=50_000_000)
    knowledge_chunk_size_chars: int = Field(default=1_200, ge=1, le=100_000)
    knowledge_chunk_overlap_chars: int = Field(default=200, ge=0, le=99_999)
    knowledge_qa_max_evidence_chars: int = Field(default=16_000, ge=1_000, le=100_000)
    knowledge_ingestion_max_attempts: int = Field(default=3, ge=1, le=100)
    knowledge_ingestion_retry_base_seconds: int = Field(default=30, ge=1, le=86_400)
    knowledge_reconciliation_batch_size: int = Field(default=100, ge=1, le=10_000)
    # Off by default because warming downloads ~3.3GB of weights the first time,
    # which is not what someone running the API to look at a settings page wants.
    # Deployments that serve `search_knowledge` should turn it on -- see
    # `warm_retrieval_components` for what the first query costs without it.
    knowledge_warm_models_on_start: bool = False
    approval_ttl_seconds: int = Field(default=3_600, ge=60, le=31_536_000)
    approval_reconciliation_batch_size: int = Field(default=100, ge=1, le=10_000)
    approval_reconciliation_stale_seconds: int = Field(default=300, ge=30, le=86_400)
    evaluation_runner_lease_seconds: int = Field(default=300, ge=5, le=86_400)
    evaluation_runner_heartbeat_seconds: int = Field(default=30, ge=1, le=86_400)
    evaluation_enqueue_grace_seconds: int = Field(default=30, ge=0, le=86_400)
    evaluation_reconciliation_batch_size: int = Field(default=100, ge=1, le=10_000)
    # Remote MCP egress. The defaults are the safe ones: private targets are
    # refused, and every remote call is bounded in time and in size. Loosening
    # any of these is an explicit operator decision, never an inference from
    # the environment name.
    mcp_allow_private_targets: bool = False
    mcp_connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    mcp_request_timeout_seconds: float = Field(default=30, gt=0, le=300)
    mcp_discovery_max_tools: int = Field(default=200, ge=1, le=10_000)
    mcp_discovery_max_schema_bytes: int = Field(default=65_536, ge=1_024, le=8_388_608)
    mcp_discovery_max_payload_bytes: int = Field(default=2_097_152, ge=4_096, le=33_554_432)
    # A tool result goes straight into a model's context, so this bounds what a
    # remote server can spend of a run's context budget in one answer.
    mcp_tool_result_max_bytes: int = Field(default=1_048_576, ge=1_024, le=8_388_608)
    langfuse_enabled: bool = False
    request_id_header: str = "X-Request-ID"
    ready_timeout_ms: int = Field(default=500, ge=50, le=10_000)
    sse_heartbeat_seconds: float = Field(default=15, gt=0, le=120)
    # How long a run may go unwatched before it is aborted. A run used to die
    # the instant its SSE consumer disconnected; this is the window in which a
    # client can reconnect (GET .../runs/{id}/stream?after_sequence=N) and take
    # the stream back. Bounded above so "nobody is coming back" still ends the
    # run rather than leaking an executor.
    run_stream_grace_seconds: float = Field(default=60, ge=0, le=3_600)
    # Execute runs in the Celery worker instead of inside the API process.
    # Off by default: Playground runs are single-shot debugging and must keep
    # their current in-process latency and behaviour exactly.
    run_execution_in_worker: bool = False
    auth_jwt_secret: str = Field(default=DEFAULT_AUTH_JWT_SECRET, min_length=32)
    auth_access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    auth_refresh_token_ttl_seconds: int = Field(default=2_592_000, ge=300, le=31_536_000)
    auth_refresh_cookie_name: str = "agenthub_refresh"
    auth_refresh_cookie_path: str = "/api/v1/auth"
    auth_refresh_cookie_samesite: Literal["lax", "strict"] = "lax"
    auth_cookie_secure: bool | None = None

    @model_validator(mode="after")
    def validate_knowledge_ingestion_settings(self) -> Settings:
        if self.knowledge_chunk_overlap_chars >= self.knowledge_chunk_size_chars:
            raise ValueError("knowledge chunk overlap must be smaller than chunk size")
        if self.knowledge_ingestion_lease_renewal_seconds >= self.knowledge_ingestion_lease_seconds:
            raise ValueError("knowledge lease renewal must be shorter than lease duration")
        if self.knowledge_embedding_device not in {"auto", "cpu", "cuda"}:
            raise ValueError("knowledge embedding device must be auto, cpu, or cuda")
        if self.knowledge_reranker_device not in {"auto", "cpu", "cuda"}:
            raise ValueError("knowledge reranker device must be auto, cpu, or cuda")
        if self.evaluation_runner_heartbeat_seconds >= self.evaluation_runner_lease_seconds:
            raise ValueError("evaluation runner heartbeat must be shorter than lease duration")
        if self.mcp_connect_timeout_seconds > self.mcp_request_timeout_seconds:
            raise ValueError("mcp connect timeout must not exceed the overall request timeout")
        if self.mcp_discovery_max_schema_bytes > self.mcp_discovery_max_payload_bytes:
            raise ValueError("mcp per-tool schema budget must fit inside the payload budget")
        if (
            self.environment.lower() not in DEVELOPMENT_ENVIRONMENTS
            and self.process_role == "api"
            and self.auth_jwt_secret == DEFAULT_AUTH_JWT_SECRET
        ):
            raise ValueError(
                "AGENTHUB_AUTH_JWT_SECRET must be explicitly configured outside "
                "local/test/development"
            )
        return self

    @property
    def database_sync_url(self) -> str:
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        return self.database_url

    @property
    def refresh_cookie_secure(self) -> bool:
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return self.environment.lower() not in DEVELOPMENT_ENVIRONMENTS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
