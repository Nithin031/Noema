from noema.infrastructure.database import SQLiteStore


def test_classification_batches_are_idempotent_and_oldest_first():
    store = SQLiteStore()
    assert store.upsert_classification_batch("batch-a", "2026-09-06T09:00:00Z", "2026-09-06T09:05:00Z", {"ids": ["activity_001"]})
    assert not store.upsert_classification_batch("batch-a", "2026-09-06T09:00:00Z", "2026-09-06T09:05:00Z", {"ids": ["activity_001"]})
    store.upsert_classification_batch("batch-b", "2026-09-06T09:05:00Z", "2026-09-06T09:10:00Z", {"ids": ["activity_002"]})

    claimed = store.claim_classification_batches()
    assert [item["batch_id"] for item in claimed] == ["batch-a", "batch-b"]
    assert all(item["status"] == "PROCESSING" for item in claimed)
    assert store.query_classification_batches(status="PROCESSING")

    store.close()


def test_processing_batches_are_recoverable():
    store = SQLiteStore()
    store.upsert_classification_batch("batch-a", "2026-09-06T09:00:00Z", "2026-09-06T09:05:00Z", {})
    store.claim_classification_batches()
    assert store.recover_processing_batches() == 1
    assert store.query_classification_batches(status="RETRYABLE")[0]["batch_id"] == "batch-a"
    store.close()