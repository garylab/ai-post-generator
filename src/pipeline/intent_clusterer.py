"""Deduplicate, embed, cluster, and score raw intents.

1. Exact-title dedup
2. Embed all intents via OpenAI
3. Deduplicate against existing intents in DB (pgvector)
4. Greedy centroid-based clustering
5. Score each intent (volume × inverse competition)
6. Pick pillar intent per cluster (broadest + highest volume)
7. Name clusters via GPT
"""
from __future__ import annotations

import re

import numpy as np

from src.config import settings
from src.storage.models import RawIntent
from src.utils.ai_client import chat_gpt, embed_texts
from src.storage import database as db
from loguru import logger as log

from src import settings_store


def _slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug[:60].strip("-")


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


async def _dedup_exact(intents: list[RawIntent]) -> list[RawIntent]:
    """Drop empty titles + dedup by source_url within the batch.

    Two intents with the same source_url are the same finding (e.g. the
    PAA box "What are the 5 hardest…" pops up under several seed keywords,
    but its reference link is identical). Different URLs = different
    findings, even if titles look similar.

    Intents without a source_url pass through unchanged.
    """
    seen_urls: set[str] = set()
    kept: list[RawIntent] = []
    for i in intents:
        if not (i.title or "").strip():
            continue
        url = (i.source_url or "").strip()
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
        kept.append(i)
    if len(kept) < len(intents):
        log.info("URL dedup: {} → {} intents", len(intents), len(kept))
    return kept


async def _dedup_semantic(
    intents: list[RawIntent],
    embeddings: list[list[float]],
) -> tuple[list[RawIntent], list[list[float]]]:
    """Remove semantically similar intents within the batch."""
    if len(intents) <= 1:
        return intents, embeddings

    dedup_sim = await settings_store.get("intent_dedup_similarity")
    matrix = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    normed = matrix / norms

    kept_intents: list[RawIntent] = []
    kept_embs: list[list[float]] = []
    kept_indices: list[int] = []

    for i, intent in enumerate(intents):
        is_dup = False
        if kept_indices:
            sims = normed[i] @ normed[kept_indices].T
            if np.max(sims) >= dedup_sim:
                is_dup = True
        if not is_dup:
            kept_intents.append(intent)
            kept_embs.append(embeddings[i])
            kept_indices.append(i)

    log.info("Semantic dedup: {} -> {} intents", len(intents), len(kept_intents))
    return kept_intents, kept_embs


async def _dedup_against_db(
    intents: list[RawIntent],
    embeddings: list[list[float]],
) -> tuple[list[RawIntent], list[list[float]]]:
    """Remove intents that already exist in the DB."""
    dedup_sim = await settings_store.get("intent_dedup_similarity")
    kept_intents: list[RawIntent] = []
    kept_embs: list[list[float]] = []

    for intent, emb in zip(intents, embeddings):
        existing = await db.find_similar_intent(emb, threshold=dedup_sim)
        if existing:
            log.debug("DB dedup dropped '{}' (sim to '{}')", intent.title, existing["title"])
        else:
            kept_intents.append(intent)
            kept_embs.append(emb)

    log.info("DB dedup: {} -> {} intents", len(intents), len(kept_intents))
    return kept_intents, kept_embs


def _cluster_intents(
    intents: list[RawIntent],
    embeddings: list[list[float]],
    cluster_sim: float = 0.70,
) -> list[dict]:
    """Greedy centroid-based clustering.

    Returns list of cluster dicts:
      {"intents": [...], "embeddings": [...], "centroid": np.ndarray}
    """
    if not intents:
        return []

    clusters: list[dict] = []
    vecs = [np.array(e, dtype=np.float32) for e in embeddings]

    for i, (intent, emb) in enumerate(zip(intents, vecs)):
        best_cluster = None
        best_sim = 0.0

        for cl in clusters:
            sim = _cosine_sim(emb, cl["centroid"])
            if sim > best_sim:
                best_sim = sim
                best_cluster = cl

        if best_cluster is not None and best_sim >= cluster_sim:
            best_cluster["intents"].append(intent)
            best_cluster["embeddings"].append(embeddings[i])
            n = len(best_cluster["intents"])
            best_cluster["centroid"] = (
                best_cluster["centroid"] * (n - 1) + emb
            ) / n
        else:
            clusters.append({
                "intents": [intent],
                "embeddings": [embeddings[i]],
                "centroid": emb.copy(),
            })

    return clusters


def _pick_pillar(cluster: dict) -> int:
    """Pick the pillar intent index: prefer broader (shorter title) + higher volume."""
    best_idx = 0
    best_score = -1.0
    for i, intent in enumerate(cluster["intents"]):
        brevity = max(0, 15 - len(intent.title.split())) / 15
        vol = intent.volume_hint / 10
        score = vol * 0.6 + brevity * 0.4
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def _score_intent(intent: RawIntent) -> float:
    """Score = volume_hint * (1 - competition_hint). Higher = more attractive."""
    vol = max(intent.volume_hint, 0)
    return round(vol * 1.0, 2)


async def _name_clusters(clusters: list[dict]) -> list[str]:
    """Use GPT to generate short names for clusters based on their intents."""
    if not clusters:
        return []

    cluster_blocks = []
    for i, cl in enumerate(clusters):
        titles = [intent.title for intent in cl["intents"][:10]]
        cluster_blocks.append(f"Cluster {i+1}: {', '.join(titles)}")

    prompt = (
        "Below are clusters of user search intents. "
        "Give each cluster a short name (2-4 words) that captures the theme. "
        "Return JSON: {\"names\": [\"name1\", \"name2\", ...]}\n\n"
        + "\n".join(cluster_blocks)
    )

    import json
    raw = await chat_gpt(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        response_format={"type": "json_object"},
        max_tokens=500,
    )

    try:
        data = json.loads(raw)
        names = data.get("names", [])
        while len(names) < len(clusters):
            names.append(f"Cluster {len(names) + 1}")
        return names[:len(clusters)]
    except Exception:
        return [f"Cluster {i+1}" for i in range(len(clusters))]


async def process_intents(
    raw_intents: list[RawIntent],
    batch_id: str,
    brand_id: int | None = None,
) -> dict:
    """Full pipeline: dedup → embed → match-existing-clusters → cluster leftovers → score → persist.

    Returns summary dict with counts.
    """
    # 1. Filter empty titles
    intents = await _dedup_exact(raw_intents)
    log.info("After filter: {} intents", len(intents))

    if not intents:
        return {"total": 0, "clusters": 0, "intents": 0}

    # 2. Embed
    titles = [i.title for i in intents]
    embeddings = await embed_texts(titles)
    log.info("Embedded {} intents", len(embeddings))

    # 3. Semantic dedup within this batch (catches near-duplicate phrasings)
    intents, embeddings = await _dedup_semantic(intents, embeddings)

    # 4. Dedup against existing DB intents (prevents re-mining across runs)
    intents, embeddings = await _dedup_against_db(intents, embeddings)

    if not intents:
        log.info("No new intents after dedup")
        return {"total": len(raw_intents), "clusters": 0, "intents": 0}

    # 5. Score intents
    for intent in intents:
        intent.volume_hint = max(intent.volume_hint, 0)

    cluster_sim = await settings_store.get("intent_cluster_similarity")

    # 6a. First match new intents against EXISTING clusters in the DB (same brand).
    #     This prevents micro-cluster fragmentation across runs.
    existing = await db.fetch_clusters_with_centroids(brand_id=brand_id)
    leftover_intents: list[RawIntent] = []
    leftover_embeddings: list[list[float]] = []
    matched_per_cluster: dict[int, list[tuple[RawIntent, list[float]]]] = {}
    matched_total = 0

    if existing:
        for intent, emb in zip(intents, embeddings):
            vec = np.array(emb, dtype=np.float32)
            best_id, best_sim, best_centroid, best_count = None, 0.0, None, 0
            for cl in existing:
                if not cl.get("centroid"):
                    continue
                cvec = np.array(cl["centroid"], dtype=np.float32)
                sim = _cosine_sim(vec, cvec)
                if sim > best_sim:
                    best_sim = sim
                    best_id = cl["id"]
                    best_centroid = cvec
                    best_count = cl.get("intent_count") or 0
            if best_id is not None and best_sim >= cluster_sim:
                matched_per_cluster.setdefault(best_id, []).append((intent, emb))
            else:
                leftover_intents.append(intent)
                leftover_embeddings.append(emb)
        log.info(
            "Matched {} intents to {} existing clusters; {} leftovers",
            sum(len(v) for v in matched_per_cluster.values()),
            len(matched_per_cluster),
            len(leftover_intents),
        )
    else:
        leftover_intents, leftover_embeddings = list(intents), list(embeddings)

    # Persist matches into existing clusters (and recompute centroids)
    matched_total = 0
    for cluster_id, items in matched_per_cluster.items():
        existing_cl = next(c for c in existing if c["id"] == cluster_id)
        existing_count = existing_cl.get("intent_count") or 0
        centroid = np.array(existing_cl["centroid"], dtype=np.float32)
        for intent, emb in items:
            evec = np.array(emb, dtype=np.float32)
            existing_count += 1
            centroid = (centroid * (existing_count - 1) + evec) / existing_count
            score_val = _score_intent(intent)
            await db.insert_intent(
                title=intent.title,
                embedding=emb,
                source=intent.source,
                source_url=intent.source_url,
                snippet=intent.snippet,
                volume_hint=intent.volume_hint,
                priority_score=score_val,
                cluster_id=cluster_id,
                is_pillar=False,
                batch_id=batch_id,
                brand_id=brand_id,
            )
            matched_total += 1
        await db.update_cluster_centroid(cluster_id, centroid.tolist(), existing_count)

    # 6b. Cluster the leftovers among themselves
    if not leftover_intents:
        log.info(
            "All {} intents absorbed into existing clusters", matched_total,
        )
        stats_after = await db.fetch_intent_stats()  # refresh after writes
        return {
            "total": len(raw_intents),
            "clusters": 0,
            "intents": matched_total,
        }

    intents = leftover_intents
    embeddings = leftover_embeddings
    clusters = _cluster_intents(intents, embeddings, cluster_sim=cluster_sim)
    log.info("Formed {} new clusters from {} leftover intents", len(clusters), len(intents))

    # 7. Name clusters via GPT
    names = await _name_clusters(clusters)

    # 8. Persist clusters and intents
    for cl, name in zip(clusters, names):
        pillar_idx = _pick_pillar(cl)
        slug = _slugify(name)

        # Score all intents in this cluster
        for intent in cl["intents"]:
            intent.volume_hint = max(intent.volume_hint, 0)

        scores = [_score_intent(intent) for intent in cl["intents"]]

        cluster_priority = sum(scores) / len(scores) if scores else 0

        cluster_id = await db.insert_intent_cluster(
            name=name,
            slug=slug,
            centroid_embedding=cl["centroid"].tolist(),
            intent_count=len(cl["intents"]),
            priority_score=cluster_priority,
            brand_id=brand_id,
        )

        intent_ids: list[int] = []
        for j, (intent, emb) in enumerate(zip(cl["intents"], cl["embeddings"])):
            intent_id = await db.insert_intent(
                title=intent.title,
                embedding=emb,
                source=intent.source,
                source_url=intent.source_url,
                snippet=intent.snippet,
                volume_hint=intent.volume_hint,
                priority_score=scores[j],
                cluster_id=cluster_id,
                is_pillar=(j == pillar_idx),
                batch_id=batch_id,
                brand_id=brand_id,
            )
            intent_ids.append(intent_id)

        # Set pillar_intent_id on the cluster
        if intent_ids:
            await db.update_intent_cluster_pillar(
                cluster_id=cluster_id,
                pillar_intent_id=intent_ids[pillar_idx],
            )

    total_intents = sum(len(cl["intents"]) for cl in clusters)
    log.info(
        "Persisted {} new clusters with {} intents + {} into existing (from {} raw)",
        len(clusters), total_intents, matched_total, len(raw_intents),
    )

    return {
        "total": len(raw_intents),
        "clusters": len(clusters),
        "intents": total_intents + matched_total,
    }
