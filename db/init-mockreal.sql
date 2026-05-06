-- ============================================================
-- EXTENSION: pgvector for embedding-based deduplication
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- ENUM-LIKE DOMAINS
-- ============================================================

DO $$ BEGIN
  CREATE TYPE content_status AS ENUM (
    'queued', 'researched', 'generated', 'enriched',
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
  source_url          TEXT                     NOT NULL DEFAULT '' UNIQUE,
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
-- BRANDS (each brand has its own keywords + social accounts)
-- ============================================================
CREATE TABLE IF NOT EXISTS brands (
  id                 SERIAL PRIMARY KEY,
  slug               TEXT UNIQUE NOT NULL,
  name               TEXT NOT NULL,
  description        TEXT NOT NULL DEFAULT '',
  website            TEXT NOT NULL DEFAULT '',
  enabled            BOOLEAN NOT NULL DEFAULT TRUE,
  telegram_bot_token TEXT NOT NULL DEFAULT '',
  telegram_chat_id   TEXT NOT NULL DEFAULT '',
  telegram_enabled   BOOLEAN NOT NULL DEFAULT FALSE,
  -- Per-brand social accounts: [{"platform","display_name","credentials","enabled"}, ...]
  social_accounts    JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- BRAND KEYWORDS (manageable from dashboard, scoped per brand)
-- ============================================================
CREATE TABLE IF NOT EXISTS brand_keywords (
  id SERIAL PRIMARY KEY,
  brand_id INTEGER REFERENCES brands(id) ON DELETE CASCADE,
  keyword TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',
  score NUMERIC(8, 2),
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (brand_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_brand_keywords_brand ON brand_keywords(brand_id);
CREATE INDEX IF NOT EXISTS idx_brand_keywords_source ON brand_keywords(source);

-- ============================================================
-- SETTINGS (admin-managed key-value store; also stores prompts)
-- ============================================================
CREATE TABLE IF NOT EXISTS settings (
  id          SERIAL PRIMARY KEY,
  key         TEXT UNIQUE NOT NULL,
  value       TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- USERS (dashboard auth — admin / editor)
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
  id              SERIAL PRIMARY KEY,
  email           TEXT UNIQUE NOT NULL,
  hashed_password TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT 'editor' CHECK (role IN ('admin','editor')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- Per-brand tagging on existing pipeline tables
ALTER TABLE intents ADD COLUMN IF NOT EXISTS brand_id INTEGER REFERENCES brands(id) ON DELETE SET NULL;
ALTER TABLE intent_clusters ADD COLUMN IF NOT EXISTS brand_id INTEGER REFERENCES brands(id) ON DELETE SET NULL;
ALTER TABLE content ADD COLUMN IF NOT EXISTS brand_id INTEGER REFERENCES brands(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_intents_brand ON intents(brand_id);
CREATE INDEX IF NOT EXISTS idx_clusters_brand ON intent_clusters(brand_id);
CREATE INDEX IF NOT EXISTS idx_content_brand ON content(brand_id);

-- ============================================================
-- Numeric / datetime indexes for dashboard sorting & filtering
-- ============================================================
-- content
CREATE INDEX IF NOT EXISTS idx_content_iteration_count ON content(iteration_count DESC);
CREATE INDEX IF NOT EXISTS idx_content_updated_at ON content(updated_at DESC);

-- intent_clusters
CREATE INDEX IF NOT EXISTS idx_iclusters_intent_count ON intent_clusters(intent_count DESC);
CREATE INDEX IF NOT EXISTS idx_iclusters_covered_count ON intent_clusters(covered_count DESC);
CREATE INDEX IF NOT EXISTS idx_iclusters_created_at ON intent_clusters(created_at DESC);

-- performance
CREATE INDEX IF NOT EXISTS idx_perf_clicks ON performance(clicks DESC);
CREATE INDEX IF NOT EXISTS idx_perf_signups ON performance(signups DESC);

-- brands / users
CREATE INDEX IF NOT EXISTS idx_brands_created_at ON brands(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at DESC);

-- brand_keywords
CREATE INDEX IF NOT EXISTS idx_brand_keywords_created_at ON brand_keywords(created_at DESC);


-- settings
CREATE INDEX IF NOT EXISTS idx_settings_updated_at ON settings(updated_at DESC);

-- ============================================================
-- CONTENT RESOURCES (URL-deduped). Images live on the resource as a JSONB array.
-- ============================================================
CREATE TABLE IF NOT EXISTS content_resources (
  id          SERIAL PRIMARY KEY,
  url         TEXT UNIQUE NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  snippet     TEXT NOT NULL DEFAULT '',
  full_text   TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT '',
  domain      TEXT NOT NULL DEFAULT '',
  -- Images discovered on this source page: [{"url": "...", "alt": "..."}, ...]
  images      JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_content_resources_kind   ON content_resources(kind);
CREATE INDEX IF NOT EXISTS idx_content_resources_domain ON content_resources(domain);

CREATE TABLE IF NOT EXISTS content_resource_relation (
  content_id  TEXT    NOT NULL REFERENCES content(content_id)        ON DELETE CASCADE,
  resource_id INTEGER NOT NULL REFERENCES content_resources(id) ON DELETE CASCADE,
  position    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (content_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_crr_resource ON content_resource_relation(resource_id);

ALTER TABLE content ADD COLUMN IF NOT EXISTS synthesis TEXT NOT NULL DEFAULT '';
