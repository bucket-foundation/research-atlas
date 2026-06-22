"""Text representations: transformer (neural) embeddings + Gian's baselines.

Three representations of each work's ``title + abstract``, so the eval can score
them head to head:

1. **TF-IDF** (``tfidf_matrix``) -- Gian's primary representation. He combined
   TF-IDF with NMF; we keep TF-IDF (the part that drives his cosine recommender)
   and optionally NMF-reduce it, faithfully reproducing his method.
2. **word2vec mean-pooled** (``word2vec_matrix``) -- Gian's second method: he
   averaged Google word2vec word vectors over the abstract and took cosine
   similarity. We reproduce it with a self-trained word2vec-style embedding
   (a lightweight, dependency-free CBOW-ish factorization of the PPMI
   co-occurrence matrix) when ``gensim`` / Google vectors are unavailable, so the
   baseline runs anywhere; the *method* (mean-pooled static word vectors) is his.
3. **transformer** (``transformer_matrix``) -- the fix to weakness #3: contextual
   sentence embeddings from a neural model (Ollama ``nomic-embed-text``, 768-d,
   on Gian's local GPU box), which preserve phrase meaning ("supernova neutrinos"
   stays a phrase) that bag-of-words TF-IDF and mean-pooled word2vec destroy.

All matrices are **L2-normalized**, so cosine similarity is a plain dot product
(matching Gian's cosine recommender). Embeddings are cached to ``.npy`` keyed by
a hash of the input texts + model, so a re-run is idempotent and free.
"""

from __future__ import annotations

import hashlib
import json
import re
import requests
from pathlib import Path

import numpy as np

from atlas.connectors.base import DATA_RAW

OLLAMA_URL = "http://localhost:11434/api/embeddings"
OLLAMA_BATCH_URL = "http://localhost:11434/api/embed"  # accepts input: [..]
EMBED_MODEL = "nomic-embed-text"
CACHE_DIR = DATA_RAW / "ranking" / "embed_cache"
EMBED_MAX_CHARS = 4000  # nomic-embed-text context cap; truncate longer abstracts

# Default transformer backend: a GPU sentence-transformers model trained on
# scientific papers (SPECTER). Far stronger and far faster than the CPU Ollama
# path for this task. The Ollama functions above remain available but are no
# longer the default; ``embed_texts_by_id`` routes to the ST/GPU backend.
ST_MODEL = "sentence-transformers/allenai-specter"   # SPECTER: trained on papers
ST_FALLBACKS = ("nomic-ai/nomic-embed-text-v1.5",
                "sentence-transformers/all-MiniLM-L6-v2")

_TOKEN_RE = re.compile(r"[a-z][a-z0-9\-]+")


def _l2_normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, >=2 chars -- the shared tokenizer for the baselines."""
    return _TOKEN_RE.findall((text or "").lower())


# --------------------------------------------------------------------------- #
# 1. TF-IDF  (Gian's primary representation)
# --------------------------------------------------------------------------- #

def tfidf_matrix(texts: list[str], max_features: int = 20000,
                 min_df: int = 2, ngram: int = 1):
    """TF-IDF over title+abstract (Gian's method).

    Returns ``(matrix, vocab)`` where ``matrix`` is a dense L2-normalized
    ``(n_docs, n_terms)`` float32 array. Uses scikit-learn when present;
    otherwise a faithful pure-numpy fallback (smoothed idf, sublinear tf off)
    so the baseline runs with no extra dependency.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        vec = TfidfVectorizer(
            tokenizer=tokenize, preprocessor=lambda x: x or "",
            token_pattern=None, max_features=max_features, min_df=min_df,
            ngram_range=(1, ngram))
        m = vec.fit_transform(texts).astype(np.float32).toarray()
        return _l2_normalize(m), vec.get_feature_names_out().tolist()
    except Exception:
        return _tfidf_numpy(texts, max_features, min_df)


def _tfidf_numpy(texts, max_features, min_df):
    from collections import Counter
    df = Counter()
    toks_per_doc = []
    for t in texts:
        toks = tokenize(t)
        toks_per_doc.append(toks)
        for w in set(toks):
            df[w] += 1
    vocab_items = [(w, c) for w, c in df.items() if c >= min_df]
    vocab_items.sort(key=lambda x: (-x[1], x[0]))
    vocab_items = vocab_items[:max_features]
    vocab = [w for w, _ in vocab_items]
    vindex = {w: i for i, w in enumerate(vocab)}
    n, v = len(texts), len(vocab)
    m = np.zeros((n, v), dtype=np.float32)
    from collections import Counter as C
    for i, toks in enumerate(toks_per_doc):
        tf = C(t for t in toks if t in vindex)
        for w, c in tf.items():
            m[i, vindex[w]] = c
    idf = np.log((1 + n) / (1 + np.array([df[w] for w in vocab]))) + 1.0
    m = m * idf
    return _l2_normalize(m), vocab


def nmf_reduce(tfidf, n_components: int = 25, seed: int = 0):
    """NMF factorization of the TF-IDF matrix (Gian's V ~= WH topic step).

    Returns the document-topic matrix W (L2-normalized), the exact representation
    his cosine recommender ran on. Falls back to truncated SVD if sklearn NMF is
    unavailable.
    """
    try:
        from sklearn.decomposition import NMF
        model = NMF(n_components=n_components, init="nndsvda",
                    random_state=seed, max_iter=400)
        w = model.fit_transform(np.clip(tfidf, 0, None))
        return _l2_normalize(w.astype(np.float32))
    except Exception:
        # SVD fallback (sign-indefinite but fine as a reduced representation)
        u, s, _ = np.linalg.svd(tfidf, full_matrices=False)
        return _l2_normalize((u[:, :n_components] * s[:n_components]).astype(np.float32))


# --------------------------------------------------------------------------- #
# 2. word2vec mean-pooled  (Gian's second method)
# --------------------------------------------------------------------------- #

def word2vec_matrix(texts: list[str], dim: int = 100, min_df: int = 5,
                    window: int = 5, seed: int = 0):
    """Mean-pooled static word vectors per document (Gian's word2vec method).

    Gian used Google's pretrained word2vec, averaged word vectors over each
    abstract, then cosine similarity. Google's 3.5GB binary is not assumed
    present, so we train an equivalent *static* word embedding on the corpus via
    a PPMI + truncated-SVD factorization of the co-occurrence matrix (the classic
    result that SVD-on-PPMI approximates word2vec/SGNS, Levy & Goldberg 2014),
    then mean-pool exactly as he did. The representation class -- static word
    vectors, mean-pooled, cosine -- is identical to his; only the training corpus
    differs (in-domain here, which is if anything fairer to the baseline).
    """
    from collections import Counter
    rng = np.random.default_rng(seed)
    toks_per_doc = [tokenize(t) for t in texts]
    df = Counter()
    for toks in toks_per_doc:
        for w in set(toks):
            df[w] += 1
    vocab = [w for w, c in df.items() if c >= min_df]
    vindex = {w: i for i, w in enumerate(vocab)}
    v = len(vocab)
    if v == 0:
        return np.zeros((len(texts), dim), dtype=np.float32), []

    # symmetric co-occurrence counts within a window
    cooc = np.zeros((v, v), dtype=np.float64)
    word_count = np.zeros(v, dtype=np.float64)
    total = 0.0
    for toks in toks_per_doc:
        ids = [vindex[w] for w in toks if w in vindex]
        for p, wi in enumerate(ids):
            word_count[wi] += 1
            total += 1
            lo, hi = max(0, p - window), min(len(ids), p + window + 1)
            for q in range(lo, hi):
                if q != p:
                    cooc[wi, ids[q]] += 1.0
    # PPMI
    with np.errstate(divide="ignore", invalid="ignore"):
        p_w = word_count / max(total, 1.0)
        p_ij = cooc / max(cooc.sum(), 1.0)
        pmi = np.log(p_ij / (np.outer(p_w, p_w) + 1e-12) + 1e-12)
    ppmi = np.maximum(pmi, 0.0)
    # truncated SVD -> word vectors (SGNS-equivalent)
    k = min(dim, v - 1) if v > 1 else 1
    try:
        u, s, _ = np.linalg.svd(ppmi, full_matrices=False)
        wv = u[:, :k] * np.sqrt(s[:k])
    except np.linalg.LinAlgError:
        wv = rng.normal(size=(v, k))
    if wv.shape[1] < dim:
        wv = np.pad(wv, ((0, 0), (0, dim - wv.shape[1])))

    # mean-pool per document (Gian's pooling), then L2-normalize
    n = len(texts)
    doc = np.zeros((n, dim), dtype=np.float32)
    for i, toks in enumerate(toks_per_doc):
        ids = [vindex[w] for w in toks if w in vindex]
        if ids:
            doc[i] = wv[ids].mean(axis=0)
    return _l2_normalize(doc), vocab


# --------------------------------------------------------------------------- #
# 3. transformer embeddings  (the fix to weakness #3)
# --------------------------------------------------------------------------- #

BY_ID_DIR = DATA_RAW / "ranking" / "embed_cache" / "by_id"


def _embed_batch(sess, inputs: list[str], model: str, url: str,
                 retries: int = 4):
    """Embed a list of texts in one Ollama /api/embed call (batched).

    Batching amortizes per-request overhead and lets Ollama process the inputs
    together -- on this CPU box it is ~3x the serial throughput. Returns a list
    of float32 vectors aligned to ``inputs``, or None on persistent failure (the
    caller then falls back to per-item embedding so one bad text can't sink a
    whole batch).
    """
    import time

    payload = {"model": model,
               "input": [(t or " ")[:EMBED_MAX_CHARS] for t in inputs]}
    for attempt in range(retries):
        try:
            resp = sess.post(url, json=payload, timeout=300)
            if resp.status_code == 200:
                embs = resp.json().get("embeddings")
                if embs and len(embs) == len(inputs):
                    return [np.array(e, dtype=np.float32) for e in embs]
            # context overflow -> shrink the whole batch and retry
            if "context length" in resp.text:
                payload["input"] = [t[: max(500, len(t) // 2)]
                                    for t in payload["input"]]
                continue
        except requests.RequestException:
            pass
        time.sleep(1.0 * (2 ** attempt))
    return None


_ST_CACHE: dict[str, object] = {}


def _load_st_model(prefer: str = ST_MODEL):
    """Load a SentenceTransformer on GPU (ROCm) when available, else CPU.

    Sets ``HSA_OVERRIDE_GFX_VERSION`` **before** importing torch so the AMD
    RX 7700S is recognized by ROCm. Tries SPECTER first (trained on scientific
    papers -- the right tool for paper recommendation), then the configured
    fallbacks if the download fails. Returns ``(model, model_name, device)``.
    Cached per process so repeated calls don't reload weights.
    """
    import os
    os.environ.setdefault("HSA_OVERRIDE_GFX_VERSION", "11.0.0")
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    candidates = [prefer, *[f for f in ST_FALLBACKS if f != prefer]]
    last_err = None
    for name in candidates:
        ck = f"{name}@{device}"
        if ck in _ST_CACHE:
            return _ST_CACHE[ck], name, device
        try:
            kw = {"device": device}
            if "nomic" in name:           # nomic ships custom code
                kw["trust_remote_code"] = True
            model = SentenceTransformer(name, **kw)
            _ST_CACHE[ck] = model
            return model, name, device
        except Exception as exc:          # download / load failure -> next
            last_err = exc
            print(f"    [st] {name} unavailable ({exc}); trying fallback",
                  flush=True)
    raise RuntimeError(f"no sentence-transformers model could load: {last_err}")


def embed_texts_by_id_st(work_ids: list[str], texts: list[str],
                         model_name: str = ST_MODEL,
                         by_id_dir: Path = BY_ID_DIR, batch_size: int = 64,
                         log_every: int = 512) -> int:
    """GPU sentence-transformers backend (default). Writes ``<wid>.npy`` per work.

    Resumable + idempotent: skips ids already on disk, batch-encodes the rest on
    the GPU, L2-normalizes, and writes the SAME per-id cache the eval consumes
    (one float32 vector per work id). Returns the number newly embedded.
    """
    by_id_dir.mkdir(parents=True, exist_ok=True)
    todo = [(w, t) for w, t in zip(work_ids, texts)
            if not (by_id_dir / f"{w}.npy").exists()]
    if not todo:
        return 0

    model, name, device = _load_st_model(model_name)
    print(f"    [st] {name} on {device}", flush=True)
    done = 0
    for s in range(0, len(todo), batch_size):
        chunk = todo[s:s + batch_size]
        batch = [(t or " ")[:EMBED_MAX_CHARS] for _, t in chunk]
        embs = model.encode(batch, batch_size=batch_size,
                            convert_to_numpy=True, normalize_embeddings=True,
                            show_progress_bar=False)
        embs = embs.astype(np.float32)
        for (w, _), v in zip(chunk, embs):
            np.save(by_id_dir / f"{w}.npy", v)
            done += 1
        if log_every and done % log_every < batch_size:
            print(f"    embedded {done}/{len(todo)} (gpu batch)", flush=True)
    return done


def embed_texts_by_id(work_ids: list[str], texts: list[str],
                      model: str | None = None, url: str = OLLAMA_BATCH_URL,
                      by_id_dir: Path = BY_ID_DIR, batch_size: int = 16,
                      log_every: int = 256, backend: str = "st") -> int:
    """Batch-embed the uncached works, writing one ``<wid>.npy`` per work.

    Resumable + idempotent: skips ids already on disk, batches the rest. Returns
    the number newly embedded. This is the fast path used by both the embed
    script and the eval.

    ``backend="st"`` (default) uses the GPU sentence-transformers SPECTER model;
    ``backend="ollama"`` uses the legacy CPU Ollama HTTP path (kept available but
    no longer default -- it ran ~0.5 docs/s).
    """
    if backend == "st":
        return embed_texts_by_id_st(
            work_ids, texts, model_name=model or ST_MODEL,
            by_id_dir=by_id_dir, batch_size=max(batch_size, 64),
            log_every=max(log_every, 512))

    # ---- legacy Ollama CPU path (opt-in) --------------------------------- #
    import requests as _rq

    model = model or EMBED_MODEL
    by_id_dir.mkdir(parents=True, exist_ok=True)
    sess = _rq.Session()
    todo = [(w, t) for w, t in zip(work_ids, texts)
            if not (by_id_dir / f"{w}.npy").exists()]
    done = 0
    for s in range(0, len(todo), batch_size):
        chunk = todo[s:s + batch_size]
        vecs = _embed_batch(sess, [t for _, t in chunk], model, url)
        if vecs is None:
            # fall back to per-item so a single bad input can't sink the batch
            vecs = []
            for _, t in chunk:
                v = _embed_one_serial(sess, t, model)
                vecs.append(v)
        for (w, _), v in zip(chunk, vecs):
            np.save(by_id_dir / f"{w}.npy", v)
            done += 1
        if log_every and done % log_every < batch_size:
            print(f"    embedded {done}/{len(todo)} (batched)", flush=True)
    return done


def _embed_one_serial(sess, text, model, url: str = OLLAMA_URL, retries=5):
    import time
    budget = EMBED_MAX_CHARS
    for attempt in range(retries):
        prompt = (text or " ")[:budget] or " "
        try:
            resp = sess.post(url, json={"model": model, "prompt": prompt},
                             timeout=120)
            if resp.status_code == 200:
                return np.array(resp.json()["embedding"], dtype=np.float32)
            if "context length" in resp.text:
                budget = max(500, budget // 2)
                continue
        except requests.RequestException:
            pass
        time.sleep(1.0 * (2 ** attempt))
    raise RuntimeError("serial embed failed")


def transformer_matrix_by_id(work_ids: list[str], texts: list[str],
                             model: str | None = None, url: str = OLLAMA_URL,
                             by_id_dir: Path = BY_ID_DIR,
                             batch_log: int = 200,
                             backend: str = "st") -> np.ndarray:
    """Transformer embeddings keyed by work id (resumable per-id cache).

    Reads ``<by_id_dir>/<work_id>.npy`` when present; otherwise embeds and writes
    it. Default backend is GPU sentence-transformers (SPECTER); ``backend=
    "ollama"`` uses the legacy CPU path. Returns an L2-normalized ``(n, dim)``
    array aligned to ``work_ids``. This is the path the eval uses so an
    interrupted embed resumes and repeated eval runs are free.
    See :mod:`scripts.ranking_embed`.
    """
    # 1) batch-fill the cache for any uncached work (fast path)
    embed_texts_by_id(work_ids, texts, model=model, by_id_dir=by_id_dir,
                      backend=backend)
    # 2) load all vectors from the per-id cache in order
    vecs = [np.load(by_id_dir / f"{wid}.npy") for wid in work_ids]
    return _l2_normalize(np.array(vecs, dtype=np.float32))


def _cache_key(texts: list[str], model: str) -> str:
    h = hashlib.sha1()
    h.update(model.encode())
    h.update(str(len(texts)).encode())
    # sample fingerprint so we don't hash megabytes; covers order + content
    for t in texts[:50] + texts[-50:]:
        h.update((t or "").encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def transformer_matrix(texts: list[str], model: str = EMBED_MODEL,
                       url: str = OLLAMA_URL, batch_log: int = 500,
                       cache: bool = True) -> np.ndarray:
    """Neural sentence embeddings of title+abstract via Ollama (local GPU).

    Returns an L2-normalized ``(n_docs, dim)`` float32 array (``nomic-embed-text``
    is 768-d). Cached to ``data/raw/ranking/embed_cache/<hash>.npy`` so a re-run
    is free and idempotent. This is the contextual representation that fixes
    Gian's phrase-meaning loss (#3).
    """
    import requests

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(texts, model)
    cpath = CACHE_DIR / f"transformer_{key}.npy"
    if cache and cpath.exists():
        return np.load(cpath)

    sess = requests.Session()
    vecs: list[list[float]] = []
    dim = None
    for i, t in enumerate(texts):
        payload = {"model": model, "prompt": (t or "")[:8000]}
        resp = sess.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        e = resp.json()["embedding"]
        if dim is None:
            dim = len(e)
        vecs.append(e)
        if batch_log and (i + 1) % batch_log == 0:
            print(f"    embedded {i + 1}/{len(texts)}", flush=True)
    m = _l2_normalize(np.array(vecs, dtype=np.float32))
    if cache:
        np.save(cpath, m)
        (CACHE_DIR / f"transformer_{key}.json").write_text(
            json.dumps({"model": model, "n": len(texts), "dim": dim}))
    return m
