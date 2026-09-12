"""Regression tests for deterministic evidence quality assessment.

assess_evidence_quality() must NEVER be determined by the model.
These tests pin every branch so regressions are caught immediately.
"""

from noema.domain.meaningful.confidence import ConfidenceCalculator
from noema.domain.meaningful.models import EvidenceQuality
from noema.domain.sessions import ActivitySession


def _session(app=None, title=None, domain=None, url=None):
    return ActivitySession(
        start="2026-09-04T10:00:00Z",
        end="2026-09-04T10:01:00Z",
        device="laptop",
        app=app,
        title=title,
        domain=domain,
        url=url,
        event_keys=("ev",),
    )


def quality(*sessions):
    return ConfidenceCalculator.assess_evidence_quality(sessions)


def test_title_plus_domain_is_strong():
    assert quality(_session("Chrome", "Python docs", "docs.python.org")) == EvidenceQuality.STRONG


def test_title_plus_url_is_strong():
    assert quality(_session("Firefox", "Some page", url="https://example.com/p")) == EvidenceQuality.STRONG


def test_native_app_with_title_is_strong():
    # Non-browser app (VS Code) + title = STRONG even without domain/URL.
    assert quality(_session("VS Code", "reward.py")) == EvidenceQuality.STRONG


def test_native_app_with_title_is_strong_for_terminal():
    assert quality(_session("Terminal", "bash — my-project")) == EvidenceQuality.STRONG


def test_browser_title_only_no_domain_is_weak():
    # Browser apps without a domain give only the window title: WEAK.
    assert quality(_session("Google Chrome", "Stack Overflow - Python")) == EvidenceQuality.WEAK


def test_browser_firefox_title_only_is_weak():
    assert quality(_session("Mozilla Firefox", "Reddit - Python")) == EvidenceQuality.WEAK


def test_app_only_no_title_is_weak():
    assert quality(_session("Slack")) == EvidenceQuality.WEAK


def test_no_signal_at_all_is_absent():
    assert quality(_session()) == EvidenceQuality.ABSENT


def test_title_only_no_app_is_moderate():
    assert quality(_session(title="Some window")) == EvidenceQuality.MODERATE


def test_non_browser_app_and_title_is_strong_not_moderate():
    # Non-browser with both app and title goes to STRONG (line 81-83 path).
    assert quality(_session("Xcode", "ViewController.swift")) == EvidenceQuality.STRONG


def test_any_session_with_strong_evidence_wins():
    # A list with mixed signals: one STRONG session upgrades the whole set.
    weak = _session("Google Chrome", "some page")
    strong = _session("VS Code", "main.py", "github.com")
    assert quality(weak, strong) == EvidenceQuality.STRONG


def test_empty_session_list_is_absent():
    assert ConfidenceCalculator.assess_evidence_quality([]) == EvidenceQuality.ABSENT
