import os, time
os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_cache"))
from fastembed.rerank.cross_encoder import TextCrossEncoder
for name in ["Xenova/ms-marco-MiniLM-L-6-v2", "Xenova/ms-marco-MiniLM-L-12-v2",
             "BAAI/bge-reranker-base", "jinaai/jina-reranker-v1-turbo-en"]:
    t = time.time()
    ce = TextCrossEncoder(name)
    s = list(ce.rerank("warmup query", ["doc one", "doc two"]))
    print("%s loaded+scored in %.1fs -> %s" % (name, time.time() - t, [round(x,3) for x in s]), flush=True)
print("RERANKERS READY")
