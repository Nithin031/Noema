from noema.application.classification import Classifier


def test_chunk_aggregation_weights_duration():
    result = Classifier._aggregate_chunk_results([
        {"category": "productive", "productivity": "productive", "confidence": 0.90, "activity": "A", "signal": "A", "_duration_seconds": 240},
        {"category": "distractive", "productivity": "distracting", "confidence": 0.20, "activity": "B", "signal": "B", "_duration_seconds": 30},
    ])

    assert result["category"] == "productive"
    assert result["confidence"] > 0.8