"""Interchangeable model providers.

STT, LLM and TTS are chosen by configuration and built behind interfaces, so
swapping one is an env-var change rather than a rewrite. Only the LLM exists
today; STT and TTS arrive with the voice pipeline in Phase 3.
"""
