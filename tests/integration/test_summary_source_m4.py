"""A published summary may only bind the same verified history source."""

from dataclasses import replace

import pytest

from packages.agent_runtime.summary_source import stable_summary_source_identity
from packages.threads.context import SqlAlchemyThreadContextProvider
from tests.integration.test_thread_history_m3 import (
    TEST_DATABASE_URL,
    _add_exchange,
    _create_thread,
    _freeze,
    _seed_workspace,
)
from tests.integration.test_thread_history_m3 import (
    db_factory as db_factory,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Requires isolated PostgreSQL"),
]


@pytest.mark.asyncio
async def test_verified_original_can_be_reused_at_later_watermark_without_changing_content(
    db_factory,
):
    base = await _seed_workspace(db_factory)
    thread = await _create_thread(db_factory, base)
    await _add_exchange(db_factory, base, thread_id=thread, sequence=1)
    _, first_run = await _add_exchange(db_factory, base, thread_id=thread, sequence=2)
    _, later_run = await _add_exchange(db_factory, base, thread_id=thread, sequence=3)
    provider = SqlAlchemyThreadContextProvider(db_factory)
    first = await _freeze(provider, base, thread_id=thread, run_id=first_run)
    later = await _freeze(provider, base, thread_id=thread, run_id=later_run)
    first_source = first.source_manifest.selected_sources[0]
    later_source = later.source_manifest.selected_sources[0]
    assert first_source.revision_sha256 != later_source.revision_sha256
    assert stable_summary_source_identity(first_source, first.turns[0]) == (
        stable_summary_source_identity(later_source, later.turns[0])
    )
    with pytest.raises(ValueError, match="does not match"):
        stable_summary_source_identity(
            first_source, replace(first.turns[0], final_output="changed")
        )


@pytest.mark.asyncio
async def test_binding_isolated_to_thread_even_when_text_is_identical(db_factory):
    base = await _seed_workspace(db_factory)
    provider = SqlAlchemyThreadContextProvider(db_factory)
    identities = []
    for _ in range(2):
        thread = await _create_thread(db_factory, base)
        await _add_exchange(db_factory, base, thread_id=thread, sequence=1)
        _, current_run = await _add_exchange(db_factory, base, thread_id=thread, sequence=2)
        view = await _freeze(provider, base, thread_id=thread, run_id=current_run)
        identities.append(
            stable_summary_source_identity(view.source_manifest.selected_sources[0], view.turns[0])
        )
    assert identities[0] != identities[1]
