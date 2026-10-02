"""Plugin data parity (mt-model-bench <-> benchmark v2).

`plugins/mt-model-bench/dist/plugin.js` embeds a bounded subset of the v2 case
set so the in-app benchmark runs inside the QuickJS sandbox.  These tests fail
if that embedded data drifts from the frozen cases, or if the subset loses the
adversarial coverage the plugin's scorecard relies on.
"""
import os
import sys

HERE = os.path.dirname(__file__)
V2 = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, V2)

from harness import gen_plugin_data as G  # noqa: E402


def test_plugin_data_matches_v2_cases():
    data = G.build_data(include_reference=False)
    ok, key = G.data_matches_embedded(data)
    assert ok, ("embedded plugin %s differs from benchmarks/v2/cases — run "
                "`python benchmarks/v2/harness/gen_plugin_data.py --write`" % key)


def test_plugin_subset_shape_and_coverage():
    data = G.build_data(include_reference=False)
    assert len(data["classify"]) == 21
    assert sum(1 for c in data["classify"] if c["quick"]) == 11
    assert len(data["draft"]) == 1
    assert len(data["rules"]) == 1
    assert len(data["summary"]) == 1
    # the scorecard's safety check depends on at least one label-injection case
    assert any(c["expect"].get("forbidden_labels") for c in data["classify"]), \
        "no label-injection case in the embedded subset"
    # every classification entry must carry the fields the plugin prompts use
    for c in data["classify"]:
        for f in ("from", "to", "subject", "date", "body"):
            assert f in c["msg"], "case %s missing msg.%s" % (c["id"], f)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
