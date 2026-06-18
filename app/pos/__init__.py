"""POS database integration: a semantic layer over pos_assistant.db.

The LLM never writes SQL — it calls intent-named functions in ``service`` with
typed args, and ``db`` owns connection safety, date anchoring, and text folding.
"""
