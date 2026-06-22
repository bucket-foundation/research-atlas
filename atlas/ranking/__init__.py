"""atlas.ranking -- a complete-citation-graph paper ranking + recommendation system.

This module is the *stronger* successor to Gian's original arXiv-only
"Academic Paper Ranking and Recommendation System". It fixes the three
weaknesses he stated in his own write-up:

1. **Incomplete citation graph** -- he dropped every cited paper that was not in
   the arXiv slice, so most edges vanished. We pull a coherent OpenAlex subfield
   with BOTH out-references (``referenced_works``) and the global in-citation
   count (``cited_by_count``), and build the in-corpus edge set so the graph is
   complete by construction within the corpus.
2. **In-citations undercounted -> PageRank came out ~uniform (an artifact).**
   With the complete in-corpus graph, PageRank produces real, heavy-tailed,
   non-uniform signal (see :mod:`atlas.ranking.rank`).
3. **TF-IDF / word2vec lose phrase meaning; no quantitative evaluation.** We add
   transformer (neural) embeddings of title+abstract (Ollama ``nomic-embed-text``)
   and a real held-out **citation-prediction** evaluation (Recall@k, MAP, MRR,
   with bootstrap CIs) that scores his baselines side by side against the
   transformer and graph methods.

Submodules
----------
- :mod:`atlas.ranking.corpus`   -- OpenAlex subfield corpus connector (works +
                                   abstracts + out-references), cached/idempotent.
- :mod:`atlas.ranking.graph`    -- build the complete in-corpus citation graph
                                   (CSR), out/in degree.
- :mod:`atlas.ranking.embed`    -- neural embeddings via Ollama; TF-IDF + word2vec
                                   baselines (Gian's methods, reimplemented).
- :mod:`atlas.ranking.rank`     -- PageRank (power method), citation count,
                                   field-normalized impact.
- :mod:`atlas.ranking.recommend`-- kNN recommenders over each representation.
- :mod:`atlas.ranking.evaluate` -- held-out citation-prediction eval + metrics.
"""
