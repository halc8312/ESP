"""Unit tests for the bg_remover service internals (no rembg runtime required)."""
from __future__ import annotations

import time

import pytest

from services.bg_remover.internal_auth import (
    compute_signature,
    verify_signature,
)


def test_compute_signature_is_stable_for_same_inputs():
    sig_a = compute_signature(job_id="abc", timestamp="1700000000", body=b"hello")
    sig_b = compute_signature(job_id="abc", timestamp="1700000000", body=b"hello")
    assert sig_a == sig_b


def test_compute_signature_changes_when_body_changes():
    ts = "1700000000"
    sig_a = compute_signature(job_id="abc", timestamp=ts, body=b"hello")
    sig_b = compute_signature(job_id="abc", timestamp=ts, body=b"world")
    assert sig_a != sig_b


def test_verify_signature_accepts_fresh_signature():
    ts = str(int(time.time()))
    sig = compute_signature(job_id="job-1", timestamp=ts, body=b"payload")
    assert verify_signature(
        job_id="job-1",
        timestamp=ts,
        body=b"payload",
        signature=sig,
    )


def test_verify_signature_rejects_stale_timestamp():
    old_ts = str(int(time.time()) - 10_000)
    sig = compute_signature(job_id="job-1", timestamp=old_ts, body=b"payload")
    assert not verify_signature(
        job_id="job-1",
        timestamp=old_ts,
        body=b"payload",
        signature=sig,
    )


def test_verify_signature_rejects_tampered_body():
    ts = str(int(time.time()))
    sig = compute_signature(job_id="job-1", timestamp=ts, body=b"payload")
    assert not verify_signature(
        job_id="job-1",
        timestamp=ts,
        body=b"tampered",
        signature=sig,
    )


def test_verify_signature_rejects_wrong_job_id():
    ts = str(int(time.time()))
    sig = compute_signature(job_id="job-1", timestamp=ts, body=b"payload")
    assert not verify_signature(
        job_id="job-2",
        timestamp=ts,
        body=b"payload",
        signature=sig,
    )


def test_verify_signature_rejects_missing_fields():
    assert not verify_signature(
        job_id=None, timestamp="1", body=b"", signature="abc"
    )
    assert not verify_signature(
        job_id="j", timestamp=None, body=b"", signature="abc"
    )
    assert not verify_signature(
        job_id="j", timestamp="1", body=b"", signature=None
    )


def test_registry_resolves_rembg_backend_name(monkeypatch):
    from services.bg_remover.registry import resolve_backend_name

    monkeypatch.delenv("BG_REMOVAL_BACKEND", raising=False)
    assert resolve_backend_name() == "rembg"

    monkeypatch.setenv("BG_REMOVAL_BACKEND", "REMBG")
    assert resolve_backend_name() == "rembg"


def test_registry_rejects_unknown_backend(monkeypatch):
    from services.bg_remover import base, registry

    monkeypatch.setenv("BG_REMOVAL_BACKEND", "does-not-exist")
    registry.reset_bg_remover_backend_for_tests()
    with pytest.raises(base.BackgroundRemoverUnavailableError):
        registry.get_bg_remover_backend()
    registry.reset_bg_remover_backend_for_tests()
