-- ============================================================
-- EXTENSION: pgvector for embedding-based deduplication
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- ENUM-LIKE DOMAINS
-- ============================================================

DO $$ BEGIN
  CREATE TYPE content_status AS ENUM (
    'researched', 'generated', 'enriched',
    'draft', 'approved', 'rejected', 'published', 'archived'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE priority_level AS ENUM (
    'high', 'medium', 'low', 'discard'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE platform_type AS ENUM (
    'website', 'twitter', 'linkedin', 'medium', 'facebook', 'wechat'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE cta_variant AS ENUM ('A', 'B');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE tracking_event AS ENUM (
    'impression', 'click', 'page_view', 'signup', 'share'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE intent_status AS ENUM (
    'pending', 'queued', 'covered', 'refresh_needed'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE intent_cluster_status AS ENUM (
    'mining', 'active', 'covered', 'expanding'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- TABLE: intent_clusters
-- Dynamic topic groups formed by embedding similarity.
-- Each cluster produces a pillar article + supporting articles.
-- ============================================================

CREATE TABLE IF NOT EXISTS intent_clusters (
  id                  SERIAL                   PRIMARY KEY,
  name                TEXT                     NOT NULL,
  slug                TEXT                     UNIQUE NOT NULL,
  centroid_embedding  vector(1536),
  pillar_intent_id    BIGINT,
  pillar_content_id   TEXT,
  status              intent_cluster_status    NOT NULL DEFAULT 'active',
  intent_count        INTEGER                  NOT NULL DEFAULT 0,
  covered_count       INTEGER                  NOT NULL DEFAULT 0,
  priority_score      NUMERIC(6,2)             NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ              NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ              NOT NULL DEFAULT NOW()
);

-- ============================================================
-- TABLE: intents
-- User search intents mined from autocomplete, PAA, forums, trends.
-- The atomic unit of the growth engine.
-- ============================================================

CREATE TABLE IF NOT EXISTS intents (
  id                  BIGSERIAL                PRIMARY KEY,
  title               TEXT                     NOT NULL,
  embedding           vector(1536),
  source              TEXT                     NOT NULL,
  source_url          TEXT,
  snippet             TEXT                     NOT NULL DEFAULT '',
  volume_hint         NUMERIC(6,1)             NOT NULL DEFAULT 0,
  competition_hint    NUMERIC(4,2)             NOT NULL DEFAULT 0,
  priority_score      NUMERIC(6,2)             NOT NULL DEFAULT 0,
  cluster_id          INTEGER                  REFERENCES intent_clusters(id) ON DELETE SET NULL,
  content_id          TEXT,
  is_pillar           BOOLEAN                  NOT NULL DEFAULT FALSE,
  status              intent_status            NOT NULL DEFAULT 'pending',
  batch_id            UUID                     NOT NULL DEFAULT gen_random_uuid(),
  created_at          TIMESTAMPTZ              NOT NULL DEFAULT NOW(),
  covered_at          TIMESTAMPTZ
);

-- ============================================================
-- TABLE: content
-- Generated articles + social posts + CTA variants.
-- ============================================================

CREATE TABLE IF NOT EXISTS content (
  id                    BIGSERIAL                PRIMARY KEY,
  content_id            TEXT                     UNIQUE NOT NULL,
  intent_id             BIGINT                   REFERENCES intents(id) ON DELETE SET NULL,
  title                 TEXT                     NOT NULL,
  title_embedding       vector(1536),
  research_data         JSONB                    NOT NULL DEFAULT '{}'::jsonb,
  article_html          TEXT,
  medium_article        TEXT,
  wechat_article        TEXT,
  outline               JSONB                    NOT NULL DEFAULT '[]'::jsonb,
  social_posts          JSONB                    NOT NULL DEFAULT '{}'::jsonb,
  social_posts_variant_b JSONB                   NOT NULL DEFAULT '{}'::jsonb,
  seo_keywords          JSONB                    NOT NULL DEFAULT '[]'::jsonb,
  meta_description      TEXT,
  image_url             TEXT,
  score                 NUMERIC(4,1)             NOT NULL DEFAULT 0,
  cluster               TEXT                     REFERENCES intent_clusters(slug) ON DELETE SET NULL,
  suggested_angle       TEXT,
  priority              priority_level  NOT NULL DEFAULT 'medium',
  cta_variant_a         TEXT,
  cta_variant_b         TEXT,
  active_cta            cta_variant     NOT NULL DEFAULT 'A',
  status                content_status  NOT NULL DEFAULT 'draft',
  iteration_count       INTEGER                  NOT NULL DEFAULT 0,
  approved_at           TIMESTAMPTZ,
  created_at            TIMESTAMPTZ              NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ              NOT NULL DEFAULT NOW()
);

-- ============================================================
-- TABLE: publish_logs
-- One row per platform per content publish event.
-- ============================================================

CREATE TABLE IF NOT EXISTS publish_logs (
  id              BIGSERIAL                PRIMARY KEY,
  content_id      TEXT                     NOT NULL REFERENCES content(content_id) ON DELETE CASCADE,
  platform        platform_type   NOT NULL,
  published_url   TEXT,
  post_body       TEXT,
  utm_source      TEXT,
  utm_medium      TEXT,
  utm_campaign    TEXT,
  utm_content     TEXT,
  cta_variant     cta_variant,
  response_data   JSONB                    DEFAULT '{}'::jsonb,
  published_at    TIMESTAMPTZ              NOT NULL DEFAULT NOW()
);

-- ============================================================
-- TABLE: tracking_events
-- Raw inbound events from the tracking webhook (click, signup…).
-- Append-only log; aggregated into performance by the daily cron.
-- ============================================================

CREATE TABLE IF NOT EXISTS tracking_events (
  id              BIGSERIAL                PRIMARY KEY,
  content_id      TEXT                     NOT NULL,
  platform        platform_type,
  event_type      tracking_event  NOT NULL,
  referrer        TEXT,
  user_agent      TEXT,
  ip_hash         TEXT,
  metadata        JSONB                    DEFAULT '{}'::jsonb,
  received_at     TIMESTAMPTZ              NOT NULL DEFAULT NOW()
);

-- ============================================================
-- TABLE: performance
-- Aggregated metrics per content × platform window.
-- ============================================================

CREATE TABLE IF NOT EXISTS performance (
  id              BIGSERIAL       PRIMARY KEY,
  content_id      TEXT            NOT NULL REFERENCES content(content_id) ON DELETE CASCADE,
  platform        platform_type NOT NULL,
  impressions     INTEGER         NOT NULL DEFAULT 0,
  clicks          INTEGER         NOT NULL DEFAULT 0,
  ctr             NUMERIC(6,2)   NOT NULL DEFAULT 0,
  landing_visits  INTEGER         NOT NULL DEFAULT 0,
  signups         INTEGER         NOT NULL DEFAULT 0,
  conversion_rate NUMERIC(6,2)   NOT NULL DEFAULT 0,
  likes           INTEGER         NOT NULL DEFAULT 0,
  shares          INTEGER         NOT NULL DEFAULT 0,
  comments        INTEGER         NOT NULL DEFAULT 0,
  cta_variant     cta_variant,
  period_start    TIMESTAMPTZ     NOT NULL DEFAULT date_trunc('day', NOW()),
  period_end      TIMESTAMPTZ     NOT NULL DEFAULT date_trunc('day', NOW()) + INTERVAL '1 day',
  measured_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

  CONSTRAINT uq_perf_content_platform_period
    UNIQUE (content_id, platform, period_start)
);

-- ============================================================
-- TABLE: ab_results
-- CTA A/B test outcomes per cluster for the learning loop.
-- ============================================================

CREATE TABLE IF NOT EXISTS ab_results (
  id              SERIAL          PRIMARY KEY,
  cluster         TEXT            NOT NULL REFERENCES intent_clusters(slug) ON DELETE CASCADE,
  variant_a_impressions INTEGER   NOT NULL DEFAULT 0,
  variant_a_clicks     INTEGER   NOT NULL DEFAULT 0,
  variant_a_signups    INTEGER   NOT NULL DEFAULT 0,
  variant_b_impressions INTEGER   NOT NULL DEFAULT 0,
  variant_b_clicks     INTEGER   NOT NULL DEFAULT 0,
  variant_b_signups    INTEGER   NOT NULL DEFAULT 0,
  winner          cta_variant,
  confidence      NUMERIC(5,2)   DEFAULT 0,
  computed_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

  CONSTRAINT uq_ab_cluster UNIQUE (cluster)
);

-- ============================================================
-- TABLE: dashboard_snapshots
-- Daily snapshots of growth metrics for trend analysis.
-- ============================================================

CREATE TABLE IF NOT EXISTS dashboard_snapshots (
  id              BIGSERIAL       PRIMARY KEY,
  snapshot_date   DATE            NOT NULL DEFAULT CURRENT_DATE,
  total_content   INTEGER         NOT NULL DEFAULT 0,
  total_published INTEGER         NOT NULL DEFAULT 0,
  total_clicks    INTEGER         NOT NULL DEFAULT 0,
  total_signups   INTEGER         NOT NULL DEFAULT 0,
  overall_ctr     NUMERIC(6,2)   NOT NULL DEFAULT 0,
  overall_conv    NUMERIC(6,2)   NOT NULL DEFAULT 0,
  top_cluster     TEXT,
  top_platform    TEXT,
  cluster_breakdown JSONB         NOT NULL DEFAULT '[]'::jsonb,
  platform_breakdown JSONB        NOT NULL DEFAULT '[]'::jsonb,
  ab_summary      JSONB           NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

  CONSTRAINT uq_snapshot_date UNIQUE (snapshot_date)
);

-- ============================================================
-- INDEXES
-- ============================================================

-- intent_clusters
CREATE INDEX IF NOT EXISTS idx_icluster_status      ON intent_clusters (status);
CREATE INDEX IF NOT EXISTS idx_icluster_priority    ON intent_clusters (priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_icluster_centroid    ON intent_clusters USING hnsw (centroid_embedding vector_cosine_ops);

-- intents
CREATE INDEX IF NOT EXISTS idx_intent_status        ON intents (status);
CREATE INDEX IF NOT EXISTS idx_intent_cluster       ON intents (cluster_id);
CREATE INDEX IF NOT EXISTS idx_intent_embedding     ON intents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_intent_batch         ON intents (batch_id);
CREATE INDEX IF NOT EXISTS idx_intent_created       ON intents (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intent_priority      ON intents (priority_score DESC);

-- content
CREATE INDEX IF NOT EXISTS idx_content_embedding    ON content USING hnsw (title_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_content_status       ON content (status);
CREATE INDEX IF NOT EXISTS idx_content_cluster      ON content (cluster);
CREATE INDEX IF NOT EXISTS idx_content_priority     ON content (priority);
CREATE INDEX IF NOT EXISTS idx_content_created      ON content (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_score        ON content (score DESC);
CREATE INDEX IF NOT EXISTS idx_content_status_score ON content (status, score DESC);

-- publish_logs
CREATE INDEX IF NOT EXISTS idx_publish_content      ON publish_logs (content_id);
CREATE INDEX IF NOT EXISTS idx_publish_platform     ON publish_logs (platform);
CREATE INDEX IF NOT EXISTS idx_publish_at           ON publish_logs (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_publish_content_plat ON publish_logs (content_id, platform);

-- tracking_events
CREATE INDEX IF NOT EXISTS idx_track_content        ON tracking_events (content_id);
CREATE INDEX IF NOT EXISTS idx_track_event          ON tracking_events (event_type);
CREATE INDEX IF NOT EXISTS idx_track_received       ON tracking_events (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_track_content_event  ON tracking_events (content_id, event_type);

-- performance
CREATE INDEX IF NOT EXISTS idx_perf_content         ON performance (content_id);
CREATE INDEX IF NOT EXISTS idx_perf_platform        ON performance (platform);
CREATE INDEX IF NOT EXISTS idx_perf_period          ON performance (period_start DESC);
CREATE INDEX IF NOT EXISTS idx_perf_content_plat    ON performance (content_id, platform);
CREATE INDEX IF NOT EXISTS idx_perf_ctr             ON performance (ctr DESC);
CREATE INDEX IF NOT EXISTS idx_perf_conversion      ON performance (conversion_rate DESC);

-- ============================================================
-- FUNCTION: auto-update updated_at on row modification
-- ============================================================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
  CREATE TRIGGER trg_content_updated
    BEFORE UPDATE ON content
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TRIGGER trg_intent_clusters_updated
    BEFORE UPDATE ON intent_clusters
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- VIEW: content_with_performance
-- Joins content with its latest aggregated performance metrics.
-- ============================================================

CREATE OR REPLACE VIEW content_with_performance AS
SELECT
  c.content_id,
  c.title,
  c.cluster,
  c.score,
  c.priority,
  c.status,
  c.active_cta,
  c.iteration_count,
  c.created_at,
  COALESCE(SUM(p.impressions), 0)       AS total_impressions,
  COALESCE(SUM(p.clicks), 0)            AS total_clicks,
  COALESCE(SUM(p.signups), 0)           AS total_signups,
  CASE
    WHEN SUM(p.impressions) > 0
    THEN ROUND(SUM(p.clicks)::NUMERIC / SUM(p.impressions) * 100, 2)
    ELSE 0
  END                                    AS overall_ctr,
  CASE
    WHEN SUM(p.clicks) > 0
    THEN ROUND(SUM(p.signups)::NUMERIC / SUM(p.clicks) * 100, 2)
    ELSE 0
  END                                    AS overall_conversion
FROM content c
LEFT JOIN performance p ON c.content_id = p.content_id
GROUP BY c.content_id, c.title, c.cluster, c.score,
         c.priority, c.status, c.active_cta, c.iteration_count, c.created_at;

-- ============================================================
-- VIEW: cluster_performance
-- Aggregated performance per cluster for the feedback loop.
-- ============================================================

CREATE OR REPLACE VIEW cluster_performance AS
SELECT
  ic.slug                                AS cluster,
  ic.name                                AS label,
  COUNT(DISTINCT c.content_id)           AS total_content,
  COALESCE(AVG(p.ctr), 0)               AS avg_ctr,
  COALESCE(AVG(p.conversion_rate), 0)    AS avg_conversion,
  COALESCE(SUM(p.signups), 0)           AS total_signups,
  ic.updated_at
FROM intent_clusters ic
LEFT JOIN content c   ON ic.slug = c.cluster AND c.status IN ('approved', 'published')
LEFT JOIN performance p ON c.content_id = p.content_id
GROUP BY ic.slug, ic.name, ic.updated_at;

-- ============================================================
-- VIEW: low_ctr_candidates
-- Content eligible for hook/CTA regeneration.
-- ============================================================

CREATE OR REPLACE VIEW low_ctr_candidates AS
SELECT
  c.content_id,
  c.title,
  c.cluster,
  c.score,
  c.iteration_count,
  c.created_at,
  AVG(p.ctr)              AS avg_ctr,
  AVG(p.conversion_rate)  AS avg_conversion
FROM content c
JOIN performance p ON c.content_id = p.content_id
WHERE c.status IN ('approved', 'published')
  AND c.score >= 7
  AND c.iteration_count < 3
  AND c.created_at < NOW() - INTERVAL '48 hours'
GROUP BY c.content_id, c.title, c.cluster, c.score, c.iteration_count, c.created_at
HAVING AVG(p.ctr) < 2;

-- ============================================================
-- ROLES (each role = a brand/persona with its own keywords + social accounts)
-- ============================================================
CREATE TABLE IF NOT EXISTS roles (
  id           SERIAL PRIMARY KEY,
  slug         TEXT UNIQUE NOT NULL,
  name         TEXT NOT NULL,
  description  TEXT NOT NULL DEFAULT '',
  enabled      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS role_social_accounts (
  id            SERIAL PRIMARY KEY,
  role_id       INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  platform      TEXT NOT NULL,
  display_name  TEXT NOT NULL DEFAULT '',
  credentials   JSONB NOT NULL DEFAULT '{}'::jsonb,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (role_id, platform, display_name)
);
CREATE INDEX IF NOT EXISTS idx_role_social_role ON role_social_accounts(role_id);

-- ============================================================
-- SEED KEYWORDS (manageable from dashboard, scoped per role)
-- ============================================================
CREATE TABLE IF NOT EXISTS seed_keywords (
  id SERIAL PRIMARY KEY,
  role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
  keyword TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (role_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_seed_kw_role ON seed_keywords(role_id);

-- Per-role tagging on existing pipeline tables
ALTER TABLE intents ADD COLUMN IF NOT EXISTS role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL;
ALTER TABLE intent_clusters ADD COLUMN IF NOT EXISTS role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL;
ALTER TABLE content ADD COLUMN IF NOT EXISTS role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_intents_role ON intents(role_id);
CREATE INDEX IF NOT EXISTS idx_clusters_role ON intent_clusters(role_id);
CREATE INDEX IF NOT EXISTS idx_content_role ON content(role_id);
