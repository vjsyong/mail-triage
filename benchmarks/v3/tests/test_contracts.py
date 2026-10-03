"""Contract tests (WP1): native parity, profiles, provenance, gold isolation.

Native parity is certified against the *live* ``engine.py`` by isolated AST
extraction -- the golden ``classify`` method and its MIME helpers are compiled
in a scratch namespace with only stdlib modules and a fake ``_chat``.  The
engine module itself is never imported, so no ``.env``, database, worker or
network is touched.
"""
import ast
import base64
import html
import json
import os
import quopri
import re
import sys
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
V3 = os.path.abspath(os.path.join(HERE, ".."))
for _p in (ROOT, V3):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.v3 import contracts as C  # noqa: E402

ENGINE = os.path.join(ROOT, "engine.py")

_VALID_JSON = ('{"category": "Action", "needs_reply": true, "confidence": 0.9, '
               '"summary": "s", "reason": "r"}')


def _extract_engine():
    """Compile only the needed functions/classes out of engine.py (no import)."""
    src = open(ENGINE).read()
    tree = ast.parse(src)
    want_funcs = {"readable_body", "looks_like_mime_junk", "_b64_decode_run",
                  "_decode_b64_blocks", "_strip_inline_mime_scaffold"}
    want_assign = {"_B64_RUN", "_BOUNDARY_TOKEN"}
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            chunks.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id in want_assign
                   for t in node.targets):
                chunks.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.ClassDef) and node.name == "LLMClient":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "classify":
                    chunks.append(textwrap.dedent(ast.get_source_segment(src, sub)))
    ns = {"re": re, "json": json, "html": html,
          "base64": base64, "quopri": quopri}
    exec("\n\n".join(chunks), ns)
    return ns


def _engine_call(msg, categories, owner, content):
    """Run the extracted production classify with a fake chat client."""
    ns = _extract_engine()

    class Fake:
        thinking = "off"
        classify = ns["classify"]

        def __init__(self):
            self.calls = []

        def _chat(self, system, user, **kw):
            self.calls.append({"system": system, "user": user, "kw": kw})
            return {"content": content}

    fake = Fake()
    try:
        result = fake.classify(msg, categories, owner)
        return {"ok": True, "result": result, "calls": fake.calls}
    except Exception as exc:  # noqa: BLE001 - record exact failure class
        return {"ok": False, "exc": type(exc).__name__, "calls": fake.calls}


def _msg(snippet, **over):
    m = {"from_addr": "sender@example.org", "to_addr": "owner@example.org",
         "subject": "Subject line", "date": "Mon, 1 Sep 2025 09:00:00 +0000",
         "snippet": snippet}
    m.update(over)
    return m


class NativeBoundaryTests(unittest.TestCase):
    def test_boundary_lengths(self):
        for n in (1499, 1500, 1501, 2500):
            with self.subTest(n=n):
                snippet = "x" * n
                req = C.build_native_request(_msg(snippet))
                body = req["user"].split("\n\n", 1)[1]
                self.assertEqual(len(body), min(n, 1500))
                self.assertEqual(body, "x" * min(n, 1500))

    def test_boundary_char_at_index(self):
        snippet = "A" * 1499 + "B" + "C" * 200  # B at index 1499, C beyond
        req = C.build_native_request(_msg(snippet))
        body = req["user"].split("\n\n", 1)[1]
        self.assertEqual(len(body), 1500)
        self.assertTrue(body.endswith("B"))
        self.assertNotIn("C", body)

    def test_headers_preserved(self):
        m = _msg("hello", from_addr="a@b.c", to_addr="me@x.y",
                 subject="Hi", date="2025-01-01")
        req = C.build_native_request(m)
        self.assertTrue(req["user"].startswith(
            "From: a@b.c\nTo: me@x.y\nSubject: Hi\nDate: 2025-01-01\n\nhello"))

    def test_boundary_matches_engine(self):
        for n in (1499, 1500, 1501, 2500):
            with self.subTest(n=n):
                m = _msg("x" * n)
                engine = _engine_call(m, None, "", _VALID_JSON)
                req = C.build_native_request(m)
                self.assertEqual(engine["calls"][0]["user"], req["user"])
                self.assertEqual(engine["calls"][0]["system"], req["system"])


class NativePromptTests(unittest.TestCase):
    def test_prompt_matches_engine(self):
        cases = [
            (_msg("hello"), ["Action", "Promo"], "Alex"),
            (_msg("x" * 1800), None, ""),
            (_msg("raw"), ["Alpha", "Beta", "Gamma"], "Dr. Lee"),
        ]
        for m, cats, owner in cases:
            with self.subTest(cats=cats, owner=owner):
                engine = _engine_call(m, cats, owner, _VALID_JSON)
                req = C.build_native_request(m, cats, owner)
                self.assertEqual(engine["calls"][0]["system"], req["system"])
                self.assertEqual(engine["calls"][0]["user"], req["user"])

    def test_dynamic_category_enum_and_owner(self):
        sys_prompt = C.native_system_prompt(["Alpha", "Beta"], "Alex")
        self.assertIn("one of [Alpha, Beta]", sys_prompt)
        self.assertIn("for Alex.", sys_prompt)
        default = C.native_system_prompt()
        self.assertIn("one of [Action, Notification, Newsletter, Receipt, "
                      "Personal, Promo]", default)
        self.assertIn("for the account owner.", default)
        empty = C.native_system_prompt([])
        self.assertIn("one of [Action, Notification", empty)

    def test_request_params_match_engine(self):
        m = _msg("hello")
        engine = _engine_call(m, None, "", _VALID_JSON)
        kw = engine["calls"][0]["kw"]
        self.assertEqual(kw.get("max_tokens"), C.NATIVE_MAX_TOKENS)
        self.assertTrue(kw.get("json_mode"))
        self.assertTrue(kw.get("full"))


class NativeParserTests(unittest.TestCase):
    def test_valid_json_parsed(self):
        result = C.parse_native_response("prefix " + _VALID_JSON + " suffix")
        self.assertEqual(result["category"], "Action")
        self.assertEqual(C.native_output(result),
                         {"category": "Action", "needs_reply": True,
                          "confidence": 0.9, "summary": "s", "reason": "r"})

    def test_no_json_raises(self):
        with self.assertRaises(C.NativeParseError):
            C.parse_native_response("no json here")

    def test_missing_category_raises(self):
        with self.assertRaises(C.NativeParseError):
            C.parse_native_response('{"needs_reply": true}')

    def test_multiple_json_objects_raise_json_error(self):
        with self.assertRaises(json.JSONDecodeError):
            C.parse_native_response('{"a": 1} {"b": 2}')

    def test_parser_matches_engine(self):
        contents = [
            _VALID_JSON,
            "prefix " + _VALID_JSON + " suffix",
            "no json here",
            '{"needs_reply": true}',
            '{"a": 1} {"b": 2}',
            '```json\n' + _VALID_JSON + '\n```',
            "",
        ]
        m = _msg("hello")
        for content in contents:
            with self.subTest(content=content[:40]):
                engine = _engine_call(m, None, "", content)
                if engine["ok"]:
                    got = C.parse_native_response(content)
                    self.assertEqual(got, engine["result"])
                else:
                    with self.assertRaises(Exception) as ctx:
                        C.parse_native_response(content)
                    if engine["exc"] == "JSONDecodeError":
                        self.assertIsInstance(ctx.exception, json.JSONDecodeError)
                    else:
                        self.assertIsInstance(ctx.exception, RuntimeError)


class ProfileTests(unittest.TestCase):
    def test_profiles_are_separately_named(self):
        m = _msg("same email")
        native = C.build_native_request(m)
        policy = C.build_policy_request(
            m, {"policy_id": "default", "categories": ["Action", "Promo"]})
        full = C.build_full_context_request(m, "much longer body " * 200)
        self.assertEqual(native["profile"], C.NATIVE_PROFILE)
        self.assertEqual(policy["profile"], C.POLICY_PROFILE)
        self.assertEqual(full["profile"], C.FULL_CONTEXT_PROFILE)
        self.assertNotEqual(policy["profile"], native["profile"])
        self.assertNotEqual(full["profile"], native["profile"])
        self.assertEqual(policy["policy_id"], "default")

    def test_policy_requires_card(self):
        with self.assertRaises(C.ContractError):
            C.build_policy_request(_msg("x"), {})

    def test_full_context_is_richer_but_named(self):
        m = _msg("short")
        native = C.build_native_request(m)
        big = "L" * C.FULL_CONTEXT_LIMIT
        full = C.build_full_context_request(m, big)
        self.assertEqual(native["user"].split("\n\n", 1)[1], "short")
        body = full["user"].split("\n\n", 1)[1]
        self.assertEqual(len(body), C.FULL_CONTEXT_LIMIT)
        self.assertGreater(len(body), C.SNIPPET_LIMIT)
        self.assertEqual(full["profile"], C.FULL_CONTEXT_PROFILE)

    def test_policy_card_object_categories(self):
        policy = {"policy_id": "default",
                  "categories": [{"name": "Action", "description": "d", "folder": "Action"},
                                 {"name": "Receipt", "description": "d", "folder": "Receipts"}]}
        req = C.build_policy_request(_msg("x"), policy)
        self.assertEqual(req["categories"], ["Action", "Receipt"])
        self.assertIn("one of [Action, Receipt]", req["system"])


class ProvenanceTests(unittest.TestCase):
    def test_decision_only_never_claims_prose(self):
        prov = C.decision_only_provenance()
        self.assertEqual(prov["summary"], C.FIELD_MISSING)
        self.assertEqual(prov["reason"], C.FIELD_MISSING)
        self.assertEqual(C.validate_provenance(prov), [])
        self.assertFalse(C.has_fabricated_prose(prov))

    def test_fabricated_prose_detected(self):
        bad = {"category": "produced", "needs_reply": "produced",
               "confidence": "missing", "summary": "produced", "reason": "missing"}
        self.assertTrue(C.has_fabricated_prose(bad))

    def test_invalid_provenance_values(self):
        bad = C.decision_only_provenance()
        bad["category"] = "guessed"
        self.assertTrue(C.validate_provenance(bad))


class GoldIsolationTests(unittest.TestCase):
    def test_clean_input_no_leak(self):
        gold = {"gold_id": "gold_0001", "hidden_evidence": ["the deadline is Friday"]}
        req = C.build_native_request(_msg("Lunch tomorrow?"))
        self.assertEqual(C.find_gold_leakage(req["user"], gold), [])
        self.assertTrue(C.assert_no_gold_leakage(req["user"], gold))

    def test_leak_detected(self):
        gold = {"gold_id": "gold_0001", "hidden_evidence": ["the deadline is Friday"]}
        leaked = "Body text. The deadline is Friday. Body text."
        self.assertIn("the deadline is Friday",
                      C.find_gold_leakage(leaked, gold))
        with self.assertRaises(C.GoldLeakageError):
            C.assert_no_gold_leakage(leaked, gold)

    def test_serialized_gold_detected(self):
        gold = {"gold_id": "gold_0001", "answer": {"category": "Action"}}
        blob = json.dumps(gold, sort_keys=True, separators=(",", ":"))
        self.assertIn("<serialized-gold>",
                      C.find_gold_leakage("here: " + blob, gold))


class ObservabilityTests(unittest.TestCase):
    def test_observable_in(self):
        self.assertTrue(C.observable_in(C.OBSERVABILITY_VISIBLE, C.NATIVE_PROFILE))
        self.assertTrue(C.observable_in(C.OBSERVABILITY_VISIBLE, C.FULL_CONTEXT_PROFILE))
        self.assertFalse(C.observable_in(C.OBSERVABILITY_FULL_CONTEXT, C.NATIVE_PROFILE))
        self.assertTrue(C.observable_in(C.OBSERVABILITY_FULL_CONTEXT, C.FULL_CONTEXT_PROFILE))
        self.assertFalse(C.observable_in(C.OBSERVABILITY_AMBIGUOUS, C.NATIVE_PROFILE))
        self.assertFalse(C.observable_in(C.OBSERVABILITY_UNAVAILABLE, C.NATIVE_PROFILE))

    def test_projection(self):
        obs = {"category": "visible", "needs_reply": "full_context"}
        proj = C.project_observable_fields(obs, C.NATIVE_PROFILE)
        self.assertEqual(proj, {"category": True, "needs_reply": False})


class MimePathTests(unittest.TestCase):
    def test_mime_helpers_match_engine(self):
        samples = [
            "Content-Type: text/plain; charset=utf-8\n\nHello there",
            "-----Original Message-----\nFrom: x\n\nbody",
            "plain readable body with spaces",
            "SGVsbG8gd29ybGQgdGhpcyBpcyBhIGJhc2U2NCBydW4gdGhhdCBkZWNvZGVz",
            "",
        ]
        from benchmarks.v3.common import mime as M
        ns = _extract_engine()
        for s in samples:
            with self.subTest(s=s[:30]):
                self.assertEqual(M.looks_like_mime_junk(s),
                                 ns["looks_like_mime_junk"](s))
                self.assertEqual(M.readable_body(s, limit=1500),
                                 ns["readable_body"](s, limit=1500))
                req = C.build_native_request(_msg(s))
                expected = ns["readable_body"](s, 1500)[:1500] \
                    if ns["looks_like_mime_junk"](s or "") else (s or "")[:1500]
                self.assertEqual(req["user"].split("\n\n", 1)[1], expected)


if __name__ == "__main__":
    unittest.main()
