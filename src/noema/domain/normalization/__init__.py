"""Canonicalization of adapter output before privacy and persistence."""

from .normalizer import EventNormalizer, application_identity

__all__ = ["EventNormalizer", "application_identity"]
