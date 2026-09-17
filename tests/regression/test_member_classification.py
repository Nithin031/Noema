"""Batched per-member classification inside grouped episodes.

When meaningful sessions are classified, each episode is tokenized into
batched items — the episode itself plus member raw sessions still needing
verdicts — and every output follows the same verdict schema, attributed by
session_id echo. This pins:

* members receive their own verdicts (no blending into one aggregate);
* the episode verdict still exists for episode-level consumers;
* member context is episode-scoped (no cross-episode corroboration);
* already-verdicted members are skipped (no quota waste);
* batch failure falls back to single calls;
* per-episode expansion is bounded; remainder converges later;
* queue eligibility covers member remainder.
"""

from datetime import timedelta

from noema.api import NoemaService
from noema.classification import queue as classification_queue
from noema.classification.core import Classification, Classifier
from noema.domain.activity import ActivityEvent
from noema.domain.meaningful import MeaningfulSession
from noema.domain.meaningful.models import EvidenceQuality
from noema.domain.sessions import ActivitySession
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore
from noema.infrastructure.providers import ProviderError


def _payload(session_id, category="productive", productivity="productive"):
    return {
        "session_id": session_id,
        "category": category,
        "subcategory": "research",
        "activity": "Judged work",
        "signal": "Member evidence supports this result.",
        "confidence": 0.8,
        "productivity": productivity,
    }


class FakeMemberChain:
    """Provider double recording exactly which session ids were batched."""

    name = "hosted_chain"
    model = None

    def __init__(self, verdicts=None, fail_batch=False):
        # verdicts: {session_id: (category, productivity)}
        self.verdicts = verdicts or {}
        self.fail_batch = fail_batch
        self.batched_ids = []
        self.evidences = {}
        self.batch_calls = 0
        self.single_calls = 0
        self.last_batch = {}
        self.last_provider = "gemini"
        self.last_model = "fake-flash"

    def classify_batch(self, entries):
        self.batch_calls += 1
        if self.fail_batch:
            raise ProviderError("batch down")
        out = {}
        for entry in entries:
            session_id = entry["session"].id
            self.batched_ids.append(session_id)
            self.evidences[session_id] = entry["evidence"]
            category, productivity = self.verdicts.get(session_id, ("productive", "productive"))
            out[session_id] = _payload(session_id, category, productivity)
            self.last_batch[session_id] = ("gemini", "fake-flash")
        return out

    def classify(self, session, prompt):
        self.single_calls += 1
        category, productivity = self.verdicts.get(session.id, ("neutral", "neutral"))
        return _payload(session.id, category, productivity)


def _service(chain):
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(), store,
        classifier=Classifier(provider=chain),
    )
    return store, service


def _seed_episode(store, service, specs, key="ep"):
    """Ingest events + raw sessions + one episode; return (episode, raws)."""
    raws = []
    for app, title, event_key in specs:
        event = ActivityEvent(
            timestamp="2026-09-04T10:00:00Z", duration=30, device="laptop",
            app=app, title=title, bucket_id="window", source_event_id=event_key,
        )
        service.ingest_events([event])
        end = (event.timestamp + timedelta(seconds=event.duration)).isoformat().replace("+00:00", "Z")
        raw = ActivitySession(
            start="2026-09-04T10:00:00Z", end=end,
            device="laptop", app=app, title=title,
            event_keys=(event.event_key,),
        )
        store.insert_session(raw)
        raws.append(raw)
    episode = MeaningfulSession(
        start_time="2026-09-04T10:00:00Z",
        end_time="2026-09-04T10:10:00Z",
        device_set=("laptop",),
        activity_session_ids=tuple(item.id for item in raws),
        primary_project="Proj-{}".format(key),
        primary_task="Task {}".format(key),
        evidence_quality=EvidenceQuality.MODERATE,
    )
    store.insert_meaningful_session(episode)
    return episode, raws


def test_members_receive_own_verdicts_in_one_batch():
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        episode, raws = _seed_episode(store, service, [
            ("Firefox", "Noema dashboard", "m-a"),
            ("Brave", "Claude Code", "m-b"),
        ])
        results = service.classify_meaningful_sessions([episode])
        by_id = {item.session_id: item for item in results}
        # Episode verdict plus one verdict per member, same schema each.
        assert set(by_id) == {episode.id, raws[0].id, raws[1].id}
        for item in by_id.values():
            assert isinstance(item, Classification)
            assert item.classification_status == "classified"
            assert item.source == "gemini"
        # Persisted under each unit's own id: timeline rows become DIRECT.
        stored = store.query_classification_map([episode.id, raws[0].id, raws[1].id])
        assert set(stored) == {episode.id, raws[0].id, raws[1].id}
        assert chain.batch_calls == 1
    finally:
        store.close()


def test_distinct_member_verdicts_are_preserved():
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        episode, raws = _seed_episode(store, service, [
            ("Firefox", "Noema dashboard", "m-a"),
            ("Brave", "Claude Code", "m-b"),
        ])
        chain.verdicts = {
            episode.id: ("productive", "productive"),
            raws[0].id: ("productive", "productive"),
            raws[1].id: ("distractive", "distracting"),
        }
        service.classify_meaningful_sessions([episode])
        stored = store.query_classification_map([raws[0].id, raws[1].id])
        assert stored[raws[0].id].productivity == "productive"
        assert stored[raws[1].id].productivity == "distracting"
    finally:
        store.close()


def test_verdicted_members_are_skipped():
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        episode, raws = _seed_episode(store, service, [
            ("Firefox", "Noema dashboard", "m-a"),
            ("Brave", "Claude Code", "m-b"),
        ])
        store.insert_classification(Classification(
            raws[0].id, category="research", activity_type="reading",
            productivity="productive", confidence=0.9,
            classification_status="classified",
            provider="gemini", model="fake-flash", source="gemini"))
        service.classify_meaningful_sessions([episode])
        assert raws[0].id not in chain.batched_ids
        assert raws[1].id in chain.batched_ids
        assert episode.id in chain.batched_ids
    finally:
        store.close()


def test_member_context_is_episode_scoped():
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        ep_a, _ = _seed_episode(store, service, [("Firefox", "Alpha work", "s-a")], key="a")
        ep_b, _ = _seed_episode(store, service, [("Brave", "Beta play", "s-b")], key="b")
        service.classify_meaningful_sessions([ep_a, ep_b])
        assert chain.batch_calls == 1
        assert len(chain.batched_ids) == 4
        member_a = [sid for sid in chain.batched_ids if sid not in (ep_a.id, ep_b.id)][0]
        member_b = [sid for sid in chain.batched_ids if sid not in (ep_a.id, ep_b.id)][1]
        assert chain.evidences[member_a]["episode"]["task"] == "Task a"
        assert chain.evidences[member_b]["episode"]["task"] == "Task b"
        # ... and no trace of the other episode anywhere in the evidence.
        blob_a = str(chain.evidences[member_a])
        blob_b = str(chain.evidences[member_b])
        assert "Task b" not in blob_a and "Beta play" not in blob_a
        assert "Task a" not in blob_b and "Alpha work" not in blob_b
    finally:
        store.close()


def test_member_evidence_carries_episode_envelope():
    from noema.classification.core import Classifier

    store = SQLiteStore()
    try:
        episode, raws = _seed_episode(store, _service(FakeMemberChain())[1], [("Firefox", "Alpha work", "s-a")], key="a")
        context = classification_queue.member_context(episode, raws[0], raws)
        evidence = Classifier.build_evidence(raws[0], context)
        assert evidence["session_type"] == "activity"
        assert evidence["app"] == "Firefox"
        assert evidence["episode"]["task"] == "Task a"
        assert evidence["episode"]["project"] == "Proj-a"
    finally:
        store.close()


def test_batch_failure_falls_back_to_single_calls():
    chain = FakeMemberChain(fail_batch=True)
    store, service = _service(chain)
    try:
        episode, raws = _seed_episode(store, service, [
            ("Firefox", "Noema dashboard", "m-a"),
            ("Brave", "Claude Code", "m-b"),
        ])
        results = service.classify_meaningful_sessions([episode])
        assert chain.single_calls == 3
        assert {item.session_id for item in results} == {episode.id, raws[0].id, raws[1].id}
        assert all(item.classification_status == "classified" for item in results)
    finally:
        store.close()


def test_expansion_bounded_per_episode_with_convergence():
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        specs = [("Firefox", "Work {}".format(i), "cap-{}".format(i)) for i in range(25)]
        episode, raws = _seed_episode(store, service, specs, key="cap")
        service.classify_meaningful_sessions([episode])
        # Episode + cap members in this call, never more.
        assert len(chain.batched_ids) == 1 + classification_queue.MAX_MEMBER_ITEMS_PER_EPISODE
        # Remainder still needs work: the episode re-qualifies next cycle.
        pending = service.pending_classification_sessions([episode], now=None)
        assert [item.id for item in pending] == [episode.id]
    finally:
        store.close()


def test_fully_covered_episode_does_not_requalify():
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        episode, raws = _seed_episode(store, service, [("Firefox", "Work", "cov-a")], key="cov")
        service.classify_meaningful_sessions([episode])
        pending = service.pending_classification_sessions([episode], now=None)
        assert pending == []
    finally:
        store.close()


def test_unknown_members_do_not_break_expansion():
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        episode, raws = _seed_episode(store, service, [("Firefox", "Work", "unk-a")], key="unk")
        # Episode references a member id that was never stored.
        episode2 = MeaningfulSession(
            start_time="2026-09-04T10:00:00Z",
            end_time="2026-09-04T10:10:00Z",
            device_set=("laptop",),
            activity_session_ids=(raws[0].id, "missing-id"),
            evidence_quality=EvidenceQuality.MODERATE,
        )
        store.insert_meaningful_session(episode2)
        results = service.classify_meaningful_sessions([episode2])
        assert {item.session_id for item in results} == {episode2.id, raws[0].id}
    finally:
        store.close()


def test_request_count_independent_of_member_count_within_budget():
    """Hard invariant: N members ride ONE provider request, never N.

    Batch transport, individual semantics: the number of model requests
    must not scale with the number of members inside safe batch capacity.
    """
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        episode, raws = _seed_episode(store, service, [
            ("Firefox", "Work {}".format(i), "inv-{}".format(i)) for i in range(20)
        ], key="inv")
        service.classify_meaningful_sessions([episode])
        assert chain.batch_calls == 1
        assert chain.single_calls == 0
        assert len(chain.batched_ids) == 21
    finally:
        store.close()


def test_fortyfour_member_episode_converges_in_bounded_batches():
    """The historical 44-member shape: bounded batches, then silence."""
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        specs = [("Firefox", "Work {}".format(i), "h44-{}".format(i)) for i in range(44)]
        episode, raws = _seed_episode(store, service, specs, key="h44")
        service.classify_meaningful_sessions([episode])
        assert chain.batch_calls == 1
        assert len(chain.batched_ids) == 1 + classification_queue.MAX_MEMBER_ITEMS_PER_EPISODE
        service.classify_meaningful_sessions([episode])
        assert chain.batch_calls == 2
        assert len(chain.batched_ids) == 1 + 2 * classification_queue.MAX_MEMBER_ITEMS_PER_EPISODE
        service.classify_meaningful_sessions([episode])
        assert chain.batch_calls == 3
        assert len(chain.batched_ids) == 1 + 44
        # Fourth cycle: everything covered, zero new provider calls.
        service.classify_meaningful_sessions([episode])
        assert chain.batch_calls == 3
        assert chain.single_calls == 0
        assert service.pending_classification_sessions([episode], now=None) == []
        stored = store.query_classification_map([item.id for item in raws])
        assert len(stored) == 44
    finally:
        store.close()


def test_batch_composition_does_not_alter_cache_identity():
    """Identical recomputation hits cache; sealed member contexts are
    order-independent by construction (episode envelope + siblings only)."""
    chain = FakeMemberChain()
    store, service = _service(chain)
    try:
        ep_a, raws_a = _seed_episode(store, service, [("Firefox", "Alpha", "cc-a")], key="ca")
        ep_b, raws_b = _seed_episode(store, service, [("Brave", "Beta", "cc-b")], key="cb")
        # persist=False: exercise the cache without writing rows.
        service.classify_meaningful_sessions([ep_a, ep_b], persist=False)
        assert chain.batch_calls == 1
        # Same computation again: cache hits, no new provider traffic.
        service.classify_meaningful_sessions([ep_a, ep_b], persist=False)
        assert chain.batch_calls == 1
        assert chain.single_calls == 0
        # Sealed member contexts do not depend on call order or grouping.
        members_a = classification_queue.episode_members(service.store, ep_a)
        members_b = classification_queue.episode_members(service.store, ep_b)
        ctx_a_first = classification_queue.member_context(ep_a, members_a[0], members_a)
        ctx_a_second = classification_queue.member_context(ep_a, members_a[0], members_a)
        assert ctx_a_first == ctx_a_second
        assert "Task b" not in str(ctx_a_first) and "Beta" not in str(ctx_a_first)
        ctx_b = classification_queue.member_context(ep_b, members_b[0], members_b)
        assert "Task a" not in str(ctx_b) and "Alpha" not in str(ctx_b)
    finally:
        store.close()
