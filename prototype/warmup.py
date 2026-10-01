import os, time
os.environ.setdefault("FASTEMBED_CACHE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_cache"))
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
t0 = time.time()
emb = TextEmbedding("Qwen/Qwen3-Embedding-0.6B")
t1 = time.time()
v = list(emb.embed(["warmup text"]))[0]
print("0.6B loaded in %.1fs; dim=%d; first embed %.2fs" % (t1 - t0, len(v), time.time() - t1), flush=True)
for name in ["Xenova/ms-marco-MiniLM-L-6-v2", "BAAI/bge-reranker-base", "jinaai/jina-reranker-v1-turbo-en"]:
    t = time.time()
    ce = TextCrossEncoder(name)
    s = list(ce.rerank("warmup query", ["doc one", "doc two"]))
    print("%s loaded+ran in %.1fs" % (name, time.time() - t), flush=True)
print("ALL MODELS READY")
